"""The one seam allowed to turn text into a vector — ADR-0045 §3.

Shaped like `src.services.llm.get_chat_model_factory` and for the same reason:
two call sites need embeddings — indexing a document and searching it — and if
each built its own client, a test would have to fake out two places or pay a
network round trip per assertion. One dependency, one override point.

**Two providers, chosen by the model's own name.** `text-embedding-*` is
OpenAI, `voyage-*` is Voyage, and `embedder_for` is the one place that decides.
Naming the provider in a second setting would let a deployment be configured
into a state where the two disagree — a Voyage key and an OpenAI model — and
that state has no sensible behaviour, so it is made unrepresentable instead.

Voyage is the preferred provider, with OpenAI kept as the configured fallback.
The provider is selected from the model name and the availability of its key;
this lets a local deployment use OpenAI immediately while a deployment with a
`VOYAGE_API_KEY` gets Voyage by default.

**Both are asked for 1024 dimensions.** Voyage's models emit that natively;
`text-embedding-3-*` emit 1536 or 3072 but support shortening (Matryoshka),
so the request names the width and the database column stays as it is.
Switching provider therefore needs no migration — only a re-index, which
`src.services.ingestion` does on a reingest when the model name changed.

**Asymmetry where the provider offers it.** Voyage embeds a *question* and a
*passage* into different points on purpose (`input_type`), because "hoàn tiền
mất bao lâu" and "Hoàn tiền được xử lý trong 7 ngày làm việc" are not the same
kind of text. OpenAI's embeddings are symmetric and have no such parameter, so
its implementation embeds both the same way — the protocol keeps the two
methods because one provider's distinction must not be lost to the other's
absence of it.

**Absence is a supported state.** No key configured means `get_embedder`
returns `None`, and `src.workflow.knowledge` serves lexical-only search. That
is what every test run and every checkout without an account does. What is
*not* supported is a key that silently stops working mid-run: an embedding
failure is raised, and the store turns it into one visible `tool_result` naming
the problem, rather than a search that answers from half its evidence.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

#: The width of every vector this platform stores, and therefore of the
#: `documents_kb` dense vector. A constant rather than a setting: it is
#: the shape of a database column, and a setting that disagrees with the schema
#: would fail at insert time with a message about a column, three layers from
#: the value that caused it. Every supported model is *asked* for this width —
#: Voyage's models emit it natively, OpenAI's are shortened to it — so changing
#: provider or model never needs a migration.
EMBEDDING_DIMENSIONS = 1024

#: How many texts go up in one call. Both providers accept far more; the ceiling
#: here is about failure size, not throughput — a batch that fails is a batch
#: that is retried, and re-embedding a whole handbook because of one 500 is a
#: bill nobody agreed to.
_BATCH_SIZE = 64

#: How a model name says which provider it belongs to. A prefix and not a
#: setting: the name is already unambiguous, and a second field naming the
#: provider could only ever disagree with it.
_OPENAI_PREFIX = "text-embedding-"
_VOYAGE_PREFIX = "voyage-"


class EmbeddingError(RuntimeError):
    """An embedding call that did not come back usable. Message is user-facing.

    A subclass of nothing in particular on purpose: `src.workflow.knowledge`
    catches it beside `KnowledgeSourceError` and reports both the same way — by
    name, on the run's timeline. What must never happen is that it is swallowed
    and the search proceeds on the lexical half alone, because that is a
    silently worse answer wearing a working search's name.
    """


class Embedder(Protocol):
    """Text in, vectors out, with the query/document distinction preserved."""

    @property
    def model(self) -> str:
        """Which model these vectors came from.

        Stored beside every chunk. Vectors from two models are not comparable —
        not even approximately — so this is what lets `ensure_indexed` notice
        that the configured model changed and re-embed instead of ranking one
        model's question against another model's passages.
        """
        ...

    @property
    def dimensions(self) -> int:
        """Length of every vector this embedder returns."""
        ...

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Vectors for passages being stored, in the order given."""
        ...

    def embed_query(self, text: str) -> tuple[float, ...]:
        """A vector for a question being asked."""
        ...


