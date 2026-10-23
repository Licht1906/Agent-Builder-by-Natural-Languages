"""The MCP Streamable HTTP handshake, which is the half a hand-written double never exercised.

Every test here scripts a whole server through `httpx.MockTransport`, so what is
under test is the bytes on the wire — the order of the requests, the headers,
the framing of the reply — rather than a function's return value. That is the
point: the previous transport passed every test in the suite by returning the
right dict, and could not have talked to a real MCP server, because talking to
one is a *conversation* and it only knew how to shout.

Nothing here opens a socket. `McpSession(client=...)` is the seam that makes
that true, and it is the same discipline `rpc` gives the layer above.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from src.workflow.mcp import ACCEPT, MAX_TOOL_PAGES, McpError, McpSession, introspect

Handler = Callable[[httpx.Request], httpx.Response]


def client_for(handler: Handler) -> httpx.Client:
    """An `httpx.Client` whose every request is answered by `handler`."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def jsonrpc(request: httpx.Request) -> dict[str, Any]:
    """The JSON-RPC body a scripted server just received."""
    body: dict[str, Any] = json.loads(request.content)
    return body


def ok(request_id: int | None, result: dict[str, Any]) -> httpx.Response:
    """A plain `application/json` JSON-RPC reply."""
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": request_id, "result": result})


def sse(*messages: dict[str, Any]) -> httpx.Response:
    """The same reply, framed the other way MCP is allowed to frame it."""
    body = "".join(f"event: message\ndata: {json.dumps(message)}\n\n" for message in messages)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


INITIALIZED = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "test", "version": "1"}}
ONE_TOOL = {"tools": [{"name": "read_ticket", "description": "Đọc một ticket"}]}


class _Server:
    """A scripted MCP server that remembers what it was asked, in order."""

    def __init__(self, *, session_id: str | None = "sess-1", reply: Handler | None = None) -> None:
        self.session_id = session_id
        self.requests: list[httpx.Request] = []
        self.methods: list[str] = []
        self._reply = reply

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        body = jsonrpc(request)
        self.methods.append(body["method"])
        response = self._reply(request) if self._reply else self._default(body)
        if self.session_id and body["method"] == "initialize":
            response.headers["mcp-session-id"] = self.session_id
        return response

    def _default(self, body: dict[str, Any]) -> httpx.Response:
        if body["method"] == "initialize":
            return ok(body["id"], INITIALIZED)
        if body["method"].startswith("notifications/"):
            return httpx.Response(202)
        return ok(body["id"], ONE_TOOL)


def test_initialize_comes_first_and_is_acknowledged() -> None:
    """`tools/list` is the *third* thing on the wire, not the first.

    The whole bug the old transport had, stated as an assertion: a server is
    entitled to refuse a `tools/list` from a client that never introduced
    itself, and most do.
    """
    server = _Server()
    with McpSession("https://mcp.test/rpc", client=client_for(server)) as session:
        session.call("tools/list", {})

    assert server.methods == ["initialize", "notifications/initialized", "tools/list"]


def test_every_request_advertises_both_reply_framings() -> None:
    """`Accept` names JSON *and* SSE, because the server picks which one it sends."""
    server = _Server()
    with McpSession("https://mcp.test/rpc", client=client_for(server)) as session:
        session.call("tools/list", {})

    assert all(request.headers["accept"] == ACCEPT for request in server.requests if request.method == "POST")


def test_session_id_is_minted_once_and_carried_afterwards() -> None:
    """The id the server put on its `initialize` reply rides every later request.

    A stateful server drops a request that arrives without it, and the symptom
    is a 404 on the second call of a connection whose first call worked.
    """
    server = _Server(session_id="sess-42")
    with McpSession("https://mcp.test/rpc", client=client_for(server)) as session:
        assert session.session_id == "sess-42"
        session.call("tools/list", {})

    initialize, initialized, listing = (request for request in server.requests if request.method == "POST")
    assert "mcp-session-id" not in initialize.headers
    assert initialized.headers["mcp-session-id"] == "sess-42"
    assert listing.headers["mcp-session-id"] == "sess-42"


