"""Registering an MCP connection, and the admin queue that reviews it — #52, ADR-0023 §7, §9.

The service layer (`src.workflow.registrations`) is covered directly in
`tests/test_workflow/test_registrations.py`; this file is the seam between it and
a client — which status a refusal arrives as, that the admin half is genuinely
admin-only, and the two claims that can only be made end to end:

- **there is no way to hand-enter a schema.** Not "the endpoint ignores one" —
  the body is refused, which is the difference between a rule and a habit.
- **disabling an approved connection stops the next run and leaves the workflow
  alone.** Criterion 6 is a claim about a *run*, so it is asserted by running one.

The MCP server is `app.dependency_overrides[get_mcp_transport]` throughout — the
seam `src.workflow.mcp` declares for exactly this. Nothing here opens a socket.
"""

from collections.abc import Callable, Generator, Mapping
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.db.models import AgentVersion, ToolRegistration, User, VersionStatus
from src.main import app
from src.workflow.mcp import McpError, get_mcp_transport
from tests.conftest import auth_header, parse_sse

pytestmark = pytest.mark.asyncio

LISTED_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_campaigns",
        "description": "Liệt kê các chiến dịch đang chạy",
        "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}},
        "outputSchema": {"type": "object", "properties": {"campaigns": {"type": "array"}}},
    },
    {
        "name": "send_campaign",
        "description": "Phát chiến dịch tới toàn bộ danh sách",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    },
]

_BODY: dict[str, Any] = {
    "name": "CRM nội bộ",
    "server_url": "https://mcp.example.test/rpc",
    "credential": {"kind": "none"},
}


@pytest.fixture
def mcp_server() -> Generator[Callable[..., None], None, None]:
    """Point the registration endpoint at a scripted MCP server for the next request."""

    def _install(tools: list[dict[str, Any]] | None = None, *, unreachable: bool = False) -> None:
        def rpc(
            server_url: str, method: str, params: Mapping[str, Any], secret: str | None = None
        ) -> Mapping[str, Any]:
            if unreachable:
                raise McpError(f"không kết nối được tới {server_url!r}")
            return {"tools": LISTED_TOOLS if tools is None else tools}

        app.dependency_overrides[get_mcp_transport] = lambda: rpc

    yield _install

    app.dependency_overrides.pop(get_mcp_transport, None)


@pytest.fixture(autouse=True)
def _no_registration_outlives_its_test(db_session: Session) -> Generator[None, None, None]:
    """Drop every row this file created.

    The registry is platform-wide and `GET /workflow/tools` lists all of it, so a
    connection left behind would show up in another file's catalogue assertion —
    and the symptom would be a failure in a suite that never registered anything.
    """
    yield
    for row in db_session.query(ToolRegistration).all():
        db_session.delete(row)
    db_session.commit()


async def register(client: AsyncClient, token: str, **body: Any) -> Any:
    return await client.post("/api/v1/workflow/tools", json=_BODY | body, headers=auth_header(token))


async def catalog(client: AsyncClient, token: str) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/workflow/tools", headers=auth_header(token))
    assert response.status_code == 200, response.text
    return list(response.json())


async def approve(client: AsyncClient, registration_id: int, token: str) -> Any:
    return await client.post(f"/api/v1/admin/tools/{registration_id}/approve", headers=auth_header(token))


async def narrow_to(client: AsyncClient, registration_id: int, token: str, *actions: str) -> Any:
    return await client.put(
        f"/api/v1/workflow/tools/registrations/{registration_id}/allowed-actions",
        json={"actions": list(actions)},
        headers=auth_header(token),
    )


async def supply_mocks(client: AsyncClient, registration_id: int, token: str, **examples: Any) -> Any:
    return await client.put(
        f"/api/v1/workflow/tools/registrations/{registration_id}/mocks",
        json={"examples": examples},
        headers=auth_header(token),
    )


async def approved_connection(client: AsyncClient, creator_token: str, admin_token: str) -> dict[str, Any]:
    """A registered connection an admin has approved — the scenery criteria 5-6 start from."""
    registered = (await register(client, creator_token)).json()
    await supply_mocks(client, registered["id"], creator_token, send_campaign={"sent": False})
    decided = await approve(client, registered["id"], admin_token)
    assert decided.status_code == 200, decided.text
    return dict(decided.json())


# --------------------------------------------------------------------------- #
# Criterion 1 — a URL and a credential go in, an introspected schema comes back
# --------------------------------------------------------------------------- #


