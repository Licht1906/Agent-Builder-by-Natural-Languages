"""Asking an MCP server what it can do, and asking it to do one of those things.

See issue #52 / ADR-0023 §7. A creator registering a connection submits a
`server_url` and a credential reference — **never** a hand-written list of
actions. This module is the reason they cannot: `introspect` calls the server's
own `tools/list` and reads `actions[]` off the answer, so what the registry
believes a connection can do is what the connection says it can do, checked
against the server rather than against a form somebody filled in months ago.

**The transport is a seam, and the seam is the whole test surface.** `rpc` is a
parameter everywhere it is needed, defaulting to `http_rpc`. A test scripts an
MCP server by passing a function; nothing in the suite needs a socket, and
nothing in production needs a flag saying "pretend". Same discipline
`src.services.llm`'s factory has.

**The seam's shape is one call; the protocol underneath it is a session.** That
split is deliberate. MCP's Streamable HTTP transport is not "POST a JSON-RPC
body at a URL": a client has to `initialize`, agree a protocol version, send
`notifications/initialized`, carry whatever `Mcp-Session-Id` the server minted
on every later request, and be able to read a reply that arrives as
`text/event-stream` rather than as JSON. A server is entitled to refuse
`tools/list` until all of that has happened, and most real ones do — which is
why the version of this module that POSTed a bare envelope worked against a
hand-written double and not against a server. `McpSession` owns that lifecycle;
`http_rpc` is one short-lived session around one call, so the seam every
existing double implements keeps its four-argument shape.

**`read_only` is inferred, not believed.** MCP lets a server annotate an action
`readOnlyHint: true`, and taking that at face value would put the safety gate
back in the hands of the party it is a gate on — the same objection ADR-0023 §3
raises against a self-declared `risk_level`. So the hint is trusted in exactly
one direction: `readOnlyHint: false` is an admission against interest and is
believed outright, while a claim of *read-only* still has to survive
`reads_only`'s own reading of the action's name. Anything this module cannot
recognise comes out as a write, which is the direction a wrong answer is
recoverable in — a write misread as a read is a real side effect in a rehearsal
nobody was watching.
"""

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field

from src.config import get_settings
from src.workflow.effects import is_high_risk, without_diacritics


class McpError(RuntimeError):
    """The MCP server could not be reached, or answered with something unusable.

    One class for both, because every caller does the same thing with them:
    tells the creator their server did not answer the way an MCP server answers.
    The distinction between "connection refused" and "no `tools` in the result"
    is in the message, where a person can read it, rather than in a type nothing
    branches on.
    """


#: The revision of MCP's own specification this client offers at `initialize`.
#:
#: Offered, not imposed: a server answers `initialize` with the version *it*
#: chose, and `McpSession` announces that one on every later request's
#: `MCP-Protocol-Version` header. Pinning ours would make this platform unable
#: to talk to a server a revision behind it, which is most of them.
MCP_PROTOCOL_VERSION = "2025-06-18"

#: Who is calling, as `initialize` asks every client to say. Servers put this in
#: their logs, and a connection nobody can attribute is one an operator cannot
#: revoke on purpose.
CLIENT_INFO: dict[str, str] = {"name": "nl-agent-builder", "version": "1"}

#: Both media types, because Streamable HTTP lets the server pick per response —
#: JSON for a short answer, SSE when it wants to stream or to interleave its own
#: requests. A client advertising only one is entitled to a 406, and that
#: failure reads as "the server is broken" when it is not.
ACCEPT = "application/json, text/event-stream"

#: How many `tools/list` pages `introspect` follows before it stops.
#:
#: A cap rather than a trusting loop: `nextCursor` is an opaque string the
#: server chooses, and one that always returns another would hold a
#: registration request open forever. Twenty pages is far past any real
#: connection's tool count, so reaching it is a server misbehaving rather than
#: a creator with a big catalogue.
MAX_TOOL_PAGES = 20


