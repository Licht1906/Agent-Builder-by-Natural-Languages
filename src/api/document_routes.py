"""Document endpoints: what sits on one of a creator's Knowledge Bases — #90.

Nested under `/knowledge-bases/{kb_id}/documents` rather than flat, because a
Document's owner is no longer expressed by "the caller's account" alone — it
belongs to one shelf, and `OwnedKnowledgeBase` is what proves the caller may
touch it. `src.workflow.documents` decides what may be stored under what
Knowledge Base; this module is the HTTP door onto it.

Identity is `id`, not a file name — see `src.db.models.Document`. Two
Documents on the same shelf may share a `file_name`, so there is no route that
addresses one by name any more.

**Upload and `POST .../reingest` both queue `src.services.ingestion.ingest_document`
as a `BackgroundTasks` job (RAG-08 / ADR-0044 §4)** — the request returns as
soon as the row exists, and the pipeline runs after. The four collaborators
`ingest_document` needs are resolved here via `Depends`, the same seam every
other call site uses, rather than inside the background task itself: a
background task is called directly, never through FastAPI's own dependency
resolution, so `app.dependency_overrides` can only reach it if the route
already resolved the dependency and handed it along. That is also what lets
the test suite default every upload to safe fakes instead of a real embedding
provider and Qdrant (see `tests/conftest.py`).
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from src.api.knowledge_base_routes import OwnedKnowledgeBase
from src.db.base import get_db
from src.db.models import Document
from src.deployments.rate_limit import RateLimitExceededError, SlidingWindowLimiter
from src.models.schemas import DocumentResponse
from src.services.embedding import EmbeddingClient, get_embedding_client
from src.services.extraction import ExtractionCache, VlmPageClient, get_extraction_cache, get_vlm_page_client
from src.services.ingestion import ingest_document, reset_for_reingest
from src.services.qdrant import QdrantWriteError, get_qdrant_client
from src.services.upload_rate_limit import charge_one_upload, get_upload_rate_limiter
from src.workflow.documents import (
    SUPPORTED_SUFFIXES,
    DocumentError,
    delete_document,
    list_documents,
    path_of,
    store_document,
)

#: What to tell the browser a document's bytes are, by suffix — the same set
#: `SUPPORTED_SUFFIXES` names, so "xem lại" (RAG-12 / #98) never opens
#: something `store_document` would have refused on upload.
_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

router = APIRouter(prefix="/knowledge-bases/{kb_id}/documents", tags=["documents"])

#: The four things `ingest_document` needs, resolved once here so a test can
#: override each independently via `app.dependency_overrides` — see module
#: docstring for why this cannot happen inside the background task itself.
EmbeddingClientDep = Annotated[EmbeddingClient, Depends(get_embedding_client)]
QdrantClientDep = Annotated[QdrantClient, Depends(get_qdrant_client)]
VlmPageClientDep = Annotated[VlmPageClient, Depends(get_vlm_page_client)]
ExtractionCacheDep = Annotated[ExtractionCache, Depends(get_extraction_cache)]
#: How many Documents this owner may upload per minute (#101) — resolved via
#: `Depends` for the same reason the four above are: `app.dependency_overrides`
#: can only reach it if the route resolved it, the same seam
#: `test_deployment_rate_limit.py` uses for the Deployment ceiling.
UploadRateLimiterDep = Annotated[SlidingWindowLimiter, Depends(get_upload_rate_limiter)]


def _queue_ingest(
    background_tasks: BackgroundTasks,
    document_id: int,
    *,
    embedding_client: EmbeddingClient,
    qdrant_client: QdrantClient,
    vlm_client: VlmPageClient,
    extraction_cache: ExtractionCache,
) -> None:
    """What upload and reingest both do once the row is ready: hand `ingest_document` its four resolved collaborators."""
    background_tasks.add_task(
        ingest_document,
        document_id,
        extraction_cache=extraction_cache,
        vlm_client=vlm_client,
        embedding_client=embedding_client,
        qdrant_client=qdrant_client,
    )


def get_owned_document(
    document_id: int, knowledge_base: OwnedKnowledgeBase, db: Annotated[Session, Depends(get_db)]
) -> Document:
    """The Document at `document_id` if it is on `knowledge_base`, otherwise 404."""
    document = db.get(Document, document_id)
    if document is None or document.kb_id != knowledge_base.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy tài liệu")
    return document


@router.get("", response_model=list[DocumentResponse])
def list_kb_documents(knowledge_base: OwnedKnowledgeBase, db: Annotated[Session, Depends(get_db)]) -> list[Document]:
    """Every Document on this shelf, newest first."""
    return list_documents(db, knowledge_base)


@router.get("/{document_id}", response_model=DocumentResponse)
def get_kb_document(document: Annotated[Document, Depends(get_owned_document)]) -> Document:
    """One Document's current pipeline position — `status` plus `status_detail` (RAG-08).

    What a poller calls after upload to watch `pending → extracting →
    chunking → embedding → indexed` (or `failed`, with a reason) happen.
    """
    return document


@router.get("/{document_id}/content")
def read_kb_document_content(document: Annotated[Document, Depends(get_owned_document)]) -> FileResponse:
    """The original bytes behind `document` — what "xem lại" (RAG-12 / #98) opens.

    Served straight off `path_of`, the same path `ingest_document` reads from —
    a re-ingest and this endpoint can never disagree about which bytes are "the
    document". `content_disposition_type="inline"` so a browser tab shows the
    file rather than always forcing a download, and `filename` carries the
    document's own name rather than the numeric id `path_of` actually stores it
    under.
    """
    suffix = Path(document.file_name).suffix.lower()
    return FileResponse(
        path_of(document),
        media_type=_CONTENT_TYPES.get(suffix, "application/octet-stream"),
        filename=document.file_name,
        content_disposition_type="inline",
    )


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_kb_document(
    knowledge_base: OwnedKnowledgeBase,
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File(description=f"Tệp {', '.join(sorted(SUPPORTED_SUFFIXES))}")],
    embedding_client: EmbeddingClientDep,
    qdrant_client: QdrantClientDep,
    vlm_client: VlmPageClientDep,
    extraction_cache: ExtractionCacheDep,
    upload_rate_limiter: UploadRateLimiterDep,
) -> Document:
    """Upload one Document onto this shelf.

    429 first, before anything else runs — #101's ceiling on how often this
    owner may set the ingest pipeline going, the same "charge before the
    expensive thing starts" call `charge_one_run` makes for a chat turn
    (ADR-0037 §1). A refusal here costs nothing: no bytes read, no row written.

    422 for anything the shelf refuses — an unsupported type, an empty file, a
    name that is not a file name, something over the ceiling, or the same
    content already sitting here. All of them are faults of *this request*,
    which is exactly what a 422 says, and all of them are worth saying now: a
    creator attaching a source finds out here rather than on a run three
    screens later.

    The whole file is read into memory before anything is written, because the
    size limit has to be enforced against the bytes that actually arrived
    rather than against a `Content-Length` the client supplied about itself.
    That is affordable precisely because `MAX_DOCUMENT_BYTES` is small.

    Returns as soon as the row is stored, `status="pending"` — the ingest
    pipeline (RAG-08) runs after, as a background task, so an upload never
    blocks on extraction/chunking/embedding/indexing.
    """
    try:
        charge_one_upload(upload_rate_limiter, knowledge_base.owner_id)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.message,
            headers={"Retry-After": str(exc.retry_after_seconds), "X-RateLimit-Limit": str(exc.limit)},
        ) from None

    content = await file.read()
    try:
        document = store_document(db, knowledge_base, file.filename or "", content)
    except DocumentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None

    _queue_ingest(
        background_tasks,
        document.id,
        embedding_client=embedding_client,
        qdrant_client=qdrant_client,
        vlm_client=vlm_client,
        extraction_cache=extraction_cache,
    )
    return document


@router.post("/{document_id}/reingest", response_model=DocumentResponse, status_code=status.HTTP_202_ACCEPTED)
def reingest_kb_document(
    document: Annotated[Document, Depends(get_owned_document)],
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
    embedding_client: EmbeddingClientDep,
    qdrant_client: QdrantClientDep,
    vlm_client: VlmPageClientDep,
    extraction_cache: ExtractionCacheDep,
) -> Document:
    """Run the pipeline again, from whatever bytes are on disk right now.

    What stands in for a broker here (see `src.services.ingestion`'s module
    docstring): a Document stuck `failed`, or the reference machine's process
    having been restarted mid-ingest, is recovered by calling this, not by an
    automatic retry running unattended. Resets to `pending` before the
    background task is even queued, so a client polling right after this
    call never reads the *previous* run's terminal status.
    """
    reset_for_reingest(db, document)
    _queue_ingest(
        background_tasks,
        document.id,
        embedding_client=embedding_client,
        qdrant_client=qdrant_client,
        vlm_client=vlm_client,
        extraction_cache=extraction_cache,
    )
    return document


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_kb_document(
    document: Annotated[Document, Depends(get_owned_document)],
    db: Annotated[Session, Depends(get_db)],
    qdrant_client: QdrantClientDep,
) -> None:
    """Remove one Document from this shelf — its Qdrant points too (RAG-09).

    A workflow that still points at the Knowledge Base is not hunted down; the
    node it runs through searches a shelf that has one fewer thing on it the
    next time it runs (ADR-0043's consequence, once RAG-05 wires `source_ids`
    up to Knowledge Bases). Documents have no `agent_kb_links`-style
    assignment of their own to warn about — that check is
    `delete_knowledge_base`'s, scoped to the shelf, not a single file on it.

    A Qdrant failure surfaces as 503: nothing wrong with the request, the
    dependency is unreachable, and the caller can just try again once it is.
    """
    try:
        delete_document(db, document, qdrant_client)
    except QdrantWriteError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None
