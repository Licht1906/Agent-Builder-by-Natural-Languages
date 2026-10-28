"""The whole road from a confirmed draft to the admin's approval queue, in the
order the UI walks it.

Every hop here already had its own unit-level test; what none of them covered
is the *chain*, which is exactly where this platform was broken: a version
could be confirmed and then sat at `draft` forever, because `GET
/approvals/queue` lists `pending_approval` only and the three conditions
between the two states (`src.agentspec.lifecycle.submit_for_approval`) had no
caller. A green run of this file is the guarantee that a spec drafted by the
meta-agent can actually reach an approver.
"""

import json
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage

from src.main import app
from src.policy.pep import AuthzRequest, Decision, Effect, get_pep_client
from src.services.llm import get_chat_model_factory, meta_agent_model
from src.services.secret_store import get_secret_store
from tests.conftest import ScriptedToolModel, auth_header
from tests.test_agentspec.conftest import spec_payload
from tests.test_api.test_connection_routes import _FakeSecretStore

pytestmark = pytest.mark.asyncio


def _always_allow(request: AuthzRequest) -> Decision:
    return Decision(effect=Effect.ALLOW, reasons=[], row_filter={}, approver_roles=[], redact_fields=[], max_rows=100)


@pytest.fixture(autouse=True)
def _fake_secrets_and_pep():
    app.dependency_overrides[get_secret_store] = lambda: _FakeSecretStore()
    app.dependency_overrides[get_pep_client] = lambda: _always_allow
    yield
    app.dependency_overrides.pop(get_secret_store, None)
    app.dependency_overrides.pop(get_pep_client, None)


@pytest.fixture(autouse=True)
def _reset_model_factory():
    yield
    app.dependency_overrides.pop(get_chat_model_factory, None)


def _install_chat_model(reply: str) -> None:
    model = ScriptedToolModel(responses=[AIMessage(content=reply)])
    app.dependency_overrides[get_chat_model_factory] = lambda: (lambda model_id: model)


def _install_golden_set_models() -> None:
    """Agent answers plainly, judge returns a perfect verdict — the same
    two-scripts-one-factory split `test_uc1_golden_set_and_approve_routes`
    uses, dispatched on the model id the caller asks for.
    """
    from src.evals.uc1_golden_set import load_golden_set

    cases = load_golden_set()
    agent_model = ScriptedToolModel(responses=[AIMessage(content="Không có thông tin phù hợp.") for _ in cases])
    verdict = json.dumps(
        {"faithfulness": {"score": 1.0, "reason": "t"}, "answer_relevancy": {"score": 1.0, "reason": "t"}}
    )
    judge_count = sum(1 for case in cases if case.judge.value == "llm")
    judge_model = ScriptedToolModel(responses=[AIMessage(content=verdict) for _ in range(judge_count)])

    def factory(model_id: str):
        return judge_model if model_id == meta_agent_model() else agent_model

    app.dependency_overrides[get_chat_model_factory] = lambda: factory


def _draft(agent_id: str, **overrides: Any) -> dict[str, Any]:
    return spec_payload(metadata={**spec_payload()["metadata"], "id": agent_id}, **overrides)


