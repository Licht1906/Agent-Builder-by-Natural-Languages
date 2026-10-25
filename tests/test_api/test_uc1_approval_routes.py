"""UC1-39 (#127) — `POST /agents/{id}/versions/{v}/submit-for-approval`, spec.md §19, AC-11.

UC1-17 (#133) wired a real `deepeval_pass_rate` in on top of #127's own
`sandbox_run_count` condition — every test below that expects a 200 now also
seeds a `UC1EvalRun` row (`_seed_eval_run`), the same way it already seeds a
`UC1SandboxRun` row for FR-38.
"""

from typing import Any

import pytest
from httpx import AsyncClient

from src.agentspec.spec import Evaluation
from src.db.models import UC1AgentSpecVersion, UC1EvalRun, UC1SandboxRun, UC1SandboxRunStatus
from tests.conftest import auth_header
from tests.test_agentspec.conftest import spec_payload

pytestmark = pytest.mark.asyncio


def _agent_admin_user_id(agent_admin_token: str) -> int:
    """The `sub` claim of an already-issued token — cheaper than a lookup call for a test fixture's own bookkeeping."""
    import jwt as _jwt

    from src.config import get_settings

    claims = _jwt.decode(agent_admin_token, get_settings().jwt_secret, algorithms=["HS256"])
    return int(claims["sub"])


def _seed_spec_version(
    db_session, *, created_by_id: int, agent_id: str = "agt_ar_assistant", version: int = 1, **overrides: Any
) -> None:
    payload = spec_payload(
        metadata={**spec_payload()["metadata"], "id": agent_id, "version": version}, **overrides
    )
    db_session.add(UC1AgentSpecVersion(agent_id=agent_id, version=version, spec=payload, created_by_id=created_by_id))
    db_session.commit()


def _seed_sandbox_run(db_session, *, agent_id: str, version: int) -> None:
    db_session.add(
        UC1SandboxRun(
            agent_id=agent_id,
            version=version,
            principal_employee_id="E102",
            thread_id=f"sandbox:{agent_id}:{version}:default",
            message="Khách nào quá hạn trên 30 ngày?",
            status=UC1SandboxRunStatus.COMPLETED,
            transcript=[],
            total_cost_usd=0.01,
            final_answer="7 khách hàng quá hạn.",
        )
    )
    db_session.commit()


def _seed_eval_run(db_session, *, agent_id: str, version: int, pass_rate: float = 0.95) -> None:
    total = 23
    passed = round(pass_rate * total)
    db_session.add(
        UC1EvalRun(
            agent_id=agent_id,
            version=version,
            passed_count=passed,
            total_count=total,
            pass_rate=pass_rate,
            case_results=[],
        )
    )
    db_session.commit()


def _spec_row(db_session, *, agent_id: str, version: int) -> UC1AgentSpecVersion:
    return (
        db_session.query(UC1AgentSpecVersion)
        .filter(UC1AgentSpecVersion.agent_id == agent_id, UC1AgentSpecVersion.version == version)
        .one()
    )


async def test_submitting_a_version_with_no_sandbox_run_is_refused_with_a_structured_error(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    """AC-11: "gọi submit-for-approval trên version chưa từng sandbox → lỗi có cấu trúc, không chuyển trạng thái"."""
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_no_sandbox", version=1)

    response = await client.post(
        "/api/v1/agents/agt_no_sandbox/versions/1/submit-for-approval", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 409, response.text
    problems = response.json()["detail"]
    assert any(problem["code"] == "sandbox_run_missing" for problem in problems)
    assert _spec_row(db_session, agent_id="agt_no_sandbox", version=1).spec["metadata"]["status"] == "draft"


async def test_submitting_a_version_with_a_sandbox_run_transitions_to_pending_approval(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_with_sandbox", version=1)
    _seed_sandbox_run(db_session, agent_id="agt_with_sandbox", version=1)
    _seed_eval_run(db_session, agent_id="agt_with_sandbox", version=1)

    response = await client.post(
        "/api/v1/agents/agt_with_sandbox/versions/1/submit-for-approval", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending_approval"
    assert _spec_row(db_session, agent_id="agt_with_sandbox", version=1).spec["metadata"]["status"] == "pending_approval"


async def test_submitting_a_version_with_no_eval_run_is_refused_with_a_structured_error(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    """UC1-17 (#133): "chưa sandbox lần nào → submit-for-approval trả lỗi có cấu trúc" —
    same shape for a version that has sandboxed but never run the golden set.
    """
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_no_eval", version=1)
    _seed_sandbox_run(db_session, agent_id="agt_no_eval", version=1)

    response = await client.post(
        "/api/v1/agents/agt_no_eval/versions/1/submit-for-approval", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 409, response.text
    problems = response.json()["detail"]
    assert any(problem["code"] == "deepeval_below_threshold" for problem in problems)


async def test_submitting_a_version_below_the_golden_set_pass_rate_is_refused(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    """UC1-17 (#133): "golden set dưới ngưỡng → submit-for-approval trả lỗi có cấu trúc".

    The seeded rate is derived from `Evaluation().min_pass_rate` rather than
    written as a literal: what this test is about is a run *below the bar* being
    refused with a structured problem list, and pinning a number here made that
    claim quietly depend on the bar never moving.
    """
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_low_eval", version=1)
    _seed_sandbox_run(db_session, agent_id="agt_low_eval", version=1)
    _seed_eval_run(db_session, agent_id="agt_low_eval", version=1, pass_rate=Evaluation().min_pass_rate - 0.05)

    response = await client.post(
        "/api/v1/agents/agt_low_eval/versions/1/submit-for-approval", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 409, response.text
    problems = response.json()["detail"]
    assert any(problem["code"] == "deepeval_below_threshold" for problem in problems)


async def test_submitting_an_already_pending_version_again_is_refused(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_double_submit", version=1)
    _seed_sandbox_run(db_session, agent_id="agt_double_submit", version=1)
    _seed_eval_run(db_session, agent_id="agt_double_submit", version=1)

    first = await client.post(
        "/api/v1/agents/agt_double_submit/versions/1/submit-for-approval", headers=auth_header(agent_admin_token)
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        "/api/v1/agents/agt_double_submit/versions/1/submit-for-approval", headers=auth_header(agent_admin_token)
    )

    assert second.status_code == 409, second.text
    assert any(problem["code"] == "invalid_transition" for problem in second.json()["detail"])


async def test_a_missing_version_is_a_404(client: AsyncClient, agent_admin_token: str) -> None:
    response = await client.post(
        "/api/v1/agents/agt_never_created/versions/1/submit-for-approval", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 404


async def test_only_agent_admin_may_submit_for_approval(
    client: AsyncClient, creator_token: str, agent_admin_token: str, db_session
) -> None:
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_forbidden", version=1)

    response = await client.post(
        "/api/v1/agents/agt_forbidden/versions/1/submit-for-approval", headers=auth_header(creator_token)
    )

    assert response.status_code == 403