async def test_registering_a_server_populates_its_actions_automatically(
    client: AsyncClient, creator_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()

    response = await register(client, creator_token)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    actions = {action["name"]: action for action in body["actions"]}
    assert set(actions) == {"list_campaigns", "send_campaign"}
    assert actions["list_campaigns"]["input_schema"] == {"type": "object", "properties": {"status": {"type": "string"}}}
    # Computed, not submitted — the request said nothing about risk.
    assert actions["list_campaigns"]["risk_level"] == "low"
    assert actions["send_campaign"]["risk_level"] == "high"
    assert actions["send_campaign"]["requires_human_approval"] is True
    assert actions["send_campaign"]["needs_mock"] is True


async def test_a_creator_cannot_hand_enter_the_schema(
    client: AsyncClient, creator_token: str, mcp_server: Callable[..., None]
) -> None:
    """Refused, not ignored: ADR-0023 §7's rule is the request body's shape."""
    mcp_server()

    response = await register(client, creator_token, actions=[{"name": "delete_everything", "read_only": True}])

    assert response.status_code == 422


async def test_a_server_that_cannot_be_read_is_a_bad_gateway_not_a_bad_request(
    client: AsyncClient, creator_token: str, mcp_server: Callable[..., None]
) -> None:
    """The request was fine; the creator's own server was not, and the message says so."""
    mcp_server(unreachable=True)

    response = await register(client, creator_token)

    assert response.status_code == 502, response.text
    assert "không kết nối được" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Criterion 2 — pending is invisible to anything that picks a tool
# --------------------------------------------------------------------------- #


async def test_a_pending_connection_is_not_offered_to_the_canvas(
    client: AsyncClient, creator_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()
    registered = (await register(client, creator_token)).json()

    selectable = await catalog(client, creator_token)

    assert registered["tool_id"] not in [entry["id"] for entry in selectable]
    # The built-in half is still there, so this is not an empty list passing.
    assert "gmail" in [entry["id"] for entry in selectable]


async def test_a_creator_still_sees_where_their_own_submission_stands(
    client: AsyncClient, creator_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()
    registered = (await register(client, creator_token)).json()

    response = await client.get("/api/v1/workflow/tools/registrations", headers=auth_header(creator_token))

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [registered["id"]]


async def test_a_stranger_cannot_supply_mocks_for_someone_elses_connection(
    client: AsyncClient, creator_token: str, second_creator_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()
    registered = (await register(client, creator_token)).json()

    response = await supply_mocks(client, registered["id"], second_creator_token, send_campaign={"sent": False})

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Criteria 3-4 — the queue, and the one thing a review may change
# --------------------------------------------------------------------------- #


async def test_a_creator_cannot_read_the_admin_review_queue(client: AsyncClient, creator_token: str) -> None:
    response = await client.get("/api/v1/admin/tools", headers=auth_header(creator_token))

    assert response.status_code == 403


async def test_the_queue_lists_pending_connections_with_their_computed_risk(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()
    registered = (await register(client, creator_token)).json()

    response = await client.get("/api/v1/admin/tools", headers=auth_header(admin_token))

    assert response.status_code == 200, response.text
    queued = next(row for row in response.json() if row["id"] == registered["id"])
    assert [action["risk_level"] for action in queued["actions"]] == ["low", "high"]
    assert queued["server_url"] == _BODY["server_url"]


async def test_an_admin_overrides_the_risk_and_the_derived_gates_move_with_it(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()
    registered = (await register(client, creator_token)).json()

    response = await client.post(
        f"/api/v1/admin/tools/{registered['id']}/risk",
        json={"action": "list_campaigns", "risk_level": "high"},
        headers=auth_header(admin_token),
    )

    assert response.status_code == 200, response.text
    listing = next(action for action in response.json()["actions"] if action["name"] == "list_campaigns")
    assert listing["risk_level"] == "high"
    assert listing["overridden"] is True
    assert listing["requires_human_approval"] is True
    assert listing["sandbox_behavior"] == "mock"


async def test_there_is_no_endpoint_that_edits_an_introspected_schema(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    """The review router has one write, and its body cannot carry a schema."""
    mcp_server()
    registered = (await register(client, creator_token)).json()

    response = await client.post(
        f"/api/v1/admin/tools/{registered['id']}/risk",
        json={
            "action": "list_campaigns",
            "risk_level": "high",
            "input_schema": {"type": "object", "properties": {"secret": {"type": "string"}}},
        },
        headers=auth_header(admin_token),
    )

    assert response.status_code == 422


async def test_a_creator_cannot_decide_on_any_connection(
    client: AsyncClient, creator_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()
    registered = (await register(client, creator_token)).json()

    response = await approve(client, registered["id"], creator_token)

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Criterion 5 — approve, and the connection becomes selectable
# --------------------------------------------------------------------------- #


async def test_approving_is_refused_until_every_mocked_action_has_its_mock(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    """ADR-0023 §4, collected at the last moment before a rehearsal could reach a send."""
    mcp_server()
    registered = (await register(client, creator_token)).json()

    refused = await approve(client, registered["id"], admin_token)

    assert refused.status_code == 409, refused.text
    assert "send_campaign" in refused.json()["detail"]

    supplied = await supply_mocks(client, registered["id"], creator_token, send_campaign={"sent": False})
    assert supplied.status_code == 200, supplied.text

    accepted = await approve(client, registered["id"], admin_token)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "approved"


async def test_an_approved_connection_is_selectable_in_mcp_nodes(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()
    approved = await approved_connection(client, creator_token, admin_token)

    entry = next(row for row in await catalog(client, creator_token) if row["id"] == approved["tool_id"])

    assert entry["source"] == "creator_registered"
    assert [action["name"] for action in entry["actions"]] == ["list_campaigns", "send_campaign"]
    # And it is gone from the queue, which is what "decided" means there.
    queue = await client.get("/api/v1/admin/tools", headers=auth_header(admin_token))
    assert approved["id"] not in [row["id"] for row in queue.json()]


async def test_rejecting_needs_a_reason_and_never_becomes_selectable(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()
    registered = (await register(client, creator_token)).json()

    refused = await client.post(
        f"/api/v1/admin/tools/{registered['id']}/reject", json={}, headers=auth_header(admin_token)
    )
    assert refused.status_code == 422

    response = await client.post(
        f"/api/v1/admin/tools/{registered['id']}/reject",
        json={"reason": "Không rõ ai vận hành server này"},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert registered["tool_id"] not in [entry["id"] for entry in await catalog(client, creator_token)]


# --------------------------------------------------------------------------- #
# Criterion 6 — disabling stops the next run and leaves saved workflows alone
# --------------------------------------------------------------------------- #

SPEC: dict[str, Any] = {
    "goal": "Gọi công cụ CRM đã đăng ký",
    "users": ["Nhân viên marketing"],
    "data": [{"name": "CRM", "kind": "connected_account", "ref": "conn://crm-1"}],
    "actions": ["Liệt kê chiến dịch"],
    "expected_outcome": ["Danh sách chiến dịch"],
    "success_criteria": [{"text": "Nêu đúng số chiến dịch", "category": "correctness"}],
    "prohibitions": [],
}


def calls_the_connection(tool_id: str) -> dict[str, Any]:
    """`MCP → End`, pointed at a registered connection's read-only action.

    The smallest graph that can make criterion 6's claim. `list_campaigns` is
    `low` risk, so a trial run reaches the *real* implementation rather than a
    mock — which is what makes the refusal below a refusal rather than a
    rehearsal that was never going to call anything.
    """
    return {
        "start_node_id": "act",
        "nodes": [
            {"id": "act", "type": "mcp", "config": {"tool_id": tool_id, "action": "list_campaigns"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [{"id": "e1", "source_node_id": "act", "source_port": "done", "target_node_id": "finish"}],
        "metadata": {},
    }


async def test_disabling_blocks_the_next_run_without_touching_the_saved_workflow(
    client: AsyncClient,
    creator_token: str,
    admin_token: str,
    mcp_server: Callable[..., None],
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
) -> None:
    """ADR-0023 §9, asserted as a run: the graph is untouched, the invocation is not."""
    mcp_server()
    approved = await approved_connection(client, creator_token, admin_token)
    version = solution_spec_version(
        seeded_creator,
        SPEC,
        status=VersionStatus.WORKFLOW_CONFIRMED,
        workflow=calls_the_connection(approved["tool_id"]),
    )

    disabled = await client.post(f"/api/v1/admin/tools/{approved['id']}/disable", headers=auth_header(admin_token))
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"

    run = await client.post(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/workflow-sandbox-runs",
        json={"message": "liệt kê chiến dịch giúp tôi"},
        headers=auth_header(creator_token),
    )

    assert run.status_code == 200, run.text
    results = parse_sse(run.text, event="tool_result")
    assert results, run.text
    assert results[0]["ok"] is False
    assert "disabled" in results[0]["result"]

    # And the saved workflow still names the connection: nothing was rewritten.
    saved = await client.get(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/workflow", headers=auth_header(creator_token)
    )
    assert saved.status_code == 200, saved.text
    node = next(entry for entry in saved.json()["workflow"]["nodes"] if entry["id"] == "act")
    assert node["config"]["tool_id"] == approved["tool_id"]


async def test_only_an_approved_connection_can_be_disabled(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()
    registered = (await register(client, creator_token)).json()

    response = await client.post(f"/api/v1/admin/tools/{registered['id']}/disable", headers=auth_header(admin_token))

    assert response.status_code == 409, response.text
    assert "approved" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# `allowed_actions` — Agent Builder's `allowed_tools`, at connection scope
# --------------------------------------------------------------------------- #


async def test_a_connection_offers_every_action_until_it_is_narrowed(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    """No allowlist is not an empty allowlist. The default has to be "everything"."""
    mcp_server()
    approved = await approved_connection(client, creator_token, admin_token)

    entry = next(row for row in await catalog(client, creator_token) if row["id"] == approved["tool_id"])
    assert sorted(action["name"] for action in entry["actions"]) == ["list_campaigns", "send_campaign"]


async def test_narrowing_removes_the_action_from_the_canvas_catalogue(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    """The one visible consequence: a creator cannot wire an `MCP` node to what was excluded.

    Asserted through `GET /workflow/tools` rather than through the row, because
    that endpoint is what the canvas dropdown is built from — the row being
    right and the catalogue being wrong is exactly the drift worth catching.
    """
    mcp_server()
    registered = (await register(client, creator_token)).json()

    narrowed = await narrow_to(client, registered["id"], creator_token, "list_campaigns")
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json()["allowed_actions"] == ["list_campaigns"]

    decided = await approve(client, registered["id"], admin_token)
    assert decided.status_code == 200, decided.text

    entry = next(row for row in await catalog(client, creator_token) if row["id"] == decided.json()["tool_id"])
    assert [action["name"] for action in entry["actions"]] == ["list_campaigns"]


async def test_the_review_queue_still_shows_what_was_left_out(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    """An admin approving a narrowing has to be able to see the narrowing."""
    mcp_server()
    registered = (await register(client, creator_token)).json()
    await narrow_to(client, registered["id"], creator_token, "list_campaigns")

    queue = await client.get("/api/v1/admin/tools", headers=auth_header(admin_token))
    assert queue.status_code == 200, queue.text
    row = next(entry for entry in queue.json() if entry["id"] == registered["id"])
    assert sorted(action["name"] for action in row["actions"]) == ["list_campaigns", "send_campaign"]
    assert row["allowed_actions"] == ["list_campaigns"]


async def test_narrowing_past_an_action_drops_its_mock_requirement(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    """`send_campaign` is the only action here that needs a mock, and it was excluded.

    Approval would otherwise be blocked on a sandbox answer for something no
    rehearsal can reach — a gate held shut by a promise nothing will collect.
    """
    mcp_server()
    registered = (await register(client, creator_token)).json()
    await narrow_to(client, registered["id"], creator_token, "list_campaigns")

    decided = await approve(client, registered["id"], admin_token)

    assert decided.status_code == 200, decided.text


async def test_an_action_the_server_never_reported_cannot_be_allowed(
    client: AsyncClient, creator_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()
    registered = (await register(client, creator_token)).json()

    response = await narrow_to(client, registered["id"], creator_token, "send_campaigns")

    assert response.status_code == 409, response.text
    assert "send_campaigns" in response.json()["detail"]


async def test_the_allowlist_is_frozen_once_an_admin_has_approved_it(
    client: AsyncClient, creator_token: str, admin_token: str, mcp_server: Callable[..., None]
) -> None:
    """Widening after approval would put an unreviewed action into live workflows."""
    mcp_server()
    approved = await approved_connection(client, creator_token, admin_token)

    response = await narrow_to(client, approved["id"], creator_token, "list_campaigns")

    assert response.status_code == 409, response.text


async def test_a_stranger_cannot_narrow_someone_elses_connection(
    client: AsyncClient, creator_token: str, second_creator_token: str, mcp_server: Callable[..., None]
) -> None:
    mcp_server()
    registered = (await register(client, creator_token)).json()

    response = await narrow_to(client, registered["id"], second_creator_token, "list_campaigns")

    assert response.status_code == 404


async def test_an_allowlist_may_be_supplied_at_registration(
    client: AsyncClient, creator_token: str, mcp_server: Callable[..., None]
) -> None:
    """For the creator who already knows their own server — `mock_response_examples`' precedent."""
    mcp_server()

    response = await register(client, creator_token, allowed_actions=["list_campaigns"])

    assert response.status_code == 201, response.text
    assert response.json()["allowed_actions"] == ["list_campaigns"]
