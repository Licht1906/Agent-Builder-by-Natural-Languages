"""`VectorDBAdapter`: one contract `QdrantKnowledgeStore` queries through, whichever database backs a shelf — RAG-21 / #107.

ADR-0044 §1 already predicted this module's existence: *"a 'connect your own
Qdrant' adapter is the internal adapter with a different client."* RAG-20
(#106) built the row a BYODB shelf points at (`ExternalConnection`) and the
SSRF-guarded dial-out `/test` uses to prove it reachable
(`src.services.connection_probe`); this module is what a *search*, not a
connectivity check, does with that same row.

**One method, dense-only.** Hybrid (dense+sparse, RRF-fused — RAG-10 /
ADR-0044 §3) is a Qdrant superpower: sparse-vector support and native fusion
that a creator's own Pinecone/Weaviate/pgvector collection does not share and
this platform did not ingest into. Folding it into `VectorDBAdapter.search`
would mean every other provider either fakes a sparse half it does not have
or the whole interface narrows to what none of them share, which is the same
thing this narrower, honest interface already is. So the internal managed
path stays exactly what RAG-10 built — `QdrantKnowledgeStore._fused_candidates`
talks to `qdrant_client` directly, unchanged — and this module is reached
only for a BYODB shelf, which the ticket's own acceptance line says outright:
*"KB byodb chạy dense-only và nói ra điều đó."* `QdrantKnowledgeStore` is what
says so, with a `logger.warning` per search that touches one, not this
module — an adapter reports what it found, not what kind of search it was.

**Every dial-out goes through `src.services.ssrf_guard`, on a shorter clock.**
`/test` already proved a BYODB address safe once; a search reusing that same
address still re-resolves and re-pins it for the reason `pin_safe_ip`'s own
docstring gives `connection_probe` — a rebind between then and now is a
question this module answers fresh, not one it trusts an old verdict for.
`EXTERNAL_SEARCH_TIMEOUT_SECONDS` sits below `tool_http_timeout_seconds`
deliberately (the ticket's own "Timeout ngắn hơn cho gọi ra ngoài"): a BYODB
search runs inside a `KnowledgeSearch` node's turn, and a slow creator-owned
database must read as one clearly-failed source, not as the whole turn
hanging.

**`field_mapping` is interpreted here, applied by the caller.** `SearchHit.payload`
carries whatever field names the target collection actually uses — this
module does not know which one is the text, only what a point came back
holding. `mapped_field`/`DEFAULT_FIELD_NAMES` are the shared vocabulary: a
`chunk_text` field by default, or whatever `ExternalConnection.field_mapping`
(RAG-20) says to read instead when a creator's own schema differs — the
ticket's own `{'text_field': 'content'}` example, made literal.

**Pinecone and Weaviate exist as rows, not as query drivers, and pgvector
alongside them.** `src.db.models.ExternalConnectionProvider`'s own docstring
already named this module's exception: `ProviderNotSupportedError`, raised
rather than a guess, the same discipline `probe_connection` already gives
`/test` for the same three providers — nobody in this build has an account
for either service to prove a real driver against, and a guessed
implementation is worse than an honest refusal (the ticket's own "Thứ tự cắt
nội bộ": Pinecone and Weaviate are the first thing to drop when time is
short; pgvector has no client dependency in this project yet either, so it
gets the same honest refusal rather than a half-built raw-socket path).
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.db.models import ExternalConnection, ExternalConnectionProvider
from src.services.ssrf_guard import SSRFBlockedError, assert_safe_url, pin_safe_ip, url_with_pinned_host

#: Shorter than `tool_http_timeout_seconds` — see the module docstring's
#: "Timeout ngắn hơn cho gọi ra ngoài".
EXTERNAL_SEARCH_TIMEOUT_SECONDS = 5.0

#: The logical field names a `Passage` needs, and the payload key each reads
#: from when `ExternalConnection.field_mapping` (RAG-20) does not override it.
#: Matches the internal schema's own names (`src.services.qdrant.PointPayload`)
#: exactly, since that is the shape most BYODB collections a creator points at
#: without any remapping already have no reason to differ from.
DEFAULT_FIELD_NAMES: Mapping[str, str] = {
    "text_field": "chunk_text",
    "doc_name_field": "doc_name",
    "section_path_field": "section_path",
    "page_number_field": "page_number",
}


def mapped_field(payload: Mapping[str, Any], field_mapping: Mapping[str, str], logical_name: str) -> Any:
    """The value of `logical_name` out of `payload` — `field_mapping`'s remap if set, `DEFAULT_FIELD_NAMES` otherwise.

    `None` when the resolved key is not in `payload` at all, which is an
    honest "this hit has no such field" rather than a `KeyError` — a creator's
    collection legitimately has no `section_path`/`page_number` concept, the
    same shape `Passage` already allows for an internally indexed source.
    """
    key = field_mapping.get(logical_name, DEFAULT_FIELD_NAMES[logical_name])
    return payload.get(key)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One candidate a `VectorDBAdapter` found — the raw payload, unmapped.

    Turning `payload` into the names a `Passage` needs is `mapped_field`'s
    job, applied by the caller (`QdrantKnowledgeStore`): this dataclass does
    not know which field *should* be the text, only what a point came back
    holding, the same separation `src.services.connection_probe.ProbeOutcome`
    draws between "what happened" and "what the caller does about it".
    """

    id: str
    score: float
    payload: Mapping[str, Any]


