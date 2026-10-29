""" "Chạy với tư cách…" — who a trial run or an eval case asks as. DS-05 / ADR-0059 §3.

On the platform the asker is whoever the creator picks; nothing overrides
`get_database_reader` here — the real reader is bound to the agent's owner and
asked by the request's own `run_as`. The Hệ CSDL, its views and its rules are
`test_data_query_node`'s, imported so both suites read the same database.
"""

# The Hệ CSDL fixtures are `test_data_query_node`'s, imported so pytest finds them by
# name — which ruff reads as each test redefining the import it receives.
# ruff: noqa: F811

import json
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage, SystemMessage
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.base import engine
from src.db.models import AgentVersion, EvalCase, SandboxRun, User, VersionStatus
from tests.conftest import ScriptedRun, auth_header, finished_eval_run, parse_sse
from tests.test_api.test_data_query_node import (  # noqa: F401 — fixtures are found by name
    OVERDUE,
    SPEC,
    _result,
    _views,
    _writes,
    source,
    version,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _trusted_test_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real reader reads the setting; the test database is local, so it has to be trusted."""
    monkeypatch.setattr(get_settings(), "database_source_trusted_hosts", str(engine.url.host))


async def _trial(client: AsyncClient, version: AgentVersion, token: str, **body: Any) -> Any:
    return await client.post(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/workflow-sandbox-runs",
        json={"message": "khách nào quá hạn trên 30 ngày?", **body},
        headers=auth_header(token),
    )


def _thread_of(response: Any) -> str:
    (thread,) = parse_sse(response.text, event="thread")
    return str(thread["conversation_id"])


# --------------------------------------------------------------------------- #
# A trial run asks as the person picked — and can change between turns
# --------------------------------------------------------------------------- #


async def test_switching_from_a_rep_to_their_lead_in_one_thread_changes_the_rows(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="C1")]])
    first = await _trial(client, version, creator_token, run_as="an@cty.vn")

    scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="C1, C3")]])
    second = await _trial(client, version, creator_token, run_as="binh@cty.vn", conversation_id=_thread_of(first))

    assert "C3" not in _result(first)["result"]
    assert "C3" in _result(second)["result"]


async def test_the_transcript_says_who_the_run_was_for(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    db_session: Session,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="C1")]])

    response = await _trial(client, version, creator_token, run_as="an@cty.vn")

    (call,) = [line for line in parse_sse(response.text, event="tool") if line["tool"] == "data_query"]
    assert call["args"]["run_as"] == "an@cty.vn"
    stored = (
        db_session.query(SandboxRun).filter(SandboxRun.version_id == version.id).order_by(SandboxRun.id.desc()).first()
    )
    assert stored is not None
    (line,) = [entry for entry in stored.transcript if entry["kind"] == "tool" and entry["tool"] == "data_query"]
    assert line["args"]["run_as"] == "an@cty.vn"


async def test_somebody_the_employee_view_does_not_know_is_told_no_permission_not_a_500(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    run = scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="Bạn không có quyền xem dữ liệu này.")]])

    response = await _trial(client, version, creator_token, run_as="nguoi.la@cty.vn")

    assert response.status_code == 200
    assert _result(response)["ok"] is False
    prompt = run[1][1].received_message_batches[0][0]
    assert isinstance(prompt, SystemMessage)
    assert "không có quyền" in str(prompt.content)


async def test_a_trial_with_nobody_picked_reads_nothing(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="…")]])

    result = _result(await _trial(client, version, creator_token))

    assert result["ok"] is False
    assert "C1" not in result["result"]


# --------------------------------------------------------------------------- #
# The list "Chạy với tư cách…" offers
# --------------------------------------------------------------------------- #


async def test_the_askers_offered_are_the_employee_views_own(
    client: AsyncClient, creator_token: str, version: AgentVersion
) -> None:
    response = await client.get(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/askers", headers=auth_header(creator_token)
    )

    assert response.status_code == 200, response.text
    assert [(row["value"], row["position"]) for row in response.json()] == [
        ("an@cty.vn", "Nhân viên KD"),
        ("binh@cty.vn", "Trưởng nhóm KD"),
    ]
    assert {row["source"] for row in response.json()} == {"ERP staging"}


@pytest.mark.parametrize(("q", "expected"), [("binh", ["binh@cty.vn"]), ("nhân viên", ["an@cty.vn"]), ("%", [])])
async def test_the_askers_narrow_by_email_or_position(
    client: AsyncClient, creator_token: str, version: AgentVersion, q: str, expected: list[str]
) -> None:
    response = await client.get(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/askers",
        params={"q": q},
        headers=auth_header(creator_token),
    )

    assert [row["value"] for row in response.json()] == expected


async def test_a_version_with_no_data_query_offers_nobody(
    client: AsyncClient,
    creator_token: str,
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
) -> None:
    plain = solution_spec_version(
        seeded_creator,
        SPEC,
        status=VersionStatus.WORKFLOW_CONFIRMED,
        workflow={
            "start_node_id": "answer",
            "nodes": [
                {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời.", "model": "gpt-4o-mini"}},
                {"id": "finish", "type": "end", "config": {}},
            ],
            "edges": [{"id": "e1", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"}],
            "metadata": {},
        },
    )

    response = await client.get(
        f"/api/v1/agents/{plain.agent_id}/versions/{plain.id}/askers", headers=auth_header(creator_token)
    )

    assert response.json() == []


# --------------------------------------------------------------------------- #
# An eval case asks as somebody too
# --------------------------------------------------------------------------- #


async def test_an_eval_case_keeps_who_it_asks_as_and_can_drop_it(
    client: AsyncClient, creator_token: str, version: AgentVersion
) -> None:
    base = f"/api/v1/agents/{version.agent_id}/versions/{version.id}/eval-cases"
    created = await client.post(
        base,
        json={
            "criterion": "Chỉ thấy khách của mình",
            "input_message": "xem hết khách quá hạn",
            "expected": "chỉ C1",
            "run_as": "an@cty.vn",
        },
        headers=auth_header(creator_token),
    )
    cleared = await client.patch(
        f"{base}/{created.json()['id']}", json={"run_as": ""}, headers=auth_header(creator_token)
    )

    assert created.json()["run_as"] == "an@cty.vn"
    assert cleared.json()["run_as"] is None


async def test_an_eval_case_runs_as_its_own_asker(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    db_session: Session,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """The ticket's own case: "nhân viên KD hỏi xem hết" passes when the rep sees only their own customers."""
    row = {
        "input": "cho tôi xem hết khách quá hạn",
        "expected": "chỉ khách của mình",
        "criterion": "Nhân viên KD chỉ thấy khách của mình",
        "category": "workflow_success",
        "expected_check": {"kind": "reaches_end"},
        "run_as": "an@cty.vn",
    }
    imported = await client.post(
        f"/api/v1/agents/{version.agent_id}/eval-suite/import",
        files={"file": ("bo-de.jsonl", json.dumps(row, ensure_ascii=False).encode(), "application/jsonl")},
        headers=auth_header(creator_token),
    )
    assert imported.status_code == 200, imported.text
    assert db_session.query(EvalCase).filter(EvalCase.run_as == "an@cty.vn").count() == 1
    scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="C1")]])

    response = await finished_eval_run(client, version.agent_id, version.id, creator_token)

    assert response.status_code == 200, response.text
    (result,) = response.json()["results"]
    (read,) = [item for item in result["evidence"] if item["tool"] == "data_query"]
    assert read["args"]["run_as"] == "an@cty.vn"
    assert "C1" in read["result"] and "C3" not in read["result"]
    assert result["passed"] is True


