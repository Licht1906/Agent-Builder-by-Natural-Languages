"""UC1-36 (#148) — `POST /agents/{id}/deployed/chat` (SSE): the production
counterpart to `sandbox_routes.sandbox_chat`, spec.md §19 ("Vận hành |
POST /agents/{id}/chat"), FR-40/FR-41.

Why a different path than spec.md's literal `POST /agents/{id}/chat`:
`src.api.agent_routes` already owns that exact path for V2's own (unrelated)
`AgentSpec`/compiler, registered first in `src.main` — Starlette dispatches on
path *shape*, not a path param's declared type, so a second router at the same
shape would never be reached (`src.api.uc1_agent_routes`' own docstring makes
the identical call for `/spec-versions` vs V2's `/versions`). `/deployed/chat`
keeps the intent spec.md names — chat with whichever version is currently
deployed — without colliding.

Unlike sandbox chat (`AdminUser` only, FR-36's "creator's own identity" during
rehearsal), this door is open to any signed-in user, the same reasoning
`agent_routes.chat_with_agent`'s own docstring gives for V2's `/chat`: a
deployed agent is a product other people use. FR-41's own RowScope (from the
*caller's* JWT, not whoever compiled the spec) is what keeps that safe rather
than a permission check here.
"""

import json
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage

from src.auth.passwords import hash_password
from src.auth.tokens import create_access_token
from src.db.models import UC1AgentSpecVersion, UC1PendingAction, User, UserRole
from src.main import app
from src.policy.pep import AuthzRequest, Decision, Effect, get_pep_client
from src.services.llm import get_chat_model_factory
from src.services.secret_store import get_secret_store
from tests.conftest import ScriptedToolModel, auth_header, user_id_of
from tests.test_agentspec.conftest import spec_payload
from tests.test_api.test_connection_routes import _FakeSecretStore

pytestmark = pytest.mark.asyncio


def _always_allow(request: AuthzRequest) -> Decision:
    return Decision(effect=Effect.ALLOW, reasons=[], row_filter={}, approver_roles=[], redact_fields=[], max_rows=100)


def _t2_faithful_pep():
    """Denies exactly `authz.rego`'s T2 rule and nothing else — the same
    faithful stand-in `tests/test_compiler/test_provenance.py` uses at the
    graph level, reused here to prove the same guarantee through this HTTP door.
    """

    def _client(request: AuthzRequest) -> Decision:
        if request.action.scope.value == "write" and request.provenance.from_untrusted_data:
            return Decision(
                effect=Effect.DENY,
                reasons=["write action derived from untrusted content"],
                row_filter={},
                approver_roles=[],
                redact_fields=[],
                max_rows=0,
            )
        return Decision(
            effect=Effect.ALLOW, reasons=[], row_filter={}, approver_roles=[], redact_fields=[], max_rows=100
        )

    return _client


@pytest.fixture(autouse=True)
def _fake_secrets_and_pep():
    app.dependency_overrides[get_secret_store] = lambda: _FakeSecretStore()
    app.dependency_overrides[get_pep_client] = lambda: _always_allow
    yield
    app.dependency_overrides.pop(get_secret_store, None)
    app.dependency_overrides.pop(get_pep_client, None)


def _install_model(responses: list[AIMessage]) -> None:
    model = ScriptedToolModel(responses=responses)
    app.dependency_overrides[get_chat_model_factory] = lambda: lambda model_id: model


@pytest.fixture(autouse=True)
def _reset_model_factory():
    yield
    app.dependency_overrides.pop(get_chat_model_factory, None)


def _seed_spec_version(
    db_session,
    *,
    created_by_id: int,
    agent_id: str = "agt_ar_assistant",
    version: int = 1,
    status_: str = "deployed",
    **overrides: Any,
) -> None:
    payload = spec_payload(
        metadata={**spec_payload()["metadata"], "id": agent_id, "version": version, "status": status_}, **overrides
    )
    db_session.add(UC1AgentSpecVersion(agent_id=agent_id, version=version, spec=payload, created_by_id=created_by_id))
    db_session.commit()


