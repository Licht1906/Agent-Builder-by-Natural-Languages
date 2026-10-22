"""The one seam allowed to call the embedding model (RAG-02 / ADR-0044 §2).

Vectors come from the same two cloud providers the rest of the platform uses —
Voyage first, OpenAI as the configured fallback — through
`src.workflow.embeddings.embedder_for`, chosen by the model's own name. Every
call site that needs a vector goes through `get_embedding_client`, a FastAPI
dependency, the same shape as `src.services.llm.get_chat_model_factory` for the
same reason: overriding `app.dependency_overrides[get_embedding_client]` is what
lets the ingest and retrieval test suites run without touching a provider.

**This used to be Ollama, and the move away was not a preference.** `bge-m3`
ran in the operator's own Ollama over `POST /api/embed`, which is free and
private and works on a laptop — and is unreachable from Cloud Run, where
`localhost:11434` answers nothing. Ingestion did not degrade there, it failed
per document: `httpx.ConnectError` per batch, `failed (0 passages)` per file,
and a knowledge base that exists and is empty. The platform already had a
hosted embedder with a token ledger behind it (ADR-0045 §3); this module now
uses that one instead of keeping a second, differently-shaped path to a
different provider.

**One selection policy, not two.** The model is picked by
`src.workflow.embedding_budget.get_embedder` — the same function
`src.workflow.knowledge` asks — so indexing and searching can never disagree
about which model is current. They must not: vectors from two models are not
comparable, not even approximately, so a passage indexed by one model and a
question embedded by another rank against each other as noise. That is exactly
what the two-stack arrangement produced, because both stacks read the *same*
`EMBEDDING_MODEL` setting and meant different things by it.

`embed` is a real batch, not a loop of single-text calls — ingesting one long
document produces hundreds of chunks, and hundreds of round-trips is the
difference between an ingest that finishes and one that times out. The batch
ceiling itself lives in `src.workflow.embeddings`, which is the module that
knows what each provider accepts.

**No embedder configured is an error here, not a fallback.** Search degrades to
lexical ranking when `get_embedder` returns `None` (ADR-0045 §3), but ingestion
has nothing to degrade *to*: a passage with no vector cannot be upserted into a
collection whose dense vector is not optional. Failing loudly at the seam is
what puts `EMBEDDING_MODEL`/`VOYAGE_API_KEY` in the log line, instead of a
document that reports `INDEXED` and never matches a query.
"""

from collections.abc import Callable, Sequence
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from src.db.base import get_db
from src.workflow.embedding_budget import get_embedder
from src.workflow.embeddings import EMBEDDING_DIMENSIONS

EmbeddingClient = Callable[[Sequence[str]], list[list[float]]]

#: Matches `src.services.qdrant.DENSE_VECTOR_SIZE` — the collection is built for
#: exactly this width, and a mismatch caught here is a clear startup error
#: instead of a corrupted collection discovered at upsert time.
#:
#: Re-exported from `src.workflow.embeddings` rather than written down again, so
#: the width of a stored vector is stated in exactly one place across both
#: retrieval stacks. The previous copy was an independent `1024` that happened
#: to agree, and this file has already paid once for a constant defined twice:
#: `embedding_model` was declared in two places in `src.config`, the second
#: silently won, and the fallback list the first one documented was dead code
#: nobody could see.
EXPECTED_DIMENSION = EMBEDDING_DIMENSIONS


class EmbeddingDimensionError(RuntimeError):
    """The configured model returned vectors of a width this platform did not provision for."""


class EmbeddingUnavailableError(RuntimeError):
    """No embedding provider is usable, so ingestion cannot produce vectors.

    Raised rather than returning empty vectors. `get_embedder` answers `None` for
    the *search* path, where it is a supported state — the store falls back to
    keyword ranking and says so. Indexing has no such fallback: a document
    written with no vector is a document that exists, reports success, and can
    never be found. That is the failure mode this platform keeps meeting, so
    this path refuses instead.

    Separate from `EmbeddingDimensionError` because the operator action is
    different: this one is answered by configuring a key or a model, not by
    correcting a width. Message is user-facing — it reaches the upload screen
    through `ingest_document`'s failure path.
    """


def client_for(db: Session) -> EmbeddingClient:
    """The embedding callable for a caller that already holds a session.

    Exists beside the FastAPI dependency because `ingest_document` runs
    detached from any request, on a session it opened itself — it cannot ask
    FastAPI to resolve `get_embedder`, but it must consult the same token
    ledger the request path does, or a background ingest would go on spending
    against an allowance that has already retired the model. Cloud Run Jobs and
    seed scripts reach the embedder the same way, through their own session.
    """
    embedder = get_embedder(db)
    if embedder is None:
        raise EmbeddingUnavailableError(
            "Chưa cấu hình được provider embedding nào — kiểm tra EMBEDDING_MODEL "
            "cùng VOYAGE_API_KEY/OPENAI_API_KEY, hoặc model đã hết hạn mức "
            "EMBEDDING_TOKEN_ALLOWANCE. Ingest không thể chạy tiếp vì một passage "
            "không có vector thì không ghi vào Qdrant được."
        )

    def _embed(texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        # `embed_documents` and not `embed_query`: these are passages being
        # stored. Voyage embeds the two into different points on purpose, and
        # using the query side here would put every stored chunk on the wrong
        # side of that distinction.
        vectors = embedder.embed_documents(list(texts))

        if len(vectors) != len(texts):
            raise EmbeddingDimensionError(
                f"Gửi {len(texts)} đoạn văn bản nhưng nhận về {len(vectors)} vector — "
                "kết quả lệch vị trí sẽ gán nhầm embedding cho chunk, phải coi là lỗi ngay tại đây."
            )
        # Width is already validated inside each `Embedder`, which is where the
        # provider's own response is in scope. Re-checked here anyway because
        # this is the seam that feeds Qdrant: the collection's dense vector is
        # fixed-width, and an override installed by a test is not covered by
        # the provider's check at all.
        for vector in vectors:
            if len(vector) != EXPECTED_DIMENSION:
                raise EmbeddingDimensionError(
                    f"Model embedding '{embedder.model}' trả về vector {len(vector)} chiều, "
                    f"nhưng collection Qdrant được tạo cho {EXPECTED_DIMENSION} chiều — "
                    "kiểm tra lại EMBEDDING_MODEL, dùng sai model ở đây sẽ làm hỏng collection."
                )

        return [list(vector) for vector in vectors]

    return _embed


def get_embedding_client(db: Annotated[Session, Depends(get_db)]) -> EmbeddingClient:
    """FastAPI dependency: the only function in the system allowed to call the embedding model."""
    return client_for(db)