# --------------------------------------------------------------------------- #
# A trial that pauses for a person resumes as the same asker
# --------------------------------------------------------------------------- #


def _gated(solution_spec_version: Callable[..., AgentVersion], owner: User, source: Any) -> AgentVersion:
    """`HumanApproval → DataQuery → AgentLlm → End`: a trial that stops before it reads anything."""
    return solution_spec_version(
        owner,
        SPEC,
        status=VersionStatus.WORKFLOW_CONFIRMED,
        workflow={
            "start_node_id": "gate",
            "nodes": [
                {
                    "id": "gate",
                    "type": "human_approval",
                    "config": {"approver_role": "manager", "message_template": "Cho đọc công nợ?"},
                },
                {"id": "query", "type": "data_query", "config": {"source_id": str(source.id), "views": ["v_dqn_ar"]}},
                {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời.", "model": "gpt-4o-mini"}},
                {"id": "finish", "type": "end", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source_node_id": "gate", "source_port": "approved", "target_node_id": "query"},
                {"id": "e2", "source_node_id": "gate", "source_port": "rejected", "target_node_id": "finish"},
                {"id": "e3", "source_node_id": "query", "source_port": "done", "target_node_id": "answer"},
                {"id": "e4", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"},
            ],
            "metadata": {},
        },
    )


async def test_a_trial_resumed_after_approval_reads_as_the_same_person(
    client: AsyncClient,
    creator_token: str,
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
    source: Any,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """The second leg is a different request carrying only an approval id — `run_as` has to ride the parked row."""
    gated = _gated(solution_spec_version, seeded_creator, source)
    paused = await _trial(client, gated, creator_token, run_as="an@cty.vn")
    (waiting,) = parse_sse(paused.text, event="awaiting_approval")
    scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="C1")]])

    resumed = await client.post(
        f"/api/v1/agents/{gated.agent_id}/versions/{gated.id}/workflow-approvals/{waiting['approval_id']}",
        json={"approved": True, "reason": None},
        headers=auth_header(creator_token),
    )

    result = _result(resumed)
    assert result["ok"] is True
    assert "C1" in result["result"] and "C3" not in result["result"]


async def test_a_trial_a_pm_approves_resumes_as_the_same_person(
    client: AsyncClient,
    creator_token: str,
    pm_token: str,
    db_session: Session,
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
    source: Any,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """The PM control room resumes parked legs too — through the same reader, asked by the same person."""
    gated = _gated(solution_spec_version, seeded_creator, source)
    paused = await _trial(client, gated, creator_token, run_as="an@cty.vn")
    (waiting,) = parse_sse(paused.text, event="awaiting_approval")
    scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="C1")]])

    decided = await client.post(
        f"/api/v1/pm/agents/{gated.agent_id}/approvals/{waiting['approval_id']}",
        json={"approved": True},
        headers=auth_header(pm_token),
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["resumed"] is True
    db_session.expire_all()
    stored = (
        db_session.query(SandboxRun).filter(SandboxRun.version_id == gated.id).order_by(SandboxRun.id.desc()).first()
    )
    assert stored is not None
    (read,) = [entry for entry in stored.transcript if entry["kind"] == "tool_result" and entry["tool"] == "data_query"]
    assert read["ok"] is True
    assert "C1" in read["content"] and "C3" not in read["content"]
