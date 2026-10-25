"""UC1-17 (#133) — `POST /agents/{id}/versions/{v}/golden-set/run` and
`POST /agents/{id}/versions/{v}/approve`, spec.md §19.
"""

import json
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage

from src.auth.passwords import hash_password
from src.db.models import UC1AgentSpecVersion, UC1EvalRun, User, UserRole
from src.main import app
from src.policy.pep import AuthzRequest, Decision, Effect, get_pep_client
from src.services.llm import get_chat_model_factory, meta_agent_model
from src.services.vault_secret_store import get_vault_store
from tests.conftest import ScriptedToolModel, auth_header, user_id_of
from tests.test_agentspec.conftest import spec_payload
from tests.test_api.test_connection_routes import _FakeVault

pytestmark = pytest.mark.asyncio


def _always_allow(request: AuthzRequest) -> Decision:
    return Decision(effect=Effect.ALLOW, reasons=[], row_filter={}, approver_roles=[], redact_fields=[], max_rows=100)


@pytest.fixture(autouse=True)
def _fake_vault_and_pep():
    app.dependency_overrides[get_vault_store] = lambda: _FakeVault()
    app.dependency_overrides[get_pep_client] = lambda: _always_allow
    yield
    app.dependency_overrides.pop(get_vault_store, None)
    app.dependency_overrides.pop(get_pep_client, None)


@pytest.fixture(autouse=True)
def _reset_model_factory():
    yield
    app.dependency_overrides.pop(get_chat_model_factory, None)


@pytest.fixture(autouse=True)
def _clean_up_second_admin(db_session):
    """`_second_agent_admin_token` creates a `User` referenced by a
    `UC1VersionApproval` row (FK, no cascade) — an unrelated test module's own
    "delete every `@example.com` account" cleanup (`test_registration.py`,
    `test_auth_register.py`) would otherwise abort on that foreign key and
    leave every one of *their* leftover rows in place too. Deleting both here,
    in dependency order, keeps that damage from ever happening.
    """
    yield
    from src.db.models import UC1VersionApproval

    admin = db_session.query(User).filter(User.email == "second-agent-admin@example.com").one_or_none()
    if admin is not None:
        db_session.query(UC1VersionApproval).filter(UC1VersionApproval.admin_id == admin.id).delete()
        db_session.delete(admin)
        db_session.commit()


def _install_golden_set_models(*, judge_score: float = 1.0) -> None:
    """A model factory that answers plainly for the agent con and returns a
    scripted DeepEval verdict for the judge — dispatched by *which model id*
    is requested, the same distinction `src.services.llm._build_chat_model`
    itself makes, so `run_uc1_golden_set` passing the same factory for both
    roles (`model_factory`/`judge_model_factory`) still gets two different
    scripts.
    """
    from src.evals.uc1_golden_set import load_golden_set

    cases = load_golden_set()
    agent_model = ScriptedToolModel(responses=[AIMessage(content="Không có thông tin phù hợp.") for _ in cases])
    verdict = json.dumps(
        {"faithfulness": {"score": judge_score, "reason": "t"}, "answer_relevancy": {"score": judge_score, "reason": "t"}}
    )
    judge_count = sum(1 for case in cases if case.judge.value == "llm")
    judge_model = ScriptedToolModel(responses=[AIMessage(content=verdict) for _ in range(judge_count)])

    def factory(model_id: str):
        return judge_model if model_id == meta_agent_model() else agent_model

    app.dependency_overrides[get_chat_model_factory] = lambda: factory


def _seed_spec_version(
    db_session, *, created_by_id: int, agent_id: str, version: int = 1, status_: str = "draft", **overrides: Any
) -> None:
    payload = spec_payload(
        metadata={**spec_payload()["metadata"], "id": agent_id, "version": version, "status": status_}, **overrides
    )
    db_session.add(UC1AgentSpecVersion(agent_id=agent_id, version=version, spec=payload, created_by_id=created_by_id))
    db_session.commit()


async def _second_agent_admin_token(client: AsyncClient, db_session) -> str:
    password = "correcthorsebatterystaple"
    user = User(
        email="second-agent-admin@example.com",
        password_hash=hash_password(password),
        role=UserRole.AGENT_ADMIN,
        employee_id="E999",
        department="IT",
    )
    db_session.add(user)
    db_session.commit()
    response = await client.post("/api/v1/auth/login", json={"email": user.email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def test_running_the_golden_set_persists_an_eval_run_and_returns_every_case(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_golden_run")
    _install_golden_set_models()

    response = await client.post(
        "/api/v1/agents/agt_golden_run/versions/1/golden-set/run", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_count"] == 23
    assert len(body["cases"]) == 23
    assert body["pass_rate"] == body["passed_count"] / body["total_count"]

    run = (
        db_session.query(UC1EvalRun)
        .filter(UC1EvalRun.agent_id == "agt_golden_run", UC1EvalRun.version == 1)
        .one()
    )
    assert run.total_count == 23
    assert len(run.case_results) == 23


async def test_a_missing_version_is_a_404(client: AsyncClient, agent_admin_token: str) -> None:
    response = await client.post(
        "/api/v1/agents/agt_never_created/versions/1/golden-set/run", headers=auth_header(agent_admin_token)
    )
    assert response.status_code == 404


async def test_approving_with_quorum_one_transitions_immediately(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_approve_q1", status_="pending_approval")

    response = await client.post(
        "/api/v1/agents/agt_approve_q1/versions/1/approve", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["approvals_recorded"] == 1
    assert body["approval_quorum"] == 1


async def test_approving_twice_as_the_same_admin_does_not_satisfy_a_quorum_of_two(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    """UC1-17 (#133) AC: "cùng một người duyệt hai lần không đủ quorum"."""
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(
        db_session,
        created_by_id=user_id,
        agent_id="agt_approve_same_admin_twice",
        status_="pending_approval",
        authority={"autonomy": "propose_then_act", "approval_quorum": 2, "budget": {"max_tool_calls_per_session": 40}},
    )

    first = await client.post(
        "/api/v1/agents/agt_approve_same_admin_twice/versions/1/approve", headers=auth_header(agent_admin_token)
    )
    second = await client.post(
        "/api/v1/agents/agt_approve_same_admin_twice/versions/1/approve", headers=auth_header(agent_admin_token)
    )

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["approvals_recorded"] == 1
    assert second.json()["approvals_recorded"] == 1
    assert second.json()["status"] == "pending_approval"


async def test_two_distinct_admins_satisfy_a_quorum_of_two(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(
        db_session,
        created_by_id=user_id,
        agent_id="agt_approve_two_admins",
        status_="pending_approval",
        authority={"autonomy": "propose_then_act", "approval_quorum": 2, "budget": {"max_tool_calls_per_session": 40}},
    )
    second_token = await _second_agent_admin_token(client, db_session)

    first = await client.post(
        "/api/v1/agents/agt_approve_two_admins/versions/1/approve", headers=auth_header(agent_admin_token)
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending_approval"

    second = await client.post(
        "/api/v1/agents/agt_approve_two_admins/versions/1/approve", headers=auth_header(second_token)
    )

    assert second.status_code == 200, second.text
    assert second.json()["status"] == "approved"
    assert second.json()["approvals_recorded"] == 2


async def test_approving_a_draft_version_is_a_409(client: AsyncClient, agent_admin_token: str, db_session) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_approve_draft", status_="draft")

    response = await client.post(
        "/api/v1/agents/agt_approve_draft/versions/1/approve", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 409, response.text
    assert any(problem["code"] == "invalid_transition" for problem in response.json()["detail"])