@dataclass(frozen=True, slots=True)
class VoyageEmbedder:
    """`Embedder` against the Voyage API.

    The client is imported inside the call rather than at module import, the
    same discipline `src.workflow.knowledge` applies to the PDF and DOCX
    extractors: a platform that will not start because an optional retrieval
    dependency is missing has turned a degraded search into an outage.
    """

    api_key: str
    model_name: str
    #: Where a call's token cost is written down. A callback rather than a
    #: database session, so this class stays a client for one API and knows
    #: nothing about tables — and so a test can count tokens without one.
    #: Defaults to discarding them, which is what a caller that has not opted
    #: into the ledger is asking for.
    record_usage: Callable[[str, int], None] = lambda _model, _tokens: None
    output_dimension: int = EMBEDDING_DIMENSIONS

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def dimensions(self) -> int:
        return self.output_dimension

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            vectors.extend(self._embed(texts[start : start + _BATCH_SIZE], input_type="document"))
        return tuple(vectors)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._embed([text], input_type="query")[0]

    def _embed(self, texts: Sequence[str], *, input_type: str) -> tuple[tuple[float, ...], ...]:
        import voyageai

        try:
            result = voyageai.Client(api_key=self.api_key).embed(
                list(texts),
                model=self.model_name,
                input_type=input_type,
                output_dimension=self.output_dimension,
            )
        except Exception as exc:  # noqa: BLE001 — provider errors are one message, not a stack trace
            raise EmbeddingError(f"Không tạo được vector cho tài liệu ({exc})") from None
        # Recorded before the vectors are even validated: Voyage has already
        # charged for this call, and a ledger that only counts calls whose
        # results we liked would under-report exactly when something is wrong.
        self.record_usage(self.model_name, int(getattr(result, "total_tokens", 0) or 0))
        vectors = tuple(tuple(float(value) for value in vector) for vector in result.embeddings)
        # Checked rather than trusted, because the failure it prevents is
        # silent: a vector of the wrong length is rejected by the database with
        # a message about a column, three layers from the setting that caused
        # it.
        if any(len(vector) != self.output_dimension for vector in vectors):
            raise EmbeddingError(
                f"Model {self.model_name!r} trả về vector khác {self.output_dimension} chiều — "
                "EMBEDDING_DIMENSIONS không khớp với model đang cấu hình"
            )
        return vectors


@dataclass(frozen=True, slots=True)
class OpenAIEmbedder:
    """`Embedder` against the OpenAI embeddings endpoint.

    Symmetric: OpenAI's embedding models have no query/document distinction, so
    both methods make the same call. That is not a shortcut — the distinction is
    a property of how a model was trained, and inventing one here (a prefix, a
    template) would be this platform guessing at the model's internals.

    `dimensions` is passed on every request. `text-embedding-3-*` are trained so
    that a prefix of the vector is still a usable vector, which is what lets a
    1536-dimension model fill a 1024-wide column with no loss worth the
    migration it would otherwise cost.
    """

    api_key: str
    model_name: str
    record_usage: Callable[[str, int], None] = lambda _model, _tokens: None
    output_dimension: int = EMBEDDING_DIMENSIONS

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def dimensions(self) -> int:
        return self.output_dimension

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            vectors.extend(self._embed(texts[start : start + _BATCH_SIZE]))
        return tuple(vectors)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._embed([text])[0]

    def _embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        from openai import OpenAI

        try:
            result = OpenAI(api_key=self.api_key).embeddings.create(
                input=list(texts), model=self.model_name, dimensions=self.output_dimension
            )
        except Exception as exc:  # noqa: BLE001 — provider errors are one message, not a stack trace
            raise EmbeddingError(f"Không tạo được vector cho tài liệu ({exc})") from None
        # Before validating, for the same reason as Voyage: the call has been
        # billed whether or not we liked what came back.
        self.record_usage(self.model_name, int(result.usage.total_tokens))
        # Sorted by `index` rather than trusted in arrival order — the API
        # documents that it may return them out of order, and a silently
        # permuted batch would attach every vector to the wrong chunk.
        vectors = tuple(
            tuple(float(value) for value in item.embedding) for item in sorted(result.data, key=lambda item: item.index)
        )
        if any(len(vector) != self.output_dimension for vector in vectors):
            raise EmbeddingError(
                f"Model {self.model_name!r} trả về vector khác {self.output_dimension} chiều — "
                "EMBEDDING_DIMENSIONS không khớp với model đang cấu hình"
            )
        return vectors


def embedder_for(
    model: str, *, openai_api_key: str, voyage_api_key: str, record_usage: Callable[[str, int], None]
) -> Embedder | None:
    """The embedder for `model`, or `None` if its provider has no key configured.

    The one place a model name is turned into a provider. `None` rather than an
    exception, because "no key for this provider" is the same supported state as
    "no key at all" — search falls back to keyword ranking — and a deployment
    that has configured neither should start normally rather than refuse to.
    """
    if model.startswith(_OPENAI_PREFIX):
        return (
            OpenAIEmbedder(api_key=openai_api_key, model_name=model, record_usage=record_usage)
            if openai_api_key
            else None
        )
    if model.startswith(_VOYAGE_PREFIX):
        return (
            VoyageEmbedder(api_key=voyage_api_key, model_name=model, record_usage=record_usage)
            if voyage_api_key
            else None
        )
    return None


#: Where the embedder is actually built: `src.workflow.embedding_budget`, which
#: is the module that knows which model still has allowance left. It lives there
#: and not here because *this* module deliberately owns no database — it is a
#: client for two APIs, and a client that reads a ledger to decide what to send
#: is two things wearing one name. (It could not live here anyway:
#: `src.db.models` imports `EMBEDDING_DIMENSIONS` from this file, so importing
#: the ledger back would be a cycle — the layering and the import graph happen
#: to agree, which is usually a sign the layering is right.)
