"""Ingestion orchestrator (RAG-08 / ADR-0044 §4).

Wires the four pieces RAG-01/02/06/07 already built into one job: extract
(`src.services.extraction`) → chunk (`src.services.chunking`) → batch embed
(`src.services.embedding`) → upsert (`src.services.qdrant`). It runs as a
FastAPI `BackgroundTasks` job, not a Celery worker — the ticket's own
reasoning: state already lives in `Document.status`, so a job a restart
kills is recoverable by running it again, which is what `reset_for_reingest`
below and `POST .../reingest` exist for. No broker, no queue, one fewer
failure mode in front of a demo.

Every step writes `Document.status` before it starts, so a poller watching
one Document sees the pipeline's real position rather than only "done" or
"not done" — and a failure at any step stops there. `status=FAILED` with a
readable `status_detail`, and no later step ever runs over a Document whose
extraction or chunking did not, in fact, succeed.

**Re-ingest deletes before it writes.** ADR-0044 §4 reverses a documented
decision (`src.workflow.knowledge._extracted`'s *"nothing cached to go
stale"*) and keeps the property that decision protected, by a different
mechanism: a Document's old points are gone before its new ones land, so a
corrected handbook does not sit in the index beside the version it replaced.
`delete_document_points` runs on *every* ingest, not only a re-ingest — the
first ingest just deletes nothing, which is what makes this one code path
instead of a re-ingest branch that could fail to be called.

**Extraction spend is recorded here, not inside RAG-06 itself** (RAG-15 /
#101). `extract_document`'s `on_vlm_spend` fires at most once per ingest, and
only for a fresh (uncached) document that actually reached the VLM — a cache
hit or a text-native document calls it never, which is what keeps a duplicate
upload from ever writing a second `ingestion_costs` row.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from qdrant_client import QdrantClient, models
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.base import SessionLocal
from src.db.models import Document, DocumentStatus
from src.services.chunking import Passage, chunk_document
from src.services.embedding import EmbeddingClient, client_for
from src.services.extraction import ExtractionCache, VlmPageClient, VlmSpend, extract_document
from src.services.ingestion_costs import check_soft_limit, record_vlm_spend
from src.services.qdrant import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    PointPayload,
    delete_document_points,
    get_qdrant_client,
    sparse_vector_of,
    upsert_passages,
)
from src.workflow.documents import path_of

logger = logging.getLogger(__name__)

#: A point's id is derived from `(document_id, chunk_index)` rather than
#: minted fresh per ingest, so two upserts of the same chunk — a retry, or an
#: ingest that raced itself — land on the same point instead of leaving
#: duplicates for `delete_document_points` to have to clean up later.
_POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "agent-builder.documents_kb.chunk")


def _point_id(document_id: int, chunk_index: int) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{document_id}:{chunk_index}"))


def _set_status(session: Session, document: Document, status: DocumentStatus, *, detail: str | None = None) -> None:
    document.status = status
    document.status_detail = detail
    session.commit()


def _build_point(document: Document, index: int, passage: Passage, vector: list[float]) -> models.PointStruct:
    payload: PointPayload = {
        "kb_id": str(document.kb_id),
        "owner_id": str(document.knowledge_base.owner_id),
        "doc_id": str(document.id),
        "doc_name": document.file_name,
        "section_path": passage.section_path,
        "page_number": passage.page_number,
        "chunk_text": passage.text,
        "chunk_index": index,
    }
    # `dict(payload)`: `qdrant_client`'s stub wants `dict[str, Any]`, and a
    # `TypedDict` is not one by mypy's rules even though it is one at
    # runtime — the copy is for the type checker, not for correctness.
    return models.PointStruct(
        id=_point_id(document.id, index),
        # Both halves of hybrid search index `embeddable`, not `text`: the
        # Section Path has to be inside what gets scored, or no vector and no
        # keyword posting in the collection contains the words "Điều 13" and
        # the question that names it can match on that term in neither half.
        # `chunk_text` in the payload stays the bare body — the breadcrumb is
        # carried beside it, and a citation should not repeat it inline.
        vector={DENSE_VECTOR_NAME: vector, SPARSE_VECTOR_NAME: sparse_vector_of(passage.embeddable)},
        payload=dict(payload),
    )


def ingest_document(
    document_id: int,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    extraction_cache: ExtractionCache | None = None,
    vlm_client: VlmPageClient | None = None,
    embedding_client: EmbeddingClient | None = None,
    qdrant_client: QdrantClient | None = None,
) -> None:
    """Run the full pipeline for one Document. The one function `BackgroundTasks` calls.

    Every collaborator is an optional override, the same injectable shape
    `extract_document`/`chunk_document`/`get_embedding_client` already use —
    production leaves them all `None` and gets the real HTTP/Qdrant clients;
    a caller (the route, a test, `reset_for_reingest` below) that already
    resolved one through FastAPI's own DI passes it through here so the two
    never disagree about which client answered.

    Opens its own session rather than accepting one from a caller: this runs
    detached from any request, after the request that queued it has already
    returned, so there is no request-scoped session left to borrow.
    """
    session = session_factory()
    try:
        document = session.get(Document, document_id)
        if document is None:
            logger.warning("Ingest requested for document %s, which no longer exists", document_id)
            return

        try:
            content = path_of(document).read_bytes()

            _set_status(session, document, DocumentStatus.EXTRACTING)

            def _on_vlm_spend(spend: VlmSpend) -> None:
                # RAG-15 / #101: recorded here, inside the pipeline, rather than
                # by `extract_document` itself — pricing and persistence are a
                # concern of the *caller* that has a Document and a session,
                # not of the module whose only job is to read bytes into blocks.
                if record_vlm_spend(session, document, spend) is not None:
                    check_soft_limit(session, document.knowledge_base.owner_id)

            extraction = extract_document(
                content,
                document.file_name,
                cache=extraction_cache,
                vlm_client=vlm_client,
                on_vlm_spend=_on_vlm_spend,
            )
            # RAG-06's own flag, carried onto the row so the upload screen (#98)
            # can warn without re-reading the extraction cache — a fact about
            # this Document, not just something the pipeline logged in passing.
            document.low_confidence = extraction.flagged_for_review

            _set_status(session, document, DocumentStatus.CHUNKING)
            passages = chunk_document(extraction)

            _set_status(session, document, DocumentStatus.EMBEDDING)
            embed = embedding_client if embedding_client is not None else client_for(session)
            vectors = embed([passage.embeddable for passage in passages])

            points = [
                _build_point(document, index, passage, vector)
                for index, (passage, vector) in enumerate(zip(passages, vectors, strict=True))
            ]
            qdrant = qdrant_client if qdrant_client is not None else get_qdrant_client()
            delete_document_points(qdrant, str(document.id))
            upsert_passages(qdrant, points)

            document.chunk_count = len(passages)
            document.indexed_at = datetime.now(UTC)
            _set_status(session, document, DocumentStatus.INDEXED)
        except Exception as exc:
            logger.exception("Ingestion failed for document %s (kb=%s)", document.id, document.kb_id)
            # A DB error is one of the things that can land here (a step's own
            # `_set_status` commit failing, say) — that leaves the session's
            # transaction unable to commit again until it is rolled back, which
            # would otherwise take *this* commit down too and leave the
            # Document silently stuck instead of honestly `failed`.
            session.rollback()
            _set_status(session, document, DocumentStatus.FAILED, detail=str(exc))
    finally:
        session.close()


def reset_for_reingest(session: Session, document: Document) -> None:
    """Put `document` back to `pending` so a poller never reads its *previous* terminal status.

    Called synchronously, before the route that calls it queues
    `ingest_document` as a background task — the reset has to land before the
    response goes out, or a client that polls immediately after `POST
    .../reingest` returns could still see `indexed`/`failed` from the run
    being replaced.
    """
    document.status = DocumentStatus.PENDING
    document.status_detail = None
    session.commit()


#: What `recover_interrupted_ingestions` writes into `status_detail` — a
#: reason a creator's upload screen (#98) can show as-is, and the string this
#: module's own test asserts on to prove *this* ran rather than some other
#: failure landing a Document on `failed` by coincidence.
INTERRUPTED_DETAIL = "Bị gián đoạn giữa chừng (khởi động lại server) — hãy ingest lại"

#: Every status a Document sits in only while a pipeline is actively working
#: on it. `pending` is included: a `BackgroundTasks` job lives entirely
#: inside the process that accepted the upload, so a Document still `pending`
#: at boot was never picked up by anybody in the process now running.
_IN_PROGRESS_STATUSES = (
    DocumentStatus.PENDING,
    DocumentStatus.EXTRACTING,
    DocumentStatus.CHUNKING,
    DocumentStatus.EMBEDDING,
)


def recover_interrupted_ingestions(session_factory: Callable[[], Session] = SessionLocal) -> int:
    """At boot, mark every Document a previous process left mid-pipeline as `failed`.

    Deliberately does not resume anything on its own — no automatic retry
    runs here, quietly, on a schedule this module controls. `BackgroundTasks`
    has no state that survives a restart to resume *from*, so the honest
    thing this can do is make `Document.status` stop claiming work is
    underway when nothing is doing it, and leave `POST .../reingest` as the
    one deliberate way to actually run the pipeline again — see the module
    docstring's reasoning for why that endpoint exists at all.

    Returns how many rows this touched, for the startup log line.
    """
    session = session_factory()
    try:
        stuck = list(session.execute(select(Document).where(Document.status.in_(_IN_PROGRESS_STATUSES))).scalars())
        for document in stuck:
            document.status = DocumentStatus.FAILED
            document.status_detail = INTERRUPTED_DETAIL
        session.commit()
        return len(stuck)
    finally:
        session.close()