class VectorDBAdapterError(RuntimeError):
    """A search could not be completed — timeout, connection refused, an unreadable response.

    One exception type across every provider, so `QdrantKnowledgeStore` has
    one `except` to write rather than one per adapter — the same discipline
    `ProbeConnectionError` gives `probe_connection`. Always carries a message
    fit to become a `KnowledgeSourceError` naming the Knowledge Base, never a
    raw client exception a caller would have to translate itself.
    """


class ProviderNotSupportedError(VectorDBAdapterError):
    """`provider` has no query driver in this build — see the module docstring."""


class VectorDBAdapter(ABC):
    """Where a `KnowledgeSearch` node's BYODB candidates come from, whichever database backs the shelf.

    One method, dense-only — see the module docstring for why hybrid does not
    belong here. `filters` is the ticket's own interface parameter
    (provider-defined key/value equality); no caller passes it today, since
    one `ExternalConnection` names one collection wholly dedicated to one
    creator's own data, so nothing in this build needs to scope a BYODB
    search any further than the connection already does. Kept on the
    contract rather than dropped, matching the interface the ticket
    specifies verbatim.
    """

    @abstractmethod
    def search(
        self, query_vector: Sequence[float], *, top_k: int, filters: Mapping[str, str] | None = None
    ) -> list[SearchHit]:
        """The `top_k` candidates nearest `query_vector`, best first.

        Raises `VectorDBAdapterError` (or the `ProviderNotSupportedError`
        subclass) rather than let a provider's own client exception, a hang,
        or a crash reach the caller — the ticket's own "lỗi rõ ràng trả cho
        agent thay vì treo".
        """
        ...


#: `(url, headers, json_body) -> httpx.Response`. The same DI seam
#: `src.services.connection_probe.HttpGet` gives `/test`, widened to a POST
#: body — a query is a POST with a vector in it, `/test` is a bare GET, and
#: nothing in production ever needs the two to share one signature.
HttpPost = Callable[[str, dict[str, str] | None, dict[str, Any]], httpx.Response]


def http_post_via_httpx(url: str, headers: dict[str, str] | None, json_body: dict[str, Any]) -> httpx.Response:
    """The real transport: one POST, no redirects ever followed, dialled to a DNS-pinned address.

    Mirrors `src.services.connection_probe.http_get_via_httpx` exactly —
    `pin_safe_ip` resolves and validates once, `url_with_pinned_host` connects
    to that literal address rather than asking DNS again, and `sni_hostname`
    plus the `Host` header keep TLS pointed at the real hostname. The
    difference is only the verb and the timeout:
    `EXTERNAL_SEARCH_TIMEOUT_SECONDS`, not `tool_http_timeout_seconds` — see
    the module docstring.
    """
    host, ip = pin_safe_ip(url)
    pinned_url = url_with_pinned_host(url, ip)
    merged_headers = {**(headers or {}), "Host": host}
    with httpx.Client(timeout=EXTERNAL_SEARCH_TIMEOUT_SECONDS, follow_redirects=False) as client:
        return client.post(pinned_url, headers=merged_headers, json=json_body, extensions={"sni_hostname": host})


