"""UC1-16 (#132) — `GET /approvals/queue`, `GET .../diff`, `GET .../dossier`, spec.md §19."""

from typing import Any

import pytest
from httpx import AsyncClient

from src.db.models import UC1AgentSpecVersion, UC1EvalRun, UC1SandboxRun, UC1SandboxRunStatus
from tests.conftest import auth_header, user_id_of
from tests.test_agentspec.conftest import spec_payload

pytestmark = pytest.mark.asyncio


def _seed_spec_version(
    db_session,
    *,
    created_by_id: int,
    agent_id: str,
    version: int = 1,
    status_: str = "draft",
    name: str | None = None,
    **overrides: Any,
) -> None:
    metadata = {**spec_payload()["metadata"], "id": agent_id, "version": version, "status": status_}
    if name is not None:
        metadata["name"] = name
    payload = spec_payload(metadata=metadata, **overrides)
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
            transcript=[{"node": "agent", "tool": None, "args": None, "result": "7 khách hàng.", "tokens": 5, "latency_ms": 1.0, "cost_usd": 0.001, "row_filter": None}],
            total_cost_usd=0.01,
            final_answer="7 khách hàng quá hạn.",
        )
    )
    db_session.commit()


def _seed_eval_run(db_session, *, agent_id: str, version: int, pass_rate: float = 0.95) -> None:
    db_session.add(
        UC1EvalRun(
            agent_id=agent_id,
            version=version,
            passed_count=round(pass_rate * 23),
            total_count=23,
            pass_rate=pass_rate,
            case_results=[{"case_id": "c1", "group": "refusal", "judge": "rule", "passed": True, "detail": "ok"}],
        )
    )
    db_session.commit()


async def test_diff_reports_exactly_the_field_that_changed(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_diff", version=1)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_diff", version=2, name="Tên mới")

    response = await client.get("/api/v1/agents/agt_diff/versions/2/diff", headers=auth_header(agent_admin_token))

    assert response.status_code == 200, response.text
    paths = [entry["path"] for entry in response.json()]
    assert paths == ["metadata.name"]


async def test_diff_defaults_against_to_version_minus_one(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_diff_default", version=1)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_diff_default", version=2)

    response = await client.get(
        "/api/v1/agents/agt_diff_default/versions/2/diff", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 200, response.text


async def test_diff_against_a_missing_version_is_a_404(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_diff_missing", version=1)

    response = await client.get(
        "/api/v1/agents/agt_diff_missing/versions/1/diff?against=99", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 404


async def test_dossier_embeds_the_latest_sandbox_transcript_and_eval_run(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    """UC1-16 (#132) AC: "hồ sơ duyệt hiển thị đúng transcript của lượt chat thử gần nhất"."""
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_dossier", version=1, status_="pending_approval")
    _seed_sandbox_run(db_session, agent_id="agt_dossier", version=1)
    _seed_eval_run(db_session, agent_id="agt_dossier", version=1, pass_rate=0.9)

    response = await client.get("/api/v1/agents/agt_dossier/versions/1/dossier", headers=auth_header(agent_admin_token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending_approval"
    assert body["sandbox_run"]["final_answer"] == "7 khách hàng quá hạn."
    assert body["eval_run"]["pass_rate"] == 0.9
    assert body["approval_quorum"] == 1
    assert body["approvals_recorded"] == 0


async def test_dossier_with_no_sandbox_run_or_eval_run_reports_both_as_null(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_dossier_empty", version=1)

    response = await client.get(
        "/api/v1/agents/agt_dossier_empty/versions/1/dossier", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sandbox_run"] is None
    assert body["eval_run"] is None
    assert body["diff_against_previous"] is None


async def test_queue_lists_only_pending_approval_versions_oldest_first(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_queue_draft", version=1, status_="draft")
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_queue_pending_1", version=1, status_="pending_approval")
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_queue_pending_2", version=1, status_="pending_approval")

    response = await client.get("/api/v1/approvals/queue", headers=auth_header(agent_admin_token))

    assert response.status_code == 200, response.text
    agent_ids = [row["agent_id"] for row in response.json()]
    assert "agt_queue_draft" not in agent_ids
    assert agent_ids.index("agt_queue_pending_1") < agent_ids.index("agt_queue_pending_2")


async def test_queue_row_flags_high_risk_write_tools(client: AsyncClient, agent_admin_token: str, db_session) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(
        db_session,
        created_by_id=user_id,
        agent_id="agt_queue_high_risk",
        version=1,
        status_="pending_approval",
        capabilities=[
            {"tool": "erp.query_receivables", "scope": "read"},
            {"tool": "mail.send_reminder", "scope": "write", "requires_approval": True},
        ],
        authority={"autonomy": "propose_then_act", "approval_quorum": 1, "budget": {"max_tool_calls_per_session": 40}},
    )

    response = await client.get("/api/v1/approvals/queue", headers=auth_header(agent_admin_token))

    row = next(row for row in response.json() if row["agent_id"] == "agt_queue_high_risk")
    assert row["high_risk_tools"] == ["mail.send_reminder"]


async def test_only_agent_admin_may_read_the_queue(client: AsyncClient, creator_token: str) -> None:
    response = await client.get("/api/v1/approvals/queue", headers=auth_header(creator_token))
    assert response.status_code == 403