async def test_a_confirmed_draft_can_reach_the_admin_approval_queue(
    client: AsyncClient, agent_admin_token: str
) -> None:
    agent_id = "agt_flow_to_queue"
    headers = auth_header(agent_admin_token)
    # `min_pass_rate` 0.0 for the rehearsal's own reason: the canned reply this
    # test scripts satisfies one golden-set case, not all of them, and the
    # score itself is a real-model concern proved elsewhere. What this file is
    # proving is that the *gate reads the record*, not what the record says.
    draft = _draft(agent_id, evaluation={"min_pass_rate": 0.0})

    confirmed = await client.post(f"/api/v1/agents/{agent_id}/spec-versions", json=draft, headers=headers)
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["status"] == "draft"

    # The catalog is the only thing that hands out the `(agent_id, version)`
    # every hop below is addressed by.
    catalog = await client.get("/api/v1/uc1-agents", headers=headers)
    assert agent_id in [row["agent_id"] for row in catalog.json()]

    # A draft is not in the queue, and asking to be refuses — with the reason.
    empty_queue = await client.get("/api/v1/approvals/queue", headers=headers)
    assert agent_id not in [row["agent_id"] for row in empty_queue.json()]

    too_early = await client.post(f"/api/v1/agents/{agent_id}/versions/1/submit-for-approval", headers=headers)
    assert too_early.status_code == 409, too_early.text
    assert "sandbox_run_missing" in [problem["code"] for problem in too_early.json()["detail"]]

    # FR-36: one rehearsal turn.
    _install_chat_model("Có 3 khách quá hạn trên 30 ngày, nguồn v_ar_open.")
    chat = await client.post(
        f"/api/v1/agents/{agent_id}/versions/1/sandbox/chat",
        json={"message": "Khách nào đang quá hạn trên 30 ngày?"},
        headers=headers,
    )
    assert chat.status_code == 200, chat.text

    runs = await client.get(f"/api/v1/agents/{agent_id}/versions/1/sandbox/runs", headers=headers)
    assert len(runs.json()) == 1, runs.text

    # FR-33: the record the gate reads.
    _install_golden_set_models()
    graded = await client.post(f"/api/v1/agents/{agent_id}/versions/1/golden-set/run", headers=headers)
    assert graded.status_code == 200, graded.text
    assert graded.json()["total_count"] > 0

    # Now the gate opens.
    submitted = await client.post(f"/api/v1/agents/{agent_id}/versions/1/submit-for-approval", headers=headers)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "pending_approval"

    queue = await client.get("/api/v1/approvals/queue", headers=headers)
    entry = next(row for row in queue.json() if row["agent_id"] == agent_id)
    assert entry["version"] == 1
    assert entry["sandbox_run_count"] == 1


async def test_the_gate_reports_every_failing_condition_at_once(
    client: AsyncClient, agent_admin_token: str
) -> None:
    """§13: "thiếu bất kỳ điều kiện nào thì ở lại draft, không có nhánh lỗi
    riêng" — the screen renders this list, so it has to be the whole list.
    """
    agent_id = "agt_flow_all_problems"
    headers = auth_header(agent_admin_token)
    # Default `min_pass_rate` (0.9) and no golden-set run: two conditions fail.
    await client.post(f"/api/v1/agents/{agent_id}/spec-versions", json=_draft(agent_id), headers=headers)

    refused = await client.post(f"/api/v1/agents/{agent_id}/versions/1/submit-for-approval", headers=headers)

    assert refused.status_code == 409, refused.text
    problems = refused.json()["detail"]
    codes = [problem["code"] for problem in problems]
    assert "sandbox_run_missing" in codes
    assert len(codes) >= 2, codes
    assert all(problem["message"] for problem in problems)


async def test_the_catalog_shows_the_status_move_after_submitting(
    client: AsyncClient, agent_admin_token: str
) -> None:
    """The catalog is what the agent list renders — a version that moved on
    must not still read `draft` there.
    """
    agent_id = "agt_flow_catalog_status"
    headers = auth_header(agent_admin_token)
    await client.post(
        f"/api/v1/agents/{agent_id}/spec-versions",
        json=_draft(agent_id, evaluation={"min_pass_rate": 0.0}),
        headers=headers,
    )

    _install_chat_model("ok")
    await client.post(
        f"/api/v1/agents/{agent_id}/versions/1/sandbox/chat", json={"message": "thử"}, headers=headers
    )
    _install_golden_set_models()
    await client.post(f"/api/v1/agents/{agent_id}/versions/1/golden-set/run", headers=headers)
    await client.post(f"/api/v1/agents/{agent_id}/versions/1/submit-for-approval", headers=headers)

    catalog = await client.get("/api/v1/uc1-agents", headers=headers)

    entry = next(row for row in catalog.json() if row["agent_id"] == agent_id)
    assert entry["status"] == "pending_approval"
