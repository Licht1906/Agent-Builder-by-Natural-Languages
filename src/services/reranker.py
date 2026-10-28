"""The backend's client for the rerank service (RAG-03 / ADR-0044 §2, §3).

Same injectable-callable shape as `src.services.embedding.get_embedding_client`
— every call site reaches the reranker only through the callable this
dependency returns, so tests replace it without a live service.

The failure mode is deliberately the opposite of `embedding`'s, though.
Embedding failures are fatal — a corrupted vector poisons the index — so that
client retries and then raises. Reranking is a quality improvement over
candidates a retriever already found by RRF (ADR-0044 §3); losing it should
degrade a result's ordering, not the request. So `rerank` never raises: a
dead or slow reranker logs a warning and returns the candidates in their
original order, `reranked=False`, and the caller decides what if anything to
surface about that in the response.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankResult:
    ranked_indices: list[int]
    scores: list[float] | None
    reranked: bool


RerankerClient = Callable[[str, Sequence[str]], RerankResult]


def _parsed_ranking(body: object, document_count: int) -> tuple[list[int], list[float]]:
    """Read a `/rerank` body, or raise if it cannot be trusted.

    Callers index their candidate list by `ranked_indices`, so an index this
    function lets through unchecked raises `IndexError` at the call site
    instead — the same 500 this module exists to prevent, only surfacing
    somewhere with no reranker in the traceback. A ranking is trustworthy
    only if it is a permutation of the documents that were sent: every
    candidate ranked, each exactly once.

    Raises `ValueError` for a body it can read but not believe, `KeyError`
    for one missing the fields entirely, and `TypeError` for one whose
    fields are the wrong type to sort or measure. `_rerank` treats all three
    the same way, so the distinction is for whoever reads the log line.
    """
    if not isinstance(body, dict):
        raise ValueError(f"expected a JSON object, got {type(body).__name__}")

    ranked_indices = body["ranked_indices"]
    scores = body["scores"]
    if sorted(ranked_indices) != list(range(document_count)):
        raise ValueError(f"ranked_indices is not a permutation of the {document_count} documents sent")
    if len(scores) != document_count:
        raise ValueError(f"got {len(scores)} scores for {document_count} documents")
    return list(ranked_indices), [float(score) for score in scores]


def _rerank(query: str, documents: Sequence[str]) -> RerankResult:
    settings = get_settings()
    return rerank_at(settings.reranker_url, timeout=settings.reranker_timeout_seconds)(query, documents)


def rerank_at(url: str, *, timeout: float, transport: httpx.BaseTransport | None = None) -> RerankerClient:
    """A `RerankerClient` for the rerank service at `url` — the platform's, or an exported package's own (DS-18).

    `transport` is a test seam. Same never-raise contract as the module says.
    """

    def rerank(query: str, documents: Sequence[str]) -> RerankResult:
        if not documents:
            return RerankResult(ranked_indices=[], scores=[], reranked=True)
        try:
            with httpx.Client(timeout=timeout, transport=transport) as client:
                response = client.post(f"{url.rstrip('/')}/rerank", json={"query": query, "documents": list(documents)})
            response.raise_for_status()
            ranked_indices, scores = _parsed_ranking(response.json(), len(documents))
            return RerankResult(ranked_indices=ranked_indices, scores=scores, reranked=True)
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError, TypeError, KeyError) as exc:
            # ValueError covers a non-JSON 200 body and everything
            # `_parsed_ranking` rejects; KeyError a body missing
            # ranked_indices/scores; TypeError a body whose fields are the wrong
            # type to sort or measure. A misbehaving reranker is the same "must
            # not 500" case as a dead one, not just a connection drop.
            logger.warning("Reranker unusable (%s) — falling back to unranked order", exc)
            return RerankResult(
                ranked_indices=list(range(len(documents))),
                scores=None,
                reranked=False,
            )

    return rerank


def get_reranker_client() -> RerankerClient:
    """FastAPI dependency: the only function in the system allowed to call the reranker."""
    return _rerank
