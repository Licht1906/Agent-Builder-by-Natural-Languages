"""`POST /external-connections/{id}/test` — a real ping at a creator's own vector database, RAG-20 / #106.

**Never raises.** The ticket's own acceptance line is "Connection string sai
→ lỗi rõ ràng, không crash": every way this can go wrong — SSRF-blocked,
unreachable, wrong dimension, a provider with no prober — comes back as a
`ProbeOutcome` the caller writes straight onto the row's `status` /
`status_detail`, never an exception the route has to turn into one more
special case.

**Every dial-out goes through `src.services.ssrf_guard` first**, and only
`qdrant` has a prober behind it — `qdrant-client` is the one vector-database
dependency this platform already carries (ADR-0044 §1); the raw REST call
here rather than that client is what keeps redirect-following under this
module's own control instead of an SDK's, which is exactly the surface the
ticket calls out ("Chặn redirect tới địa chỉ nội bộ").

`http_get` is the seam, the same discipline `McpTransport` is for
`src.workflow.mcp`: a test scripts a vector database by passing a function,
and nothing in production needs a flag saying "pretend".
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from src.config import get_settings
from src.db.models import ExternalConnection, ExternalConnectionProvider, ExternalConnectionStatus
from src.services.ssrf_guard import SSRFBlockedError, assert_safe_url, pin_safe_ip, url_with_pinned_host

#: `(url, headers) -> httpx.Response`. Two positional members and not a full
#: request object, because that is everything a probe ever varies: the
#: address it dials and, when a connection carries a credential, the header
#: that carries it. `follow_redirects` is not a parameter a caller can turn
#: on — see `_get_qdrant_collection` for why the redirect check happens on
#: the response this returns rather than being delegated to it.
HttpGet = Callable[[str, dict[str, str] | None], httpx.Response]


class ProbeFailureReason(StrEnum):
    """Which *kind* of failure a probe hit, apart from the sentence describing it — RAG-22 / #108.

    `detail` is written for an operator reading a row: it quotes the URL, an
    httpx exception's own words, the first 200 bytes a server answered with.
    None of that is what RAG-22's connection form promises a creator ("lý do
    lỗi **dễ hiểu**, không phải raw error"), and a screen that reworded
    `detail` by matching on its prose would be a second parser for a sentence
    this module already knows the parts of. So the kind travels as a value,
    and the wording of the advice ("kiểm tra lại API key", "tên collection
    có đúng không") belongs to whoever is doing the telling.

    `BLOCKED_ADDRESS` is kept apart from `UNREACHABLE` deliberately: both
    mean no answer came back, but only one of them is this platform refusing
    an address on purpose, and telling a creator to "check the server is
    running" when the real answer is "that address is not allowed" sends
    them to debug the wrong machine.
    """

    PROVIDER_UNSUPPORTED = "provider_unsupported"
    BLOCKED_ADDRESS = "blocked_address"
    UNREACHABLE = "unreachable"
    REDIRECT_REFUSED = "redirect_refused"
    AUTH_REJECTED = "auth_rejected"
    COLLECTION_NOT_FOUND = "collection_not_found"
    SERVER_ERROR = "server_error"
    UNREADABLE_ANSWER = "unreadable_answer"
    DIMENSION_MISMATCH = "dimension_mismatch"
    #: The two ways a credential fails before anything is dialled. Written by
    #: `src.workflow.external_connections.run_connection_test`, never by
    #: `probe_connection` — which by then has already been handed a resolved
    #: secret or none. Members of this enum all the same, because a creator
    #: reading "why did my test fail" makes no distinction between the
    #: platform not finding their key and the far end rejecting it.
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_UNREADABLE = "credential_unreadable"


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """What one `/test` call decided — written straight onto `ExternalConnection.status`/`status_detail`/`status_reason`."""

    status: ExternalConnectionStatus
    detail: str
    reason: ProbeFailureReason | None = None
    """Which kind of failure this was; `None` on a connected outcome, which has no kind."""


class ProbeConnectionError(RuntimeError):
    """The target could not be reached, or answered with something a probe cannot use.

    Caught in exactly one place, `probe_connection` itself — every prober
    raises this instead of returning a `ProbeOutcome` on its own, so there is
    one place, not one per provider, that turns "something went wrong" into
    the `failed` outcome the ticket promises never crashes the request.

    Carries `reason` for the same purpose `ProbeOutcome` does: the raiser is
    the only code that knows *which* way this went wrong, and re-deriving it
    from the message at the catch site would be guessing at what it just
    finished knowing.
    """

    def __init__(self, message: str, reason: ProbeFailureReason) -> None:
        super().__init__(message)
        self.reason = reason


def http_get_via_httpx(url: str, headers: dict[str, str] | None) -> httpx.Response:
    """The real transport: one GET, no redirects ever followed, dialled to a DNS-pinned address.

    `pin_safe_ip` resolves and validates `url`'s host once and hands back the
    literal address that passed. This then connects to *that* address
    directly instead of letting `httpx` resolve the hostname a second time —
    the "checked one answer, connected to whatever the next lookup says" gap
    a low-TTL DNS rebind is built to walk through (the ticket's own
    "`evil.com` trỏ về `127.0.0.1`" line). `extensions={"sni_hostname": ...}`
    plus the `Host` header keep TLS SNI and certificate verification checking
    the real hostname, so pinning the connection does not also break HTTPS
    against a normally-configured server.

    `follow_redirects=False` is not a default being left alone — it is the
    ticket's "Chặn redirect tới địa chỉ nội bộ" enforced at the one call site
    that leaves this process. A redirect response comes back as an ordinary
    3xx `httpx.Response` for `_get_qdrant_collection_dimension` to refuse,
    rather than being silently chased somewhere this pinning never saw.

    `httpx.Client`, not the module-level `httpx.get` shortcut: only the
    `Client` methods accept `extensions=`, which is how `sni_hostname` above
    actually reaches httpcore's connection layer.
    """
    host, ip = pin_safe_ip(url)
    pinned_url = url_with_pinned_host(url, ip)
    merged_headers = {**(headers or {}), "Host": host}
    with httpx.Client(timeout=get_settings().tool_http_timeout_seconds, follow_redirects=False) as client:
        return client.get(pinned_url, headers=merged_headers, extensions={"sni_hostname": host})


def get_connection_http_get() -> HttpGet:
    """FastAPI dependency: how a request reaches a creator's vector database.

    Declared the way `get_mcp_transport`/`get_secret_store` are — a test aims
    `/external-connections/{id}/test` at a fake by overriding this, and the
    real, redirect-refusing transport is the default rather than a branch on
    a flag.
    """
    return http_get_via_httpx


def probe_connection(
    connection: ExternalConnection, *, secret: str | None, http_get: HttpGet = http_get_via_httpx
) -> ProbeOutcome:
    """Test `connection` for real. Always returns; never raises.

    Provider support is checked before anything is dialled — an unsupported
    provider is refused by name, not discovered by trying and failing to
    reach it. Everything past that point goes through `assert_safe_url`
    before a single byte leaves this process, whatever the provider.
    """
    if connection.provider is not ExternalConnectionProvider.QDRANT:
        return ProbeOutcome(
            status=ExternalConnectionStatus.FAILED,
            detail=(
                f"Provider {connection.provider.value!r} chưa được hỗ trợ kiểm tra kết nối tự động — "
                "hiện chỉ 'qdrant' có driver kiểm tra trong bản này"
            ),
            reason=ProbeFailureReason.PROVIDER_UNSUPPORTED,
        )

    try:
        assert_safe_url(connection.connection_url)
    except SSRFBlockedError as exc:
        return ProbeOutcome(
            status=ExternalConnectionStatus.FAILED, detail=str(exc), reason=ProbeFailureReason.BLOCKED_ADDRESS
        )

    try:
        dimension = _get_qdrant_collection_dimension(connection, secret=secret, http_get=http_get)
    except ProbeConnectionError as exc:
        return ProbeOutcome(status=ExternalConnectionStatus.FAILED, detail=str(exc), reason=exc.reason)

    if dimension != connection.embedding_dimension:
        return ProbeOutcome(
            status=ExternalConnectionStatus.FAILED,
            detail=(
                f"embedding_dimension khai là {connection.embedding_dimension} nhưng collection "
                f"{connection.collection_name!r} thực có dimension {dimension}"
            ),
            reason=ProbeFailureReason.DIMENSION_MISMATCH,
        )

    return ProbeOutcome(
        status=ExternalConnectionStatus.CONNECTED,
        detail=f"Kết nối thành công — dimension {dimension} khớp khai báo",
    )


def _get_qdrant_collection_dimension(
    connection: ExternalConnection, *, secret: str | None, http_get: HttpGet
) -> int:
    """The vector dimension `connection.collection_name` actually has, read off Qdrant's own REST API.

    Raw REST (`GET /collections/{name}`) rather than `qdrant_client`, so this
    module — not a client library's own defaults — decides whether a
    redirect is followed. `api-key` is Qdrant's own header name for a
    credential; sent only when `secret` is non-empty, so a self-hosted
    instance with no key configured is dialled with none.
    """
    url = f"{connection.connection_url.rstrip('/')}/collections/{connection.collection_name}"
    headers = {"api-key": secret} if secret else None
    try:
        response = http_get(url, headers)
    except (httpx.HTTPError, SSRFBlockedError) as exc:
        # `SSRFBlockedError` here means the real transport's own, fresher
        # resolution (`pin_safe_ip`, inside `http_get_via_httpx`) disagreed
        # with `probe_connection`'s earlier `assert_safe_url` — a live rebind
        # caught in the act rather than missed. Either way this is "could not
        # reach it", not a crash.
        reason = (
            ProbeFailureReason.BLOCKED_ADDRESS
            if isinstance(exc, SSRFBlockedError)
            else ProbeFailureReason.UNREACHABLE
        )
        raise ProbeConnectionError(f"Không kết nối được tới {connection.connection_url!r}: {exc}", reason) from exc

    _ensure_answered_usefully(connection, response)

    try:
        body = response.json()
    except ValueError as exc:
        raise ProbeConnectionError(
            f"{connection.connection_url!r} không trả về JSON hợp lệ", ProbeFailureReason.UNREADABLE_ANSWER
        ) from exc

    dimension = _dimension_of(body, connection.field_mapping.get("vector_field"))
    if dimension is None:
        raise ProbeConnectionError(
            f"Không đọc được cấu hình vector của collection {connection.collection_name!r} từ câu trả lời",
            ProbeFailureReason.UNREADABLE_ANSWER,
        )
    return dimension


def _ensure_answered_usefully(connection: ExternalConnection, response: httpx.Response) -> None:
    """Raise unless `response` is something a dimension could be read out of — one refusal per status shape.

    Split out of `_get_qdrant_collection_dimension` rather than inlined: the
    cascade is five unrelated verdicts about the same one value, and reading
    the collection's dimension is a different job from deciding whether the
    far end answered at all.
    """
    if 300 <= response.status_code < 400:
        raise ProbeConnectionError(
            f"{connection.connection_url!r} trả về redirect — bị từ chối để chống SSRF qua redirect",
            ProbeFailureReason.REDIRECT_REFUSED,
        )
    if response.status_code == 404:
        raise ProbeConnectionError(
            f"Không tìm thấy collection {connection.collection_name!r} trên {connection.connection_url!r}",
            ProbeFailureReason.COLLECTION_NOT_FOUND,
        )
    if response.status_code in (401, 403):
        raise ProbeConnectionError(
            f"Máy chủ từ chối thông tin xác thực (HTTP {response.status_code})", ProbeFailureReason.AUTH_REJECTED
        )
    if response.status_code >= 400:
        raise ProbeConnectionError(
            f"Máy chủ trả lỗi HTTP {response.status_code}: {response.text[:200]}", ProbeFailureReason.SERVER_ERROR
        )


def _dimension_of(collection_info: Any, vector_field: str | None = None) -> int | None:
    """The vector size out of a Qdrant `GET /collections/{name}` body, or `None` if it cannot be read.

    Handles the shapes Qdrant serves: an unnamed default vector
    (`vectors: {"size": N, ...}`) and named ones
    (`vectors: {"dense": {"size": N, ...}}`, this platform's own shape —
    `src.services.qdrant.DENSE_VECTOR_NAME`).

    `vector_field` is `ExternalConnection.field_mapping["vector_field"]` — the
    same declaration `QdrantAdapter.search` passes to the Query API as
    `using`. When a collection holds several named vectors, that declaration
    is the only thing that says which one is the embedding, and measuring a
    different vector than the one searches will actually run against would
    make `/test` agree about a connection that then behaves otherwise. With
    no declaration and more than one vector there is nothing to measure, so
    it stays unread rather than guessed at.
    """
    try:
        vectors = collection_info["result"]["config"]["params"]["vectors"]
    except (KeyError, TypeError, IndexError):
        return None
    if not isinstance(vectors, dict):
        return None
    if vector_field is not None:
        named = vectors.get(vector_field)
        return named["size"] if isinstance(named, dict) and isinstance(named.get("size"), int) else None
    if isinstance(vectors.get("size"), int):
        return vectors["size"]
    if len(vectors) == 1:
        (only,) = vectors.values()
        if isinstance(only, dict) and isinstance(only.get("size"), int):
            return only["size"]
    return None