#: How one JSON-RPC call reaches an MCP server: `(server_url, method, params, secret) -> result`.
#:
#: The `result` member of the envelope, already unwrapped — callers of this
#: module are interested in what the server said, not in the fact that it said
#: it in JSON-RPC. A double that has to reproduce `{"jsonrpc": "2.0", ...}` to be
#: useful is a double testing the envelope rather than the code above it.
#:
#: `secret` (RAG-19 / ADR-0045) is the plaintext value a `SECRET_REFERENCE`
#: connection's credential resolved to, or `None` for every other connection —
#: a fourth positional member of the seam rather than folded into `params`,
#: because `params` becomes the JSON-RPC body a server reads as the *call's own
#: arguments*, and a credential is not one of them.
McpTransport = Callable[[str, str, Mapping[str, Any], str | None], Mapping[str, Any]]


class McpSession:
    """One Streamable HTTP conversation with one MCP server, from `initialize` to close.

    Everything the transport has to remember between two requests lives here:
    the protocol version the server picked, the `Mcp-Session-Id` it minted, and
    the JSON-RPC id counter. Those are the three things a bare `httpx.post` has
    nowhere to keep.

    Used as a context manager, entering *is* the handshake — there is no state
    in which a caller holds a session that has not initialized, so no call site
    has to remember to do it. `close` is best-effort on purpose: the `DELETE` is
    a courtesy that lets the server drop the session early, and a server that
    refuses it (the spec allows that) has not failed the work already done.

    `client` is injectable for the same reason `rpc` is a parameter: a test
    hands in `httpx.Client(transport=httpx.MockTransport(...))` and scripts a
    whole handshake — status codes, SSE framing, session headers — without a
    socket. A client this class opened is one it also closes; a caller's own is
    left alone.
    """

    def __init__(
        self,
        server_url: str,
        *,
        secret: str | None = None,
        headers: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
        timeout: float | None = None,
    ) -> None:
        self.server_url = server_url
        self._secret = secret
        # The `CUSTOM_HEADERS` credential kind — Agent Builder's third auth
        # choice. Merged *under* the protocol headers below, never over them: a
        # connection that set its own `Mcp-Session-Id` would break the session
        # this class is here to keep.
        self._extra_headers = dict(headers or {})
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=get_settings().tool_http_timeout_seconds if timeout is None else timeout
        )
        self._session_id: str | None = None
        self._protocol_version = MCP_PROTOCOL_VERSION
        self._initialized = False
        self._last_id = 0

    def __enter__(self) -> Self:
        try:
            self.initialize()
        except BaseException:
            # `__exit__` never runs for an `__enter__` that raised, so a failed
            # handshake would otherwise leak the client this object opened.
            self.close()
            raise
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    @property
    def session_id(self) -> str | None:
        """The `Mcp-Session-Id` this server minted, or `None` if it runs stateless."""
        return self._session_id

    @property
    def protocol_version(self) -> str:
        """The revision the server chose at `initialize` — what later requests announce."""
        return self._protocol_version

    def initialize(self) -> Mapping[str, Any]:
        """The handshake, and the reason a real server answers `tools/list` at all.

        Three steps MCP requires in this order: offer a protocol version and say
        who is calling; adopt whatever version the server answered with; then
        send `notifications/initialized` so the server knows the client is
        ready. A `tools/list` sent before that last step is a client the server
        may refuse — and the refusal reads, to the creator who registered the
        connection, as their own server being broken.
        """
        result = self.request(
            "initialize",
            {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": dict(CLIENT_INFO)},
        )
        negotiated = result.get("protocolVersion")
        if isinstance(negotiated, str) and negotiated:
            self._protocol_version = negotiated
        self._initialized = True
        self.notify("notifications/initialized")
        return result

    def call(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """One initialized JSON-RPC call — what `McpTransport` promises, minus the setup.

        Refuses rather than initializing implicitly: a session that handshook on
        first use would hide a caller that forgot, and the hiding is what lets
        the ordering bug survive into production. Every path that builds a
        session enters it as a context manager, so this is a programming error
        rather than anything a creator can cause.
        """
        if not self._initialized:
            raise McpError(f"Chưa initialize phiên MCP với {self.server_url!r} nên chưa gọi {method!r} được")
        return self.request(method, params)

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """One JSON-RPC request and its reply's `result`, initialized or not."""
        self._last_id += 1
        body = {"jsonrpc": "2.0", "id": self._last_id, "method": method, "params": dict(params)}
        reply = self._post(body, wanted_id=self._last_id, method=method)
        if reply is None:
            raise McpError(f"MCP server {self.server_url!r} không trả lời gì cho {method!r}")
        return self._result_of(reply, method)

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """A JSON-RPC notification — no id, and no reply expected or waited for."""
        self._post({"jsonrpc": "2.0", "method": method, "params": dict(params or {})}, wanted_id=None, method=method)

    def close(self) -> None:
        """Let the server drop the session, then let go of the client we opened.

        Both halves are best-effort and neither may fail the caller: this runs
        in `__exit__`, where raising would replace whatever exception the body
        was already carrying with a tidy-up failure nobody can act on.
        """
        if self._session_id is not None:
            try:
                self._client.delete(self.server_url, headers=self._headers())
            except httpx.HTTPError:
                pass
            self._session_id = None
        if self._owns_client:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        """What every request in this session carries.

        `MCP-Protocol-Version` only once the handshake has settled one, because
        before that there is nothing truthful to put there — announcing a
        version the server has not agreed to is the client telling the server
        what it decided.

        `Authorization` rather than a query parameter or a body field, for the
        reason the MCP security guidance gives: a URL is logged by every proxy
        on the way, and a JSON-RPC body is echoed back inside errors.
        """
        headers = {"Accept": ACCEPT, "Content-Type": "application/json", **self._extra_headers}
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._initialized:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    def _post(self, body: Mapping[str, Any], *, wanted_id: int | None, method: str) -> Mapping[str, Any] | None:
        """Send `body`, and give back the JSON-RPC message answering `wanted_id`.

        Streamed rather than read whole, because an SSE reply is a stream the
        server may hold open after it has already said the thing we asked for.
        Reading to the end would turn every such server into a timeout; stopping
        at the matching id turns it into an answer.
        """
        try:
            with self._client.stream("POST", self.server_url, json=dict(body), headers=self._headers()) as response:
                minted = response.headers.get("mcp-session-id")
                if minted:
                    self._session_id = minted
                if response.status_code == 404 and self._session_id is not None:
                    response.read()
                    raise McpError(
                        f"Phiên với MCP server {self.server_url!r} đã hết hạn khi gọi {method!r} — cần kết nối lại"
                    )
                if response.status_code >= 400:
                    response.read()
                    raise McpError(
                        f"MCP server {self.server_url!r} trả về HTTP {response.status_code} cho {method!r}: "
                        f"{response.text[:400]}"
                    )
                if wanted_id is None:
                    # A notification. `202 Accepted` with an empty body is the
                    # spec's own answer, and reading it is what releases the
                    # connection back to the pool.
                    response.read()
                    return None
                if response.headers.get("content-type", "").partition(";")[0].strip() == "text/event-stream":
                    return self._from_sse(response, wanted_id=wanted_id, method=method)
                response.read()
                return self._from_json(response, method=method)
        except httpx.HTTPError as exc:
            raise McpError(f"Không gọi được {method!r} trên MCP server {self.server_url!r}: {exc}") from exc

    def _from_json(self, response: httpx.Response, *, method: str) -> Mapping[str, Any]:
        """A plain `application/json` reply, as the one JSON-RPC message it holds."""
        try:
            envelope = response.json()
        except ValueError as exc:
            raise McpError(f"MCP server {self.server_url!r} trả về thứ không phải JSON cho {method!r}") from exc
        if not isinstance(envelope, Mapping):
            raise McpError(f"MCP server {self.server_url!r} trả về JSON không phải object cho {method!r}")
        return envelope

    def _from_sse(self, response: httpx.Response, *, wanted_id: int, method: str) -> Mapping[str, Any]:
        """The first SSE event carrying the JSON-RPC reply to `wanted_id`.

        Everything else on the stream is skipped rather than treated as an
        error: a server may send its own requests and notifications down the
        same channel, and a client that fell over on the first thing it did not
        ask for would work only against servers that never use the feature.
        """
        for payload in _sse_payloads(response):
            try:
                message = json.loads(payload)
            except ValueError:
                continue
            if isinstance(message, Mapping) and message.get("id") == wanted_id:
                return message
        raise McpError(f"MCP server {self.server_url!r} đóng luồng SSE mà chưa trả lời {method!r}")

    def _result_of(self, envelope: Mapping[str, Any], method: str) -> Mapping[str, Any]:
        """The `result` member, or the server's refusal turned into `McpError`.

        A JSON-RPC error is a *reply* — it arrives with HTTP 200 — so it is
        caught here or not at all.
        """
        if "error" in envelope:
            raise McpError(f"MCP server {self.server_url!r} từ chối {method!r}: {envelope['error']}")
        result = envelope.get("result")
        if not isinstance(result, Mapping):
            raise McpError(f"MCP server {self.server_url!r} trả lời {method!r} mà không có `result`")
        return result


def _sse_payloads(response: httpx.Response) -> Iterator[str]:
    """The `data` of each event on an SSE stream, in order.

    A minimal reading of the format on purpose — `data:` lines joined by
    newlines, a blank line ending the event, everything else (`event:`, `id:`,
    `retry:`, comments) ignored. MCP puts one JSON-RPC message in `data` and
    uses none of the rest, so parsing them would be parsing for a case that does
    not arise.
    """
    data: list[str] = []
    for line in response.iter_lines():
        if not line.strip():
            if data:
                yield "\n".join(data)
                data = []
            continue
        if line.startswith("data:"):
            data.append(line[len("data:") :].lstrip(" "))
    if data:
        yield "\n".join(data)


def http_rpc(
    server_url: str,
    method: str,
    params: Mapping[str, Any],
    secret: str | None = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> Mapping[str, Any]:
    """One JSON-RPC call to `server_url`, over a session opened and closed around it.

    Blocking on purpose: the one caller that runs inside the event loop
    (`src.workflow.runtime`) already pushes a connection's `invoke` onto a
    thread, and a second async path would be a second implementation of this
    that could disagree with the first about, say, what a timeout is.

    A whole handshake per call is more traffic than a long-lived session would
    be, and it is the right trade here: a `ToolDefinition` is rebuilt per run
    (`registrations.registered_tool`), an `MCP` node fires once, and a pooled
    session would need an owner outliving both — plus an invalidation rule for a
    server that expired it. `McpSession` is public for the caller that does have
    somewhere to keep one; `introspect` is the first.
    """
    with McpSession(server_url, secret=secret, headers=headers) as session:
        return session.call(method, params)


def get_mcp_transport() -> McpTransport:
    """FastAPI dependency: how a request reaches an MCP server.

    The registration endpoint's one seam, declared the way `get_chat_model_factory`
    and `get_knowledge_store` are — a test scripts a server by overriding this,
    and the production wiring is the default rather than a branch on a flag.
    """
    return http_rpc


class IntrospectedAction(BaseModel):
    """One entry of an MCP server's `tools/list`, as this platform reads it.

    Frozen, and deliberately without a `risk_level`: this is the *report* from
    the server, and risk is something the platform decides about the report
    (ADR-0023 §3). Keeping the two apart is what lets an admin override a risk
    without anything they do reaching the introspected schema — there is no
    field here for a review to write into.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(..., min_length=1, description="Tên action như server tự khai trong `tools/list`")
    description: str = Field(default="", description="Mô tả server tự khai, để nguyên văn cho admin đọc")
    input_schema: dict[str, Any] = Field(default_factory=dict, description="JSON Schema tham số, do server khai")
    output_schema: dict[str, Any] = Field(default_factory=dict, description="JSON Schema kết quả, do server khai")
    read_only: bool = Field(..., description="Suy ra từ tên action và hint của server — xem `reads_only`")


#: Verbs an action is named with when it only looks at something. Matched as
#: whole tokens of the action *name*, never as substrings of a sentence: "get"
#: inside "budget" and "post" inside "compost" are exactly the false *low* this
#: module is built to avoid, and a description is a paragraph where such
#: accidents are common.
#:
#: Vietnamese beside English because a creator's own MCP server is named in
#: whichever language they think in, and the platform's other word list
#: (`src.workflow.effects`) is bilingual for the same reason.
_READ_VERBS: frozenset[str] = frozenset(
    {
        "get",
        "list",
        "read",
        "search",
        "fetch",
        "find",
        "query",
        "lookup",
        "show",
        "describe",
        "count",
        "view",
        "doc",
        "xem",
        "tim",
        "tra",
        "lay",
        "liet",
    }
)


def reads_only(name: str, *, hint: bool | None = None) -> bool:
    """Whether this action only looks at things — the fact every risk rule is keyed to.

    Three readings, in the order they can each be wrong:

    1. `hint is False` — the server says it writes. Believed immediately: a
       server has no reason to claim a side effect it does not have.
    2. `is_high_risk` over the **name** — the platform's one word list for
       unrecoverable effects (`src.workflow.effects`). A `get_and_delete_message`
       is not a read whatever its first token says.
    3. A read verb among the tokens of the name. Absent one, the answer is "no",
       not "unknown": an action this module cannot place becomes at least
       `medium`, so a sandbox mocks it and an admin looks at it.

    **The description is deliberately not read here**, and that is the same
    judgement `src.workflow.tools.risk_of` makes when it answers `low` for a
    read before consulting any word list: a description is prose, "email" in it
    is a noun, and letting the list see it would classify `list_campaigns`
    ("liệt kê các chiến dịch email") as a write. The description still decides
    plenty — it is what separates `medium` from `high` once an action is known to
    write — but it cannot make a read into one.

    Note what is *not* here either: `hint is True` on its own decides nothing.
    Trusting it would hand the safety gate to the party being gated, which is
    ADR-0023 §3's objection to a self-declared `risk_level` arriving through a
    different field.
    """
    if hint is False:
        return False
    if is_high_risk(name):
        return False
    return bool(_READ_VERBS & _name_tokens(name))


def _name_tokens(name: str) -> set[str]:
    """`name` as the words it is built from — `read_inbox`, `readInbox` and `read.inbox` alike.

    The camel-case split happens **before** the lowercasing, which is the whole
    of why it works: `without_diacritics` lowercases, so a caller that
    normalised first would hand this a flat `readinbox` with no boundary left to
    find — and every camel-cased server (the dominant style in MCP) would come
    back with no read verb and every one of its actions graded a write.
    """
    spaced = "".join(f" {character}" if character.isupper() else character for character in name)
    separated = without_diacritics(spaced).replace("_", " ").replace("-", " ").replace(".", " ").replace("/", " ")
    return set(separated.split())


def introspect(
    server_url: str,
    *,
    rpc: McpTransport = http_rpc,
    secret: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[IntrospectedAction, ...]:
    """Everything `server_url` says it can do, as `actions[]` in this platform's shape.

    **Every page of it.** MCP paginates `tools/list` with an opaque `nextCursor`,
    and a client reading the first page only registers a connection whose later
    actions do not exist as far as this platform is concerned — the creator
    would open the canvas dropdown and simply never find the action they came
    for. The pages are walked inside a *single* session when the transport is
    the real one, because a cursor is a token the server minted and is entitled
    to scope to the session that received it.

    `secret` is the connection's own credential, so a server that authenticates
    `tools/list` — most do — can be introspected at all, rather than answering
    401 and reading, to the creator, as a broken server.

    Raises `McpError` rather than returning an empty tuple when the server
    answers with no tools at all: a connection with nothing on it is not a
    connection anybody can wire an `MCP` node to, and landing it in the review
    queue would spend an admin's attention on a row that could never be useful.
    """
    if rpc is http_rpc:
        with McpSession(server_url, secret=secret, headers=headers) as session:
            actions = _all_pages(lambda params: session.call("tools/list", params), server_url)
    else:
        actions = _all_pages(lambda params: rpc(server_url, "tools/list", params, secret), server_url)
    if not actions:
        raise McpError(f"MCP server {server_url!r} không khai action nào — không có gì để đăng ký")
    return actions


def _all_pages(
    list_tools: Callable[[Mapping[str, Any]], Mapping[str, Any]], server_url: str
) -> tuple[IntrospectedAction, ...]:
    """Follow `nextCursor` until the server stops offering one, or `MAX_TOOL_PAGES`."""
    actions: list[IntrospectedAction] = []
    cursor: str | None = None
    for _ in range(MAX_TOOL_PAGES):
        result = list_tools({"cursor": cursor} if cursor else {})
        listed = result.get("tools")
        if not isinstance(listed, Sequence) or isinstance(listed, str | bytes):
            raise McpError(f"MCP server {server_url!r} trả lời `tools/list` mà không có danh sách `tools`")
        actions.extend(_action_of(entry) for entry in listed if isinstance(entry, Mapping))
        following = result.get("nextCursor")
        if not isinstance(following, str) or not following:
            break
        cursor = following
    return tuple(actions)


def _action_of(entry: Mapping[str, Any]) -> IntrospectedAction:
    """One `tools/list` entry, read into `IntrospectedAction`.

    Tolerant about what an entry *has* and strict about what it means. MCP names
    the schemas `inputSchema`/`outputSchema` and most servers omit the second;
    an absent schema becomes `{}` rather than a refusal, because a server that
    does not describe its output is still a server whose actions an admin can
    review. An entry with no `name` is the one thing that cannot be tolerated —
    there would be nothing for `McpConfig.action` to point at.
    """
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise McpError("Có entry trong `tools/list` không có `name` — không biết đặt tên action là gì")
    annotations = entry.get("annotations")
    hint = annotations.get("readOnlyHint") if isinstance(annotations, Mapping) else None
    description = entry.get("description")
    return IntrospectedAction(
        name=name,
        description=description if isinstance(description, str) else "",
        input_schema=_schema(entry.get("inputSchema")),
        output_schema=_schema(entry.get("outputSchema")),
        read_only=reads_only(name, hint=_hint(hint)),
    )


def _hint(value: object) -> bool | None:
    """`readOnlyHint` as a tri-state — `None` for "the server did not say"."""
    return value if isinstance(value, bool) else None


def _schema(value: object) -> dict[str, Any]:
    """A JSON-Schema member of a `tools/list` entry, or `{}` if the server left it out."""
    return dict(value) if isinstance(value, Mapping) else {}


def call_action(
    server_url: str,
    action: str,
    args: Mapping[str, Any],
    *,
    rpc: McpTransport = http_rpc,
    secret: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> str:
    """Run `action` on `server_url` and give back what it said, as text.

    Text rather than the structured content blocks MCP returns, because the one
    caller is `src.workflow.runtime`, which puts the answer into a run's
    `messages` for the next node's model to read. A model reads text.

    `isError` is a refusal the *server* is reporting, and it becomes an
    `McpError` here so the runtime's existing handler files it the same way it
    files a connection that could not be reached — one "the action did not
    happen" path, not two.

    `secret` (RAG-19 / ADR-0045) is `src.workflow.registrations._invoker`'s
    resolved `SECRET_REFERENCE` value, threaded straight to `rpc` rather than
    folded into `args` — the one call site this module has for a credential
    that is not one of the action's own arguments.
    """
    result = (
        http_rpc(server_url, "tools/call", {"name": action, "arguments": dict(args)}, secret, headers=headers)
        if headers and rpc is http_rpc
        else rpc(server_url, "tools/call", {"name": action, "arguments": dict(args)}, secret)
    )
    text = _content_text(result.get("content"))
    if result.get("isError"):
        raise McpError(f"MCP server báo lỗi khi chạy action {action!r}: {text or 'không nói vì sao'}")
    return text if text else json.dumps(result, ensure_ascii=False)


def _content_text(content: object) -> str:
    """The text blocks of an MCP result, joined; empty when the result carried none."""
    if not isinstance(content, Sequence) or isinstance(content, str | bytes):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, Mapping) and isinstance(block.get("text"), str) and block["text"]
    )