def test_protocol_version_announced_is_the_one_the_server_chose() -> None:
    """We offer `2025-06-18`; this server answered `2025-03-26`, so that is what we speak.

    Announcing our own would be the client telling the server what it decided,
    and it is how a platform ends up unable to talk to anything a revision
    behind it.
    """
    server = _Server()
    with McpSession("https://mcp.test/rpc", client=client_for(server)) as session:
        assert session.protocol_version == "2025-03-26"
        session.call("tools/list", {})

    listing = [request for request in server.requests if request.method == "POST"][-1]
    assert listing.headers["mcp-protocol-version"] == "2025-03-26"


def test_no_protocol_version_header_before_one_has_been_agreed() -> None:
    """`initialize` itself carries no `MCP-Protocol-Version` — there is nothing true to put there."""
    server = _Server()
    with McpSession("https://mcp.test/rpc", client=client_for(server)):
        pass

    initialize = next(request for request in server.requests if request.method == "POST")
    assert "mcp-protocol-version" not in initialize.headers


def test_reply_delivered_as_sse_is_read() -> None:
    """A `text/event-stream` answer is the same answer, and the old transport could not read one."""

    def reply(request: httpx.Request) -> httpx.Response:
        body = jsonrpc(request)
        if body["method"] == "initialize":
            return sse({"jsonrpc": "2.0", "id": body["id"], "result": INITIALIZED})
        if body["method"].startswith("notifications/"):
            return httpx.Response(202)
        return sse({"jsonrpc": "2.0", "id": body["id"], "result": ONE_TOOL})

    with McpSession("https://mcp.test/rpc", client=client_for(_Server(reply=reply))) as session:
        assert session.call("tools/list", {}) == ONE_TOOL


def test_server_traffic_we_did_not_ask_for_is_skipped_not_fatal() -> None:
    """A server may put its own notifications on the stream ahead of our reply.

    Falling over on the first one would mean working only against servers that
    never use a feature the protocol grants them.
    """

    def reply(request: httpx.Request) -> httpx.Response:
        body = jsonrpc(request)
        if body["method"] == "initialize":
            return ok(body["id"], INITIALIZED)
        if body["method"].startswith("notifications/"):
            return httpx.Response(202)
        return sse(
            {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}},
            {"jsonrpc": "2.0", "id": 999, "result": {"tools": []}},
            {"jsonrpc": "2.0", "id": body["id"], "result": ONE_TOOL},
        )

    with McpSession("https://mcp.test/rpc", client=client_for(_Server(reply=reply))) as session:
        assert session.call("tools/list", {}) == ONE_TOOL


def test_credential_travels_in_the_authorization_header() -> None:
    """Never in the URL and never in the JSON-RPC body — both are places a server logs."""
    server = _Server()
    with McpSession("https://mcp.test/rpc", secret="tok-abc", client=client_for(server)) as session:
        session.call("tools/list", {})

    for request in server.requests:
        if request.method != "POST":
            continue
        assert request.headers["authorization"] == "Bearer tok-abc"
        assert "tok-abc" not in str(request.url)
        assert "tok-abc" not in request.content.decode()


def test_jsonrpc_error_becomes_mcp_error_even_though_http_said_200() -> None:
    """A refusal is a *reply*. `raise_for_status` would never have seen it."""

    def reply(request: httpx.Request) -> httpx.Response:
        body = jsonrpc(request)
        if body["method"] == "initialize":
            return ok(body["id"], INITIALIZED)
        if body["method"].startswith("notifications/"):
            return httpx.Response(202)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "no such method"}}
        )

    with McpSession("https://mcp.test/rpc", client=client_for(_Server(reply=reply))) as session:
        with pytest.raises(McpError, match="từ chối"):
            session.call("tools/list", {})


def test_expired_session_is_named_as_such_rather_than_as_a_missing_server() -> None:
    """A 404 once a session exists means the server dropped it — not that the URL is wrong.

    Two very different things for the person reading the message: one is "log in
    again", the other is "you typed the address wrong".
    """

    def reply(request: httpx.Request) -> httpx.Response:
        body = jsonrpc(request)
        if body["method"] == "initialize":
            return ok(body["id"], INITIALIZED)
        if body["method"].startswith("notifications/"):
            return httpx.Response(202)
        return httpx.Response(404, text="Session not found")

    with McpSession("https://mcp.test/rpc", client=client_for(_Server(reply=reply))) as session:
        with pytest.raises(McpError, match="hết hạn"):
            session.call("tools/list", {})