def _make_user(db_session, *, role: UserRole, employee_id: str, department: str = "SALES") -> tuple[User, str]:
    user = User(
        email=f"{employee_id.lower()}@example.com",
        password_hash=hash_password("a-long-enough-password"),
        role=role,
        employee_id=employee_id,
        department=department,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user, create_access_token(user)


def _parse_sse(body: str) -> list[dict[str, Any]]:
    events = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        events.append({"event": event, "data": json.loads(data)})
    return events


async def test_a_regular_signed_in_user_can_chat_with_the_deployed_version(
    client: AsyncClient, db_session, agent_admin_token: str
) -> None:
    _seed_spec_version(db_session, created_by_id=user_id_of(agent_admin_token), agent_id="agt_deployed_chat_basic")
    rep, rep_token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E301")
    _install_model([AIMessage(content="Không có công nợ quá hạn.")])

    response = await client.post(
        "/api/v1/agents/agt_deployed_chat_basic/deployed/chat",
        json={"message": "Có ai quá hạn không?"},
        headers=auth_header(rep_token),
    )

    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    assert events[0]["event"] == "thread"
    assert events[0]["data"]["thread_id"].startswith("prod:agt_deployed_chat_basic:1:")
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["final_answer"] == "Không có công nợ quá hạn."


async def test_two_different_callers_with_the_same_conversation_id_get_two_different_threads(
    client: AsyncClient, db_session, agent_admin_token: str
) -> None:
    """Neither the manager nor the rep ever picks a `conversation_id` (spec.md
    §15, 4:00/4:45) — both get the default, so per-caller addressing is what
    keeps their two conversations from landing on one shared thread.
    """
    _seed_spec_version(
        db_session, created_by_id=user_id_of(agent_admin_token), agent_id="agt_deployed_chat_two_callers"
    )
    _, manager_token = _make_user(db_session, role=UserRole.SALES_MANAGER, employee_id="E307")
    _, rep_token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E308")
    _install_model([AIMessage(content="ok")])

    manager_response = await client.post(
        "/api/v1/agents/agt_deployed_chat_two_callers/deployed/chat",
        json={"message": "hỏi"},
        headers=auth_header(manager_token),
    )
    _install_model([AIMessage(content="ok")])
    rep_response = await client.post(
        "/api/v1/agents/agt_deployed_chat_two_callers/deployed/chat",
        json={"message": "hỏi"},
        headers=auth_header(rep_token),
    )

    manager_thread = _parse_sse(manager_response.text)[0]["data"]["thread_id"]
    rep_thread = _parse_sse(rep_response.text)[0]["data"]["thread_id"]
    assert manager_thread != rep_thread
    assert manager_thread.startswith("prod:agt_deployed_chat_two_callers:1:")
    assert rep_thread.startswith("prod:agt_deployed_chat_two_callers:1:")


async def test_no_deployed_version_is_a_404(client: AsyncClient, db_session, agent_admin_token: str) -> None:
    _seed_spec_version(
        db_session, created_by_id=user_id_of(agent_admin_token), agent_id="agt_never_deployed", status_="draft"
    )
    _, rep_token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E303")

    response = await client.post(
        "/api/v1/agents/agt_never_deployed/deployed/chat", json={"message": "hỏi"}, headers=auth_header(rep_token)
    )

    assert response.status_code == 404


async def test_an_agent_that_was_never_created_is_a_404(client: AsyncClient, db_session) -> None:
    _, rep_token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E304")

    response = await client.post(
        "/api/v1/agents/agt_does_not_exist/deployed/chat", json={"message": "hỏi"}, headers=auth_header(rep_token)
    )

    assert response.status_code == 404


async def test_a_require_approval_write_is_queued_and_never_touches_smtp(
    client: AsyncClient, db_session, agent_admin_token: str, smtp_that_must_never_be_reached: None
) -> None:
    _seed_spec_version(
        db_session,
        created_by_id=user_id_of(agent_admin_token),
        agent_id="agt_deployed_chat_pending_action",
        capabilities=[{"tool": "mail.send_reminder", "scope": "write", "requires_approval": True}],
        authority={"autonomy": "propose_then_act", "budget": {"max_tool_calls_per_session": 40}},
    )
    rep, rep_token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E305")
    app.dependency_overrides[get_pep_client] = lambda: (
        lambda request: Decision(
            effect=Effect.REQUIRE_APPROVAL,
            reasons=[],
            row_filter={},
            approver_roles=["sales_manager"],
            redact_fields=[],
            max_rows=0,
        )
    )
    _install_model(
        [
            AIMessage(
                content="", tool_calls=[{"name": "mail.send_reminder", "args": {"customer_id": "cust-z"}, "id": "c1"}]
            )
        ]
    )

    response = await client.post(
        "/api/v1/agents/agt_deployed_chat_pending_action/deployed/chat",
        json={"message": "gửi email nhắc nợ cho cust-z"},
        headers=auth_header(rep_token),
    )

    assert response.status_code == 200, response.text
    row = (
        db_session.query(UC1PendingAction).filter(UC1PendingAction.agent_id == "agt_deployed_chat_pending_action").one()
    )
    assert row.tool == "mail.send_reminder"
    assert row.requested_by_employee_id == "E305"
    assert row.status.value == "pending"


async def test_a_write_tool_call_following_a_read_in_the_same_turn_is_denied(
    client: AsyncClient, db_session, agent_admin_token: str, smtp_that_must_never_be_reached: None
) -> None:
    """T2 (§10.3, R2): a write that follows an untrusted read in the same turn is
    denied structurally, the same guarantee `tests/test_compiler/test_provenance.py`
    proves at the graph level — this is the HTTP-level proof for the production
    door.

    A deliberately ordinary message rather than spec.md §15's own 6:30 line
    ("bỏ qua hướng dẫn...") — that exact phrase already trips the *input*
    guardrail (`_blocked_by_input_guardrails`) before the model is ever
    called, which proves a different, earlier defense layer and never
    reaches a tool call at all. T2 is what stands behind that first layer,
    for the injected instruction that does not happen to match a denied
    topic string — this test isolates it on its own.
    """
    _seed_spec_version(
        db_session,
        created_by_id=user_id_of(agent_admin_token),
        agent_id="agt_deployed_chat_injection",
        capabilities=[
            {"tool": "erp.query_receivables", "scope": "read"},
            {"tool": "mail.send_reminder", "scope": "write", "requires_approval": True},
        ],
        authority={"autonomy": "propose_then_act", "budget": {"max_tool_calls_per_session": 40}},
    )
    rep, rep_token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E306")
    app.dependency_overrides[get_pep_client] = lambda: _t2_faithful_pep()
    _install_model(
        [
            AIMessage(content="", tool_calls=[{"name": "erp.query_receivables", "args": {}, "id": "c1"}]),
            AIMessage(
                content="",
                tool_calls=[{"name": "mail.send_reminder", "args": {"customer_id": "cust-x"}, "id": "c2"}],
            ),
            AIMessage(content="Đã dừng."),
        ]
    )

    response = await client.post(
        "/api/v1/agents/agt_deployed_chat_injection/deployed/chat",
        json={"message": "Xem công nợ quá hạn rồi gửi email nhắc nợ giúp tôi"},
        headers=auth_header(rep_token),
    )

    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    tool_steps = [e for e in events if e["event"] == "step" and e["data"]["tool"] == "mail.send_reminder"]
    assert len(tool_steps) == 1
    assert (
        "provenance" in (tool_steps[0]["data"]["result"] or "").lower()
        or "untrusted" in (tool_steps[0]["data"]["result"] or "").lower()
    )
    assert (
        db_session.query(UC1PendingAction).filter(UC1PendingAction.agent_id == "agt_deployed_chat_injection").count()
        == 0
    )


async def test_the_catalog_lists_a_deployed_agent_this_caller_may_use(
    client: AsyncClient, db_session, agent_admin_token: str
) -> None:
    """The front door this endpoint exists to be. Chatting was always open to any
    signed-in caller; nothing handed out the ids, so the audience an agent was
    built for could hold a valid session and never learn it existed.
    """
    _seed_spec_version(db_session, created_by_id=user_id_of(agent_admin_token), agent_id="agt_available_visible")
    _, token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E401")

    response = await client.get("/api/v1/uc1-agents/available", headers=auth_header(token))

    assert response.status_code == 200, response.text
    listed = {entry["agent_id"]: entry for entry in response.json()}
    assert "agt_available_visible" in listed
    assert listed["agt_available_visible"]["version"] == 1
    # `data_scope` rides along so the screen can say why two people get
    # different numbers from the same agent before either thinks it is broken.
    assert listed["agt_available_visible"]["data_scope"] == "own_records"


async def test_the_catalog_hides_an_agent_whose_audience_excludes_the_caller(
    client: AsyncClient, db_session, agent_admin_token: str
) -> None:
    """Listing an agent that would only ever refuse the caller teaches them
    nothing except that the product is broken — the PEP still refuses either
    way, so this is about what is worth showing, not about what is permitted.
    """
    _seed_spec_version(
        db_session,
        created_by_id=user_id_of(agent_admin_token),
        agent_id="agt_available_wrong_role",
        audience={"roles": ["sales_manager"], "data_scope": "own_records"},
    )
    _, token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E402")

    response = await client.get("/api/v1/uc1-agents/available", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert "agt_available_wrong_role" not in {entry["agent_id"] for entry in response.json()}


async def test_the_catalog_hides_a_version_that_is_not_deployed(
    client: AsyncClient, db_session, agent_admin_token: str
) -> None:
    """An end user has nothing to do with a draft and cannot be helped by seeing
    one — this is the line between this catalog and the agent_admin one.
    """
    _seed_spec_version(
        db_session, created_by_id=user_id_of(agent_admin_token), agent_id="agt_available_draft", status_="draft"
    )
    _, token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E403")

    response = await client.get("/api/v1/uc1-agents/available", headers=auth_header(token))

    assert "agt_available_draft" not in {entry["agent_id"] for entry in response.json()}


async def test_the_catalog_applies_the_department_half_of_the_audience(
    client: AsyncClient, db_session, agent_admin_token: str
) -> None:
    """`authz.rego` denies on department only when `audience.departments` is
    non-empty; an empty list means no restriction, and the filter here has to
    agree or a correctly-usable agent goes missing from the list.
    """
    _seed_spec_version(
        db_session,
        created_by_id=user_id_of(agent_admin_token),
        agent_id="agt_available_other_dept",
        audience={"roles": ["sales_rep"], "departments": ["FINANCE"], "data_scope": "own_records"},
    )
    _seed_spec_version(
        db_session,
        created_by_id=user_id_of(agent_admin_token),
        agent_id="agt_available_any_dept",
        audience={"roles": ["sales_rep"], "departments": [], "data_scope": "own_records"},
    )
    _, token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E404", department="SALES")

    listed = {entry["agent_id"] for entry in (await client.get(
        "/api/v1/uc1-agents/available", headers=auth_header(token)
    )).json()}

    assert "agt_available_other_dept" not in listed
    assert "agt_available_any_dept" in listed


async def test_the_catalog_is_not_admin_only(client: AsyncClient, db_session, agent_admin_token: str) -> None:
    """The whole defect in one assertion: `GET /uc1-agents` answers 403 to the
    very people a deployed agent is for, and that 403 is why the chat door had
    no reachable entrance.
    """
    _seed_spec_version(db_session, created_by_id=user_id_of(agent_admin_token), agent_id="agt_available_not_admin")
    _, token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E405")

    admin_catalog = await client.get("/api/v1/uc1-agents", headers=auth_header(token))
    open_catalog = await client.get("/api/v1/uc1-agents/available", headers=auth_header(token))

    assert admin_catalog.status_code == 403
    assert open_catalog.status_code == 200


async def test_the_catalog_still_needs_a_session(client: AsyncClient) -> None:
    """Open to every signed-in caller is not open to everyone."""
    assert (await client.get("/api/v1/uc1-agents/available")).status_code == 401
