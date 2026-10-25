"""UC1-38 (#126) — `POST /agents/{id}/versions/{v}/sandbox/chat` (SSE) and
`GET .../sandbox/runs`, spec.md §19.

`get_pep_client` is overridden with a fake that always `allow`s (same shape
`tests/test_policy/test_pep.py` scripts `httpx.post` with) — no live OPA
sidecar in this suite. `get_vault_store` is overridden with the in-memory fake
from `tests.test_api.test_connection_routes`.
"""

import json
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage

from src.db.models import UC1AgentSpecVersion, UC1PendingAction
from src.main import app
from src.policy.pep import AuthzRequest, Decision, Effect, get_pep_client
from src.services.llm import get_chat_model_factory
from src.services.vault_secret_store import get_vault_store
from tests.conftest import ScriptedToolModel, auth_header
from tests.test_agentspec.conftest import spec_payload
from tests.test_api.test_connection_routes import _FakeVault

pytestmark = pytest.mark.asyncio


def _always_allow(request: AuthzRequest) -> Decision:
    return Decision(effect=Effect.ALLOW, reasons=[], row_filter={}, approver_roles=[], redact_fields=[], max_rows=100)


def _always_deny(reason: str):
    def _client(request: AuthzRequest) -> Decision:
        return Decision(effect=Effect.DENY, reasons=[reason], row_filter={}, approver_roles=[], redact_fields=[], max_rows=0)

    return _client


@pytest.fixture(autouse=True)
def _fake_vault_and_pep():
    app.dependency_overrides[get_vault_store] = lambda: _FakeVault()
    app.dependency_overrides[get_pep_client] = lambda: _always_allow
    yield
    app.dependency_overrides.pop(get_vault_store, None)
    app.dependency_overrides.pop(get_pep_client, None)


def _install_model(responses: list[AIMessage]) -> None:
    model = ScriptedToolModel(responses=responses)
    app.dependency_overrides[get_chat_model_factory] = lambda: (lambda model_id: model)


@pytest.fixture(autouse=True)
def _reset_model_factory():
    yield
    app.dependency_overrides.pop(get_chat_model_factory, None)


def _seed_spec_version(db_session, *, created_by_id: int, version: int = 1, **overrides: Any) -> None:
    payload = spec_payload(metadata={**spec_payload()["metadata"], "version": version}, **overrides)
    db_session.add(
        UC1AgentSpecVersion(agent_id="agt_ar_assistant", version=version, spec=payload, created_by_id=created_by_id)
    )
    db_session.commit()


def _agent_admin_user_id(agent_admin_token: str) -> int:
    """The `sub` claim of an already-issued token — cheaper than a lookup call for a test fixture's own bookkeeping."""
    import jwt as _jwt

    from src.config import get_settings

    claims = _jwt.decode(agent_admin_token, get_settings().jwt_secret, algorithms=["HS256"])
    return int(claims["sub"])


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


