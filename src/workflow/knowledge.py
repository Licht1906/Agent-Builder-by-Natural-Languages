"""What a `KnowledgeSearch` node reads when it runs — the seam behind #45's grounding.

ADR-0021 gives the node one config field, `source_ids`, holding Knowledge Base
ids a creator actually attached, never a name a model read off a description
(ADR-0025 §5, ADR-0043 §1). This module is what turns one of those refs into
text a model can answer from.

**A seam, not a search engine.** `KnowledgeStore` is the whole contract — a
question and some refs in, passages out — and the runtime holds one rather than
reaching for documents itself. That is what lets a test run the real
`document_qa` graph against a shelf it uploaded a moment earlier, and what lets
the connected-account half of `ADDRESSABLE_DATA_KINDS` arrive later (#47's MCP
work) as a second implementation rather than as a rewrite of the node.

**One store.** For a while there were two, because two branches built a RAG
stack in parallel and the merge kept both: a pgvector one that chunked, scored
and ranked the shelf off disk on every question, and this one. They answered the
same question two ways, and the pgvector one had to reinterpret a Knowledge Base
id as a directory name to answer it at all — so it is gone, along with
`src.workflow.index`, `src.workflow.chunking`, `src.workflow.retrieval` and the
`document_chunks` table. What replaced each piece of it:

- ranking — `QdrantKnowledgeStore` below: dense and sparse over one index,
  RRF-fused by Qdrant itself, reordered and thresholded by a cross-encoder
  (ADR-0044 §3).
- chunking — `src.services.chunking`, which cuts on the boundaries a document's
  own author wrote (`Chương III > Điều 12 > Khoản 2`) rather than on a character
  budget.
- reading the bytes — `src.services.extraction`, including the vision model a
  scanned page needs and a text layer cannot give.
- putting them in the index — `src.services.ingestion`, at upload time, so a
  question is never the thing that pays for indexing a document.

**What a ref means, and what still does not write one.** A ref is a Knowledge
Base id, whose Documents are uploaded through
`POST /knowledge-bases/{kb_id}/documents` (`src.api.document_routes`). The
interview still leaves `DataSource.ref` as `None` (ADR-0020) — a model reading a
description cannot know which shelf was meant — so a ref is attached by a
person, on the spec screen or on the canvas.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Depends
from pydantic import BaseModel, ConfigDict
from qdrant_client import QdrantClient, models
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.base import get_db
from src.db.models import Agent, ExternalConnection, KnowledgeBase, KnowledgeBaseMode
from src.services.embedding import EmbeddingClient, get_embedding_client
from src.services.qdrant import (
    COLLECTION_NAME,
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    get_qdrant_client,
    sparse_vector_of,
)
from src.services.reranker import RerankerClient, get_reranker_client
from src.services.secret_store import (
    SecretNotFoundError,
    SecretStore,
    SecretUnreadableError,
    get_secret_store,
)
from src.services.vector_adapter import (
    HttpPost,
    SearchHit,
    VectorDBAdapterError,
    adapter_for_connection,
    http_post_via_httpx,
    mapped_field,
)

logger = logging.getLogger(__name__)

#: The owner id `get_knowledge_store` bakes into a store built for an
#: `agent_id` that does not exist. No `KnowledgeBase.owner_id` is ever
#: negative, so every source in a search against this owner raises
#: `KnowledgeSourceError` — the honest answer, not a silent empty result that
#: would look identical to every shelf actually coming up empty.
_NO_SUCH_OWNER = -1

#: How many passages a search hands back when the caller does not say. Enough
#: that a question spanning two sections of a handbook is answerable, small
#: enough that the answering node's prompt stays mostly *prompt*.
#:
#: Six rather than four since the first real document was measured: a 34-page
#: regulation chunks into ~156 passages, and on a question whose answer the
#: semantic ranker placed *second*, fusion with a lexical ranker that placed it
#: nineteenth landed it at seven. Four slots is a budget set for a handbook,
#: applied to a corpus forty times larger. Six passages is still under 6k
#: characters of grounding.
#:
#: Eight since the regulation was measured against questions whose answer is
#: *assembled* rather than looked up. "Thời gian học tối đa của cử nhân" is
#: settled by Điều 3 khoản 3 together with the design-time table in Điều 2, and
#: the clause carrying it ranks seventh — the question is phrased "tối đa" while
#: the clause is written "chậm tiến độ", so no chunking makes the two meet. Ask
#: the same thing in the document's own words and it ranks third. Two more slots
#: is what a vocabulary gap costs to survive; on the seven-question set it is
#: the difference between six answerable and seven.
DEFAULT_PASSAGE_LIMIT = 8

#: How many of those slots one section may take. Two chunks of the same clause
#: are two views of one rule; a third is the fourth-best *section* going unread.
#: The same argument as near-duplicate removal, one level up — and on the
#: measured document it was the difference between the top slots being two
#: sections shown twice each and being four different clauses.
_MAX_PER_SECTION = 2

#: How deep each ranker looks before the two are merged. Wider than the limit on
#: purpose: fusion can only promote a passage both rankers found, so a candidate
#: list cut to four would throw away the agreement it exists to reward.
_CANDIDATE_DEPTH = 20


class KnowledgeSourceError(ValueError):
    """A `source_ids` ref that cannot honestly be searched. Message is user-facing.

    Raised rather than skipped. A source silently dropped is an answer that looks
    grounded in every document the creator attached while resting on a subset of
    them — the failure a groundedness check cannot see. The runtime turns this
    into a visible `tool_result` on the run rather than a crash (see
    `src.workflow.runtime`), so the creator is told which ref is wrong.
    """


class Passage(BaseModel):
    """One stretch of a document, and which document it came from.

    `source_ref` travels with the text rather than being resolved back later:
    what the answering node is asked to ground in is *this passage from this
    source*, and a citation reassembled afterwards from the ref list would be a
    citation nobody checked.

    `doc_name`, `section_path` and `page_number` (RAG-11 / #97 / ADR-0044 §6,
    Consequences) are what let the model cite at the granularity a reader
    would expect — *"Điều 12"*, not *"handbook.pdf"* — without this platform
    growing an `article_number` column: `section_path` already carries the
    article for a document that has one, and is deliberately `None` for a
    Document without a hierarchy rather than a domain field with no honest
    value there. Frozen and `extra="forbid"`, so a field either belongs on
    every `Passage` or on none of them — see `_citation_label` in
    `src.workflow.runtime` for how a `None` section still cites something.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: str
    text: str
    doc_name: str
    #: `"Chương III > Điều 12 > Khoản 2"`-shaped, or `None` where the source
    #: document has no hierarchy for the chunker to have found one in.
    section_path: str | None
    #: `None` for every source with no page concept — `.md`/`.txt`/`.docx`, whose
    #: formats have no pages for extraction to have numbered.
    page_number: int | None