def test_calling_before_the_handshake_is_refused() -> None:
    """No implicit initialize. Hiding a caller's mistake is how the mistake survives."""
    session = McpSession("https://mcp.test/rpc", client=client_for(_Server()))
    with pytest.raises(McpError, match="Chưa initialize"):
        session.call("tools/list", {})


def test_close_lets_the_server_drop_the_session() -> None:
    """The `DELETE` is a courtesy, but a server holding sessions forever is a real leak."""
    server = _Server(session_id="sess-9")
    with McpSession("https://mcp.test/rpc", client=client_for(server)):
        pass

    deletes = [request for request in server.requests if request.method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0].headers["mcp-session-id"] == "sess-9"


def test_a_server_that_refuses_the_delete_does_not_fail_the_work_already_done() -> None:
    """`close` runs in `__exit__`, where raising would bury whatever the body was carrying."""

    def reply(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(405)
        body = jsonrpc(request)
        if body["method"] == "initialize":
            return ok(body["id"], INITIALIZED)
        return httpx.Response(202) if body["method"].startswith("notifications/") else ok(body["id"], ONE_TOOL)

    with McpSession("https://mcp.test/rpc", client=client_for(_Server(reply=reply))) as session:
        assert session.call("tools/list", {}) == ONE_TOOL


def test_a_failed_handshake_still_closes_the_client_it_opened() -> None:
    """`__exit__` never runs for an `__enter__` that raised, so `__enter__` cleans up itself."""
    client = client_for(lambda request: httpx.Response(500, text="down"))
    with pytest.raises(McpError, match="HTTP 500"):
        with McpSession("https://mcp.test/rpc", client=client):
            pass


class _PagedServer(_Server):
    """A server whose `tools/list` comes in three pages, cursor-linked."""

    PAGES = [
        {"tools": [{"name": "read_ticket"}], "nextCursor": "p2"},
        {"tools": [{"name": "search_ticket"}], "nextCursor": "p3"},
        {"tools": [{"name": "close_ticket"}]},
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cursors: list[str | None] = []

    def _default(self, body: dict[str, Any]) -> httpx.Response:
        if body["method"] == "initialize":
            return ok(body["id"], INITIALIZED)
        if body["method"].startswith("notifications/"):
            return httpx.Response(202)
        cursor = body["params"].get("cursor")
        self.cursors.append(cursor)
        page = 0 if cursor is None else ["p2", "p3"].index(cursor) + 1
        return ok(body["id"], self.PAGES[page])


def test_introspect_walks_every_page_in_one_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """One handshake, three `tools/list` calls, three actions — and one cursor chain.

    Walked inside a single session on purpose: a cursor is a token the server
    minted, and it is entitled to scope it to the session that received it.
    """
    server = _PagedServer()
    monkeypatch.setattr(
        "src.workflow.mcp.McpSession.__init__",
        _session_init_with(client_for(server)),
    )

    actions = introspect("https://mcp.test/rpc")

    assert [action.name for action in actions] == ["read_ticket", "search_ticket", "close_ticket"]
    assert server.cursors == [None, "p2", "p3"]
    assert server.methods.count("initialize") == 1


def _session_init_with(client: httpx.Client) -> Callable[..., None]:
    """`McpSession.__init__` with this test's scripted client forced in.

    Patching the constructor rather than passing a client is what lets
    `introspect`'s *own* choice of session be the thing under test — the point
    of the assertion is that it opens exactly one, and a session handed in from
    outside could not show that.
    """
    original = McpSession.__init__

    def patched(self: McpSession, server_url: str, **kwargs: Any) -> None:
        kwargs["client"] = client
        original(self, server_url, **kwargs)

    return patched


def test_page_following_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that always offers another cursor cannot hold a registration open forever."""

    class _Endless(_Server):
        def _default(self, body: dict[str, Any]) -> httpx.Response:
            if body["method"] == "initialize":
                return ok(body["id"], INITIALIZED)
            if body["method"].startswith("notifications/"):
                return httpx.Response(202)
            return ok(body["id"], {"tools": [{"name": "read_thing"}], "nextCursor": "more"})

    server = _Endless()
    monkeypatch.setattr("src.workflow.mcp.McpSession.__init__", _session_init_with(client_for(server)))

    assert len(introspect("https://mcp.test/rpc")) == MAX_TOOL_PAGES