async def test_a_full_turn_streams_a_transcript_with_a_nonzero_cost(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id)
    _install_model(
        [
            AIMessage(content="", tool_calls=[{"name": "erp.query_receivables", "args": {}, "id": "c1"}]),
            AIMessage(content="7 khách quá hạn trên 30 ngày, nguồn v_ar_open."),
        ]
    )

    response = await client.post(
        "/api/v1/agents/agt_ar_assistant/versions/1/sandbox/chat",
        json={"message": "Khách nào quá hạn trên 30 ngày?"},
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    assert events[0]["event"] == "thread"
    assert events[0]["data"]["thread_id"].startswith("sandbox:agt_ar_assistant:1:")
    tool_steps = [e for e in events if e["event"] == "step" and e["data"]["tool"] == "erp.query_receivables"]
    assert len(tool_steps) == 1
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["total_cost_usd"] > 0


async def test_the_run_is_persisted_and_listed_by_sandbox_runs(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, version=2)
    _install_model([AIMessage(content="Không có công nợ quá hạn.")])

    chat = await client.post(
        "/api/v1/agents/agt_ar_assistant/versions/2/sandbox/chat",
        json={"message": "Có ai quá hạn không?"},
        headers=auth_header(agent_admin_token),
    )
    assert chat.status_code == 200, chat.text

    runs = await client.get(
        "/api/v1/agents/agt_ar_assistant/versions/2/sandbox/runs", headers=auth_header(agent_admin_token)
    )
    assert runs.status_code == 200, runs.text
    body = runs.json()
    assert len(body) == 1
    assert body[0]["message"] == "Có ai quá hạn không?"
    assert body[0]["status"] == "completed"
    assert body[0]["final_answer"] == "Không có công nợ quá hạn."


async def test_a_missing_version_is_a_404_not_a_broken_stream(client: AsyncClient, agent_admin_token: str) -> None:
    response = await client.post(
        "/api/v1/agents/agt_never_created/versions/1/sandbox/chat",
        json={"message": "bất kỳ"},
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 404


async def test_exceeding_the_session_budget_stops_the_turn_with_a_structured_error(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, version=3)
    app.dependency_overrides[get_pep_client] = lambda: _always_deny("session tool-call budget exhausted (40)")
    _install_model([AIMessage(content="", tool_calls=[{"name": "erp.query_receivables", "args": {}, "id": "c1"}])])

    response = await client.post(
        "/api/v1/agents/agt_ar_assistant/versions/3/sandbox/chat",
        json={"message": "hỏi liên tục"},
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    error_event = next(e for e in events if e["event"] == "error")
    assert error_event["data"]["code"] == "budget_exceeded"
    assert "budget exhausted" in error_event["data"]["detail"]


async def test_only_agent_admin_may_open_a_sandbox_chat(client: AsyncClient, creator_token: str) -> None:
    response = await client.post(
        "/api/v1/agents/agt_ar_assistant/versions/1/sandbox/chat",
        json={"message": "bất kỳ"},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 403


def _always_require_approval(request: AuthzRequest) -> Decision:
    return Decision(
        effect=Effect.REQUIRE_APPROVAL, reasons=[], row_filter={}, approver_roles=["sales_manager"], redact_fields=[], max_rows=0
    )


async def test_a_require_approval_write_is_queued_and_never_touches_smtp(
    client: AsyncClient, agent_admin_token: str, db_session, smtp_that_must_never_be_reached: None
) -> None:
    """UC1-21 (#137), FR-42: a turn that hits `require_approval` on a write tool
    writes a `uc1_pending_actions` row — via the sandbox endpoint's own turn,
    not a hand-built graph state — and the real SMTP send never runs, proven
    by `smtp_that_must_never_be_reached` exploding if it ever did.
    """
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(
        db_session,
        created_by_id=user_id,
        version=4,
        capabilities=[{"tool": "mail.send_reminder", "scope": "write", "requires_approval": True}],
        authority={"autonomy": "propose_then_act", "budget": {"max_tool_calls_per_session": 40}},
    )
    app.dependency_overrides[get_pep_client] = lambda: _always_require_approval
    _install_model(
        [AIMessage(content="", tool_calls=[{"name": "mail.send_reminder", "args": {"customer_id": "cust-z"}, "id": "c1"}])]
    )

    response = await client.post(
        "/api/v1/agents/agt_ar_assistant/versions/4/sandbox/chat",
        json={"message": "gửi email nhắc nợ cho cust-z"},
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 200, response.text
    row = (
        db_session.query(UC1PendingAction)
        .filter(UC1PendingAction.agent_id == "agt_ar_assistant", UC1PendingAction.version == 4)
        .one()
    )
    assert row.tool == "mail.send_reminder"
    assert row.args == {"customer_id": "cust-z"}
    assert row.requested_by_id == user_id
    assert row.status.value == "pending"