@dataclass(frozen=True, slots=True)
class QdrantExternalAdapter(VectorDBAdapter):
    """Dense-only search against a creator's own Qdrant, over its REST Query API — RAG-21's `QdrantExternalAdapter`.

    Raw REST rather than `qdrant_client`, for `connection_probe`'s own reason:
    this module, not an SDK's defaults, decides whether a redirect is
    followed, and `http_post_via_httpx`'s DNS pinning has to run underneath
    whatever HTTP call actually leaves the process.

    `field_mapping["vector_field"]`, when a creator sets it, is passed as the
    Query API's `using` — a collection with a *named* vector (this platform's
    own shape) needs to be told which one to search; the common case, an
    unnamed default vector, needs nothing extra and is what a request with no
    `using` already asks for.

    `http_post` is a construction-time collaborator, not a `search()`
    parameter — every `VectorDBAdapter.search` call stays the ABC's own
    three-argument shape this way, so a caller (`QdrantKnowledgeStore`) never
    has to know which concrete adapter it is holding to inject a transport
    into it. Defaults to the real, SSRF-guarded one; a test overrides it at
    `adapter_for_connection(..., http_post=...)`.
    """

    connection: ExternalConnection
    secret: str | None
    http_post: HttpPost = field(default=http_post_via_httpx)

    def search(
        self, query_vector: Sequence[float], *, top_k: int, filters: Mapping[str, str] | None = None
    ) -> list[SearchHit]:
        try:
            assert_safe_url(self.connection.connection_url)
        except SSRFBlockedError as exc:
            raise VectorDBAdapterError(str(exc)) from exc

        body: dict[str, Any] = {"query": list(query_vector), "limit": top_k, "with_payload": True}
        vector_field = self.connection.field_mapping.get("vector_field")
        if vector_field:
            body["using"] = vector_field
        if filters:
            body["filter"] = {
                "must": [{"key": key, "match": {"value": value}} for key, value in filters.items()]
            }

        url = f"{self.connection.connection_url.rstrip('/')}/collections/{self.connection.collection_name}/points/query"
        headers = {"api-key": self.secret} if self.secret else None
        try:
            response = self.http_post(url, headers, body)
        except (httpx.HTTPError, SSRFBlockedError) as exc:
            # `SSRFBlockedError` here means the real transport's own, fresher
            # resolution disagreed with the `assert_safe_url` check above — a
            # live rebind caught in the act, `connection_probe`'s same case.
            raise VectorDBAdapterError(
                f"Không kết nối được tới {self.connection.connection_url!r}: {exc}"
            ) from exc

        if 300 <= response.status_code < 400:
            raise VectorDBAdapterError(
                f"{self.connection.connection_url!r} trả về redirect — bị từ chối để chống SSRF qua redirect"
            )
        if response.status_code >= 400:
            raise VectorDBAdapterError(f"Máy chủ trả lỗi HTTP {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
            points = data["result"]["points"]
        except (ValueError, KeyError, TypeError) as exc:
            raise VectorDBAdapterError(
                f"Không đọc được kết quả truy vấn từ {self.connection.connection_url!r}"
            ) from exc

        return [
            SearchHit(id=str(point.get("id")), score=float(point.get("score") or 0.0), payload=point.get("payload") or {})
            for point in points
        ]


@dataclass(frozen=True, slots=True)
class _UnsupportedProviderAdapter(VectorDBAdapter):
    """Shared body for a provider that has a row and a `/test` verdict, but no query driver — see the module docstring."""

    connection: ExternalConnection

    def search(
        self, query_vector: Sequence[float], *, top_k: int, filters: Mapping[str, str] | None = None
    ) -> list[SearchHit]:
        raise ProviderNotSupportedError(
            f"Provider {self.connection.provider.value!r} chưa có driver truy vấn — "
            "hiện chỉ 'qdrant' hỗ trợ tìm kiếm BYODB trong bản này"
        )


class PineconeAdapter(_UnsupportedProviderAdapter):
    """Pinecone — a row and an honest refusal to search, per the ticket's own cut order."""


class WeaviateAdapter(_UnsupportedProviderAdapter):
    """Weaviate — a row and an honest refusal to search, per the ticket's own cut order."""


class PgvectorAdapter(_UnsupportedProviderAdapter):
    """pgvector — a row and an honest refusal to search; no client dependency in this project yet."""


def adapter_for_connection(
    connection: ExternalConnection, *, secret: str | None, http_post: HttpPost = http_post_via_httpx
) -> VectorDBAdapter:
    """The `VectorDBAdapter` for `connection.provider` — RAG-21's "Store chọn adapter... byodb → dựng adapter từ external_connections".

    `secret` is resolved by the caller (`QdrantKnowledgeStore`, through
    `SecretStore`) and passed in already-plaintext — the same boundary
    `run_connection_test` draws for `/test`, so this module never itself
    reads a `secret_ref`. `http_post` only matters to `QdrantExternalAdapter`
    — the stub adapters below never dial out at all — but it is accepted
    here uniformly rather than the caller having to know which provider
    cares, the same reason `secret` is passed to every branch even though
    the stubs ignore it too.
    """
    if connection.provider is ExternalConnectionProvider.QDRANT:
        return QdrantExternalAdapter(connection=connection, secret=secret, http_post=http_post)
    if connection.provider is ExternalConnectionProvider.PINECONE:
        return PineconeAdapter(connection=connection)
    if connection.provider is ExternalConnectionProvider.WEAVIATE:
        return WeaviateAdapter(connection=connection)
    return PgvectorAdapter(connection=connection)
