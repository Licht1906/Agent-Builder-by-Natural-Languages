"""The shared run ledger — ADR-0053 §1–§3: every run leaves a readable record.

Three claims:

1. **A production workflow turn writes `agent_runs` beside `run_costs`**, in
   one transaction, carrying the question, the answer, the transcript the
   browser saw, and the `trace_id` the `thread` event announced first — so a
   thumbs from the chat window can find the row.
2. **The taxonomy is decided by code at write time**: the same error sentence
   files as the same `outcome` every time, and an unknown one stays `error`.
3. **The transcript is summarised into the group-by columns** — with V2's
   tool-before-node ordering handled, V1's `step` lines understood, and UC-1's
   folded rows unfolded into the one flat shape.
"""

from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import AgentRun, AgentVersion, QualityScore, RunCost, VersionStatus
from src.models.schemas import SandboxTranscriptEntry
from src.pm.taxonomy import classify_error, summarize_transcript
from tests.conftest import ScriptedRun, auth_header, parse_sse, parse_sse_stream

pytestmark = pytest.mark.asyncio


def _answer(text: str) -> AIMessage:
    return AIMessage(content=text, usage_metadata=UsageMetadata(input_tokens=40, output_tokens=10, total_tokens=50))


def _agent_llm_only(prompt: str) -> dict[str, Any]:
    return {
        "start_node_id": "answer",
        "nodes": [
            {"id": "answer", "type": "agent_llm", "config": {"prompt": prompt, "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [{"id": "e1", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"}],
        "metadata": {},
    }


SPEC = {
    "goal": "Trả lời câu hỏi nội bộ",
    "users": ["Nhân viên"],
    "data": [{"kind": "user_input", "description": "Câu hỏi"}],
    "actions": ["Trả lời"],
    "expected_outcome": ["Câu trả lời đúng"],
    "success_criteria": [{"text": "Trả lời đúng việc được hỏi", "category": "correctness"}],
    "prohibitions": ["Không bịa"],
}


@pytest.fixture(autouse=True)
def _drop_ledger_rows(db_session: Session):
    yield
    db_session.rollback()
    for row in db_session.scalars(select(AgentRun)).all():
        db_session.delete(row)
    db_session.commit()


async def test_a_workflow_turn_leaves_a_record_beside_its_bill(
    client: AsyncClient,
    creator_token: str,
    seeded_creator,
    solution_spec_version: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    db_session: Session,
) -> None:
    version = solution_spec_version(
        seeded_creator, SPEC, status=VersionStatus.WORKFLOW_CONFIRMED, workflow=_agent_llm_only("Bạn là trợ lý."), name="Trợ lý"
    )
    scripted_chat_model_factory([[_answer("Xin chào, tôi giúp gì được?")]])
    response = await client.post(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/workflow-chat",
        json={"message": "xin chào"},
        headers=auth_header(creator_token),
    )
    assert response.status_code == 200, response.text
    thread = parse_sse(response.text, event="thread")[0]
    assert thread["trace_id"], "the thread event names the run before the answer arrives"

    run = db_session.scalars(select(AgentRun).where(AgentRun.trace_id == thread["trace_id"])).one()
    assert run.pipeline == "v2" and run.agent_key == str(version.agent_id) and run.version_key == str(version.id)
    assert run.agent_name == "Trợ lý" and run.input_message == "xin chào"
    assert run.answer == "Xin chào, tôi giúp gì được?" and run.outcome == "success"
    assert run.node_count == 2 and run.tool_calls == 0 and run.failure_node_type is None  # answer + end
    lines = [SandboxTranscriptEntry.model_validate(line) for line in run.transcript]
    assert [(line.kind, line.node_id, line.node_type) for line in lines] == [
        ("node", "answer", "agent_llm"),
        ("node", "finish", "end"),
    ]
    # The record and the bill are one commit, pointed at each other.
    bill = db_session.get(RunCost, run.run_cost_id)
    assert bill is not None and bill.trace_id == run.trace_id and bill.cost_usd == run.cost_usd
    assert run.input_tokens == bill.input_tokens == 40
    # The stream itself was not changed by the recording.
    names = [name for name, _ in parse_sse_stream(response.text)]
    assert names[0] == "thread" and "cost" in names and "node" in names
    # No tool ran, so no layer-one score — a check with nothing to check is skipped, not failed.
    assert db_session.scalar(select(QualityScore).where(QualityScore.run_id == run.id)) is None


def test_the_taxonomy_files_the_same_sentence_the_same_way() -> None:
    assert classify_error("Node 'gui_thu': tool_id trống — bước chưa được nối với kết nối nào") == "spec_error"
    assert classify_error("UnsupportedNodeError: node type 'foo'") == "spec_error"
    assert classify_error("Kết nối ERP hết hạn (HTTP 502) khi gọi query_overdue_invoices") == "system_error"
    assert classify_error("ReadTimeout: request timed out after 30s") == "system_error"
    assert classify_error("401 Unauthorized: token expired") == "system_error"
    assert classify_error("Hành động send_email bị chặn: người nhận ngoài miền tổ chức") == "guardrail_block"
    assert classify_error("RuntimeError: step 3 returned no output") == "error"
    assert classify_error(None) == "success"
    # A death the patterns do not know, after a tool that said why it failed: the tool's word wins.
    transcript = [{"kind": "tool_result", "node_id": "x", "tool": "erp.query", "ok": False, "content": "HTTP 503 Service Unavailable"}]
    assert classify_error("RuntimeError: boom", transcript=transcript) == "system_error"


def test_the_summary_reads_v2_tool_lines_that_arrive_before_their_node() -> None:
    transcript = [
        {"kind": "tool", "node_id": "tim", "tool": "gmail.search_emails", "args": {"q": "x"}, "simulated": False},
        {"kind": "tool_result", "node_id": "tim", "tool": "gmail.search_emails", "ok": False, "simulated": False, "content": "HTTP 500"},
        {"kind": "node", "node_id": "tim", "node_type": "mcp", "content": "{}"},
        {"kind": "node", "node_id": "re", "node_type": "if_else", "content": '{"holds": true}', "port_taken": "true"},
        {"kind": "node", "node_id": "noi", "node_type": "agent_llm", "content": "Không tìm thấy thư."},
    ]
    summary = summarize_transcript(transcript)
    assert summary.node_count == 3 and summary.tool_calls == 1 and summary.tool_failures == 1
    assert summary.actions == ["gmail.search_emails"]
    assert summary.first_failure == "tim:gmail.search_emails" and summary.failure_node_type == "mcp"
    assert summary.answer == "Không tìm thấy thư."  # the router's JSON is not the answer

    # A run that died with no failed tool: the last node is where it was.
    died = summarize_transcript(transcript[2:4], error="boom")
    assert died.first_failure == "re" and died.failure_node_type == "if_else" and died.answer is None

    # V1: every step speaks.
    v1 = summarize_transcript([{"kind": "step", "step_id": "s1", "title": "Bước", "content": "Xong."}])
    assert v1.node_count == 1 and v1.answer == "Xong."