class KnowledgeStore(Protocol):
    """Where a `KnowledgeSearch` node's passages come from.

    One method, because the node asks one question: *given what the user wants to
    know and which sources this node was pointed at, what should the model read?*
    Everything about how that is answered — files today, a connected account
    later — stays behind it.
    """

    def search(
        self, query: str, *, source_refs: Sequence[str], limit: int = DEFAULT_PASSAGE_LIMIT
    ) -> tuple[Passage, ...]:
        """The passages of `source_refs` most relevant to `query`, best first."""
        ...


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One retrieval candidate, ready for reranking — the shape a managed hit and a BYODB hit both reduce to.

    Reranking scores raw text, not a `models.ScoredPoint` or a `SearchHit`;
    collapsing both to this immediately after retrieval (`_candidate_of_point`,
    `_candidate_of_hit`) is what lets `_reranked` treat a managed-mode hit and
    a RAG-21 BYODB hit identically — the acceptance criterion's own "đều trả
    đúng định dạng Passage" made literal, rather than a special case per
    source.
    """

    text: str
    doc_name: str
    section_path: str | None
    page_number: int | None


@dataclass(frozen=True, slots=True)
class QdrantKnowledgeStore:
    """Knowledge Bases in Qdrant: hybrid dense+sparse, RRF-fused, reranked — RAG-10 / ADR-0044 §3.

    `source_refs` here are Knowledge Base ids (`KnowledgeSearchConfig.source_ids`,
    ADR-0043 §1), not file paths — the `KnowledgeStore` protocol's parameter name
    stays the name the `KnowledgeStore` protocol gave it, but what a ref
    *means* is this store's own business, same as ADR-0031 §3 always said it
    would be.

    `owner_id` is baked in at construction (`get_knowledge_store`), not passed
    to `search`: the same filter that makes tenant isolation impossible to
    forget also makes it impossible to spoof from a call site.

    **BYODB (RAG-21 / #107).** A `KnowledgeBase` in `KnowledgeBaseMode.BYODB`
    has no points in the internal `documents_kb` collection at all — it names
    an `ExternalConnection` instead, and `search` reaches it through
    `src.services.vector_adapter` rather than through `_fused_candidates`.
    Hybrid stays exactly what RAG-10 built, for managed-mode shelves only:
    `src.services.vector_adapter`'s own docstring is where "why not hybrid for
    BYODB too" is answered. A search naming both kinds of shelf at once runs
    both paths and reranks the union — one ranked answer, not two.
    """

    db: Session
    owner_id: int
    qdrant_client: QdrantClient
    embedding_client: EmbeddingClient
    reranker_client: RerankerClient
    secret_store: SecretStore
    #: The transport `QdrantExternalAdapter` dials a BYODB shelf's own Qdrant
    #: through — the real, SSRF-guarded one by default, overridable the same
    #: way every other collaborator here is, so a test scripts a BYODB search
    #: without reaching the network. Ignored for every other provider, which
    #: never dial out at all (`ProviderNotSupportedError` first).
    http_post: HttpPost = http_post_via_httpx

    def search(
        self, query: str, *, source_refs: Sequence[str], limit: int = DEFAULT_PASSAGE_LIMIT
    ) -> tuple[Passage, ...]:
        """Dense top-k and sparse top-k, fused by RRF, reordered by the reranker, thresholded.

        Empty is a result, returned in three honest cases: no source named,
        a blank query, nothing survives the score threshold. A reranker
        that is down or unusable degrades to the RRF order with a warning —
        never a 500 (ADR-0044 §2) — and skips the threshold entirely, since
        an RRF rank score is not the cross-encoder score the threshold is
        calibrated against.
        """
        kbs = self._owned_kbs(source_refs)
        if not kbs or not query.strip():
            return ()

        managed_ids = [str(kb.id) for kb in kbs if kb.mode is KnowledgeBaseMode.MANAGED]
        byodb_kbs = [kb for kb in kbs if kb.mode is KnowledgeBaseMode.BYODB]

        # Computed once and shared by both paths below — `_fused_candidates`
        # used to embed the query itself; hoisted here so a search spanning
        # both a managed and a BYODB shelf does not pay for the embedding twice.
        dense_vector = self.embedding_client([query])[0]

        candidates: list[_Candidate] = []
        if managed_ids:
            fused = self._fused_candidates(query, managed_ids, dense_vector)
            candidates.extend(_candidate_of_point(point) for point in fused)
        if byodb_kbs:
            candidates.extend(self._byodb_candidates(dense_vector, byodb_kbs))

        if not candidates:
            return ()
        return self._reranked(query, candidates, limit)

    def _fused_candidates(self, query: str, kb_ids: list[str], dense_vector: list[float]) -> list[models.ScoredPoint]:
        """Dense top-k and sparse top-k over `kb_ids`, RRF-fused into one candidate list — ADR-0044 §3."""
        source_filter = models.Filter(
            must=[
                models.FieldCondition(key="kb_id", match=models.MatchAny(any=kb_ids)),
                models.FieldCondition(key="owner_id", match=models.MatchValue(value=str(self.owner_id))),
            ]
        )
        return fused_points(self.qdrant_client, COLLECTION_NAME, query, dense_vector, source_filter)

    def _byodb_candidates(self, dense_vector: list[float], kbs: Sequence[KnowledgeBase]) -> list[_Candidate]:
        """`kbs` (every one `byodb`), searched dense-only through its own `ExternalConnection` — RAG-21.

        Raised rather than dropped on a failure, `KnowledgeSourceError`'s own
        reason: a BYODB shelf that timed out or is down must read as a named,
        visible failure on the run (the ticket's own "DB ngoài timeout/chết →
        agent nhận lỗi rõ ràng, không sập hệ thống"), not as a shelf that
        silently contributed nothing — that would be an answer that looks
        grounded in every source the creator attached while resting on fewer
        of them.
        """
        settings = get_settings()
        candidates: list[_Candidate] = []
        for kb in kbs:
            connection = (
                self.db.get(ExternalConnection, kb.external_connection_id) if kb.external_connection_id else None
            )
            if connection is None:
                raise KnowledgeSourceError(
                    f"Knowledge Base {kb.name!r} ở chế độ byodb nhưng chưa gắn kết nối ngoài nào"
                )
            secret = self._resolve_secret(connection)
            adapter = adapter_for_connection(connection, secret=secret, http_post=self.http_post)
            try:
                hits = adapter.search(dense_vector, top_k=settings.knowledge_dense_top_k)
            except VectorDBAdapterError as exc:
                raise KnowledgeSourceError(
                    f"Không truy vấn được database ngoài của Knowledge Base {kb.name!r}: {exc}"
                ) from None
            logger.warning(
                "Knowledge Base %s (%r) là byodb — truy vấn dense-only, không có hybrid fusion", kb.id, kb.name
            )
            candidates.extend(
                candidate for hit in hits if (candidate := _candidate_of_hit(hit, connection)) is not None
            )
        return candidates

    def _resolve_secret(self, connection: ExternalConnection) -> str | None:
        """The plaintext credential `connection.secret_ref` names, or `None` if it has none.

        Mirrors `src.workflow.external_connections.run_connection_test`'s own
        resolution exactly — same two failure modes, same `KnowledgeSourceError`
        rather than an unhandled `SecretNotFoundError`/`SecretUnreadableError`
        reaching the caller.
        """
        if not connection.secret_ref:
            return None
        try:
            return self.secret_store.get(connection.secret_ref)
        except (SecretNotFoundError, SecretUnreadableError) as exc:
            raise KnowledgeSourceError(
                f"Không đọc được thông tin xác thực cho kết nối {connection.name!r}: {exc}"
            ) from None

    def _reranked(self, query: str, candidates: list[_Candidate], limit: int) -> tuple[Passage, ...]:
        """`candidates`, reordered and thresholded by the reranker — or the bare fused order if it is unusable.

        `limit` is `DEFAULT_PASSAGE_LIMIT` unless a caller asks for fewer —
        deliberately smaller than either `top_k` above: reranking already
        paid the CPU cost ADR-0044 §2 measured (~2.2s at 10 candidates), so
        cutting the *answer* down further here is a prompt-budget decision,
        not another latency one.

        **What a dead reranker loses, and what it must not.** The threshold
        goes: an RRF rank score is not the cross-encoder score it is calibrated
        against, so applying it there would be comparing two different numbers.
        The per-section cap stays. It is a rule about the shape of the
        *answer* — two chunks of one clause are two views of one rule — and
        nothing about it depends on who did the ordering. This service degrades
        rather than failing, so the degraded path is the one most likely to be
        serving, and it is the worst one to quietly hand eight slots of the same
        Điều to.
        """
        return ranked_passages(query, candidates, limit=limit, reranker_client=self.reranker_client)

    def _owned_kbs(self, source_refs: Sequence[str]) -> list[KnowledgeBase]:
        """Every ref in `source_refs`, checked real and owned by `self.owner_id` — or a `KnowledgeSourceError` naming what is not.

        Raised rather than skipped, for `KnowledgeSourceError`'s own reason:
        a Knowledge Base a creator attached but that got deleted, or that
        belongs to someone else because a stored graph carries no owner of
        its own, must not quietly drop out of the search — that is an
        answer that looks grounded in every shelf the creator named while
        resting on a subset of them.

        Returns `KnowledgeBase` rows rather than bare ids — `search` needs
        `.mode` to route each one to the managed hybrid path or the RAG-21
        BYODB path.
        """
        if not source_refs:
            return []
        try:
            wanted = {int(ref) for ref in source_refs}
        except ValueError:
            raise KnowledgeSourceError(
                f"Id Knowledge Base không hợp lệ trong nguồn dữ liệu: {', '.join(source_refs)}"
            ) from None
        owned = {
            kb.id: kb
            for kb in self.db.execute(
                select(KnowledgeBase).where(KnowledgeBase.id.in_(wanted), KnowledgeBase.owner_id == self.owner_id)
            ).scalars()
        }
        missing = sorted(wanted - owned.keys())
        if missing:
            raise KnowledgeSourceError(
                f"Không tìm thấy Knowledge Base cho nguồn dữ liệu: {', '.join(str(kb_id) for kb_id in missing)}"
            )
        return list(owned.values())


def fused_points(
    client: QdrantClient, collection: str, query: str, dense_vector: list[float], source_filter: models.Filter
) -> list[models.ScoredPoint]:
    """Dense top-k and sparse top-k under `source_filter`, RRF-fused by Qdrant — ADR-0044 §3.

    A function rather than a method so the exported package (DS-18) ranks its
    own collection exactly as the platform ranks `documents_kb`.
    """
    settings = get_settings()
    return client.query_points(
        collection,
        prefetch=[
            models.Prefetch(
                query=dense_vector, using=DENSE_VECTOR_NAME, filter=source_filter, limit=settings.knowledge_dense_top_k
            ),
            models.Prefetch(
                query=sparse_vector_of(query),
                using=SPARSE_VECTOR_NAME,
                filter=source_filter,
                limit=settings.knowledge_sparse_top_k,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        query_filter=source_filter,
        limit=max(settings.knowledge_dense_top_k, settings.knowledge_sparse_top_k),
        with_payload=True,
    ).points


def passages_of_points(
    query: str, points: Sequence[models.ScoredPoint], *, limit: int, reranker_client: RerankerClient | None
) -> tuple[Passage, ...]:
    """Managed-mode hits as the answer: reranked (or not), thresholded, spread across sections."""
    candidates = [_candidate_of_point(point) for point in points]
    return ranked_passages(query, candidates, limit=limit, reranker_client=reranker_client) if candidates else ()


def ranked_passages(
    query: str, candidates: Sequence[_Candidate], *, limit: int, reranker_client: RerankerClient | None
) -> tuple[Passage, ...]:
    """`candidates`, reordered and thresholded by the reranker — or in fused order without one.

    `reranker_client=None` is a reranker switched off on purpose (the exported
    package's `reranker.enabled: false`): the fused order, no threshold, and no
    warning, since nothing is down. A reranker that *is* configured but
    answers unusably degrades the same way, with the warning.
    """
    if reranker_client is None:
        ordered = list(candidates)
    else:
        rerank_result = reranker_client(query, [candidate.text for candidate in candidates])
        if not rerank_result.reranked or rerank_result.scores is None:
            logger.warning("Reranker unavailable for a knowledge search — returning fused order, unthresholded")
            ordered = [candidates[i] for i in rerank_result.ranked_indices]
        else:
            threshold = get_settings().knowledge_rerank_score_threshold
            ordered = [candidates[i] for i in rerank_result.ranked_indices if rerank_result.scores[i] > threshold]
    return tuple(_passage_of(candidate) for candidate in _spread_across_sections(ordered, limit=limit))


def _spread_across_sections(candidates: Sequence[_Candidate], *, limit: int) -> list[_Candidate]:
    """The best `limit` of `candidates`, no section taking more than `_MAX_PER_SECTION` slots.

    Applied after the reranker rather than before it: what is being capped is
    the *answer*, and until the cross-encoder has spoken there is no answer to
    cap — a candidate dropped at fusion time is one the reranker never got to
    promote.

    Two chunks of the same clause are two views of one rule; a third is the
    next *section* going unread, and on the measured regulation it was the
    difference between the top slots being two sections shown twice each and
    being four different clauses.
    """
    taken: dict[str, int] = {}
    spread: list[_Candidate] = []
    for position, candidate in enumerate(candidates):
        # A Passage with no Section Path is its own section, keyed by where it
        # sat: a document with no hierarchy has exactly one section, and a
        # shared key would cap it at two passages and make it unanswerable.
        section = candidate.section_path or f"#{position}"
        if taken.get(section, 0) >= _MAX_PER_SECTION:
            continue
        taken[section] = taken.get(section, 0) + 1
        spread.append(candidate)
        if len(spread) == limit:
            break
    return spread


def _candidate_of_point(point: models.ScoredPoint) -> _Candidate:
    """One internal Qdrant hit (managed mode) as a `_Candidate` — the payload always `src.services.qdrant.PointPayload`-shaped."""
    payload = point.payload or {}
    doc_name = str(payload.get("doc_name", ""))
    section_path = payload.get("section_path")
    page_number = payload.get("page_number")
    return _Candidate(
        text=str(payload.get("chunk_text", "")),
        doc_name=doc_name,
        section_path=str(section_path) if section_path is not None else None,
        page_number=int(page_number) if page_number is not None else None,
    )


def _candidate_of_hit(hit: SearchHit, connection: ExternalConnection) -> _Candidate | None:
    """One BYODB `SearchHit` as a `_Candidate`, `connection.field_mapping` (RAG-20) applied — `None` if it carries no text.

    A hit with no readable text field is dropped rather than turned into an
    empty-string citation: `mapped_field` already tried the remapped key and
    the standard default, so a still-missing text means this row of the
    creator's own collection genuinely does not hold what a `Passage` needs.

    `doc_name` falls back to the connection's own `collection_name` when the
    external payload names none: an honest, non-empty citation beats a domain
    field with no honest value there.
    """
    mapping = connection.field_mapping
    text = mapped_field(hit.payload, mapping, "text_field")
    if not text:
        return None
    doc_name = mapped_field(hit.payload, mapping, "doc_name_field")
    section_path = mapped_field(hit.payload, mapping, "section_path_field")
    page_number = mapped_field(hit.payload, mapping, "page_number_field")
    return _Candidate(
        text=str(text),
        doc_name=str(doc_name) if doc_name else connection.collection_name,
        section_path=str(section_path) if section_path is not None else None,
        page_number=int(page_number) if isinstance(page_number, int | float) else None,
    )


def _passage_of(candidate: _Candidate) -> Passage:
    """One retrieval candidate as a `Passage` — the citation a `KnowledgeSearch` node's findings carry.

    `source_ref` is just the document name — the "found N passages from" list
    (`_found_summary`) reads better grouped by Document than repeated once per
    Điều. The article-level detail lives in `section_path`/`page_number`
    instead (RAG-11 / #97), which `_citation_label` in `src.workflow.runtime`
    is what actually renders for the model.
    """
    return Passage(
        source_ref=candidate.doc_name,
        text=candidate.text,
        doc_name=candidate.doc_name,
        section_path=candidate.section_path,
        page_number=candidate.page_number,
    )


def get_knowledge_store(
    agent_id: int,
    db: Annotated[Session, Depends(get_db)],
    qdrant_client: Annotated[QdrantClient, Depends(get_qdrant_client)],
    embedding_client: Annotated[EmbeddingClient, Depends(get_embedding_client)],
    reranker_client: Annotated[RerankerClient, Depends(get_reranker_client)],
    secret_store: Annotated[SecretStore, Depends(get_secret_store)],
) -> KnowledgeStore:
    """FastAPI dependency: the store for the `KnowledgeSearch` node's `source_ids`.

    `QdrantKnowledgeStore` and nothing else. There were briefly two retrievers
    answering this one question — a pgvector one that read the shelf off disk at
    search time, and this one — because two branches built a RAG stack in
    parallel and the merge kept both. One had to go, and the argument is not
    "the newer one": `source_ids` holds **Knowledge Base ids** (ADR-0043 §1,
    enforced by `src.workflow.knowledge_bases.ensure_knowledge_bases_owned`),
    which is what this store already takes and what the other one had to
    reinterpret as a directory name to use at all. The rest follows — hybrid
    dense+sparse over one index rather than BM25 recomputed per query, a
    cross-encoder over the fused list, `page_number` on a citation instead of a
    hardcoded `None`, and BYODB shelves reachable at all.

    Scoped to the *agent owner's* `owner_id` rather than the caller's, so a run
    can only ever see the Knowledge Bases of the creator who owns the agent —
    which is what makes `source_ids` safe to be creator-supplied text. The owner
    and not the caller: an end-user chatting with a published agent has to reach
    the shelves that agent was built on, and their own shelves have nothing to
    do with it.

    Resolved from the path's `agent_id`, which every route holding a `Knowledge`
    dependency has. Whether the caller is *entitled* to address that agent is
    settled before this runs, by `OwnedAgent` / `MeteredAgent` on the same route;
    this dependency answers a different question, and an agent that does not
    exist gets an owner id no Knowledge Base can ever match, so every source in
    a search raises `KnowledgeSourceError` naming it — the honest answer to a
    graph pointing at shelves that, from this store's perspective, cannot be
    reached — rather than a route-layer 404 duplicated here.

    Every collaborator is its own `Depends`, not called bare, so a test aims
    this at fakes through `app.dependency_overrides[get_qdrant_client]` /
    `get_embedding_client` / `get_reranker_client` without needing to override
    `get_knowledge_store` itself — the same reason `knowledge_base_routes.py`
    takes `QdrantClientDep` instead of calling `get_qdrant_client()` directly.
    """
    agent = db.get(Agent, agent_id)
    owner_id = agent.owner_id if agent is not None else _NO_SUCH_OWNER
    return QdrantKnowledgeStore(
        db=db,
        owner_id=owner_id,
        qdrant_client=qdrant_client,
        embedding_client=embedding_client,
        reranker_client=reranker_client,
        secret_store=secret_store,
    )


__all__ = [
    "DEFAULT_PASSAGE_LIMIT",
    "fused_points",
    "passages_of_points",
    "ranked_passages",
    "KnowledgeSourceError",
    "KnowledgeStore",
    "Passage",
    "QdrantKnowledgeStore",
    "get_knowledge_store",
]
