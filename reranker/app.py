"""Rerank service (RAG-03 / ADR-0044 §2).

`bge-reranker-v2-m3` is a cross-encoder, not an embedding model, and neither
embedding provider this platform uses (Voyage, OpenAI) exposes a rerank
endpoint for it. So this is a service of its own: a thin FastAPI wrapper around
`sentence-transformers`' `CrossEncoder`, and the one part of the retrieval
stack that still runs locally.

`create_app` takes a `scorer_factory` rather than importing `torch` and
`sentence_transformers` at module scope, so the module can be imported —
and its ranking/response-assembly logic exercised — without either
dependency installed. Production's module-level `app` uses the real
factory; tests inject a fake one.

Device selection is `cuda` when available, `cpu` otherwise (§2's stated
policy) — never forced, because a `deploy.resources.devices` requirement in
compose would turn this demo into "only runs on one machine".
"""

import logging
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from typing import TypedDict

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: A scorer takes (query, document) pairs and returns one relevance score per
#: pair, in the same order. The only shape `rank` depends on — everything
#: about how the score is produced (model, device, batching) lives behind it.
Scorer = Callable[[Sequence[tuple[str, str]]], list[float]]


class _ModelState(TypedDict):
    scorer: Scorer | None
    device: str | None


class RerankRequest(BaseModel):
    query: str
    documents: list[str]


class RerankResponse(BaseModel):
    scores: list[float]
    ranked_indices: list[int]


def select_device() -> str:
    import torch

    return "cuda" if torch.cuda.is_available() else "cpu"


def load_scorer() -> tuple[Scorer, str]:
    """The real scorer: `bge-reranker-v2-m3` behind `sentence_transformers.CrossEncoder`."""
    from sentence_transformers import CrossEncoder

    device = select_device()
    model = CrossEncoder("BAAI/bge-reranker-v2-m3", device=device)

    def score(pairs: Sequence[tuple[str, str]]) -> list[float]:
        return [float(s) for s in model.predict(list(pairs))]

    return score, device


def rank(query: str, documents: Sequence[str], scorer: Scorer) -> RerankResponse:
    if not documents:
        return RerankResponse(scores=[], ranked_indices=[])

    pairs = [(query, document) for document in documents]
    scores = scorer(pairs)
    ranked_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return RerankResponse(scores=scores, ranked_indices=ranked_indices)


def create_app(scorer_factory: Callable[[], tuple[Scorer, str]] = load_scorer) -> FastAPI:
    state: _ModelState = {"scorer": None, "device": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scorer, device = scorer_factory()
        state["scorer"] = scorer
        state["device"] = device
        logger.info("Reranker model loaded on device=%s", device)
        yield

    app = FastAPI(lifespan=lifespan)

    def get_scorer() -> Scorer:
        scorer = state["scorer"]
        if scorer is None:
            raise HTTPException(status_code=503, detail="model not loaded")
        return scorer

    @app.get("/health")
    def health() -> JSONResponse:
        """Reports whether the model has finished loading, not just that the process is up."""
        loaded = state["scorer"] is not None
        return JSONResponse(
            status_code=200 if loaded else 503,
            content={"model_loaded": loaded, "device": state["device"]},
        )

    @app.post("/rerank", response_model=RerankResponse)
    def rerank_endpoint(payload: RerankRequest, scorer: Scorer = Depends(get_scorer)) -> RerankResponse:
        return rank(payload.query, payload.documents, scorer)

    return app


app = create_app()
