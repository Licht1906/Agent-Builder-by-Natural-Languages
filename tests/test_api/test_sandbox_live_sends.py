"""The trial's "Gửi thật" switch — DS-09 / ADR-0059 Hệ quả.

A deliberate exception to V2's rule that a rehearsal mocks every risky
action: for one run, a creator may let the actions a person approved — the
exact payload, DS-08 — go out for real through their own connection. What is
held here is the ticket's four acceptance criteria, plus the two edges that
keep the exception narrow: an action nobody had to approve stays a mock, and
the run is still recorded as a rehearsal, flagged, never as production.

Gmail itself is replaced at `src.workflow.google_tools` by a recorder (as in
`test_action_payload_binding`), so "sent" and "not sent" are observed.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import AgentRun, AgentVersion, RunTrigger, User, VersionStatus
from src.workflow import google_tools
from src.workflow.tools import GMAIL
from tests.conftest import ScriptedRun, auth_header, parse_sse
from tests.test_api.test_action_payload_binding import APPROVED_ARGS, ASKED, draft_approve_send, fills
from tests.test_api.test_workflow_approval import SPEC, answers, decide, trial_workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

SEND = f"{GMAIL}.send_email"


@pytest.fixture
def gmail(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str, dict[str, Any]]]:
    """Every real Gmail call, `(agent_id, action, args)` — whose account it borrows is the agent's owner's."""
    sent: list[tuple[int, str, dict[str, Any]]] = []
    asked_for: list[int] = []

    def as_owner(agent_id: int, call: Callable[[dict[str, str]], str]) -> str:
        asked_for.append(agent_id)
        return call({})

    def recorded(action: str, args: Any, headers: Any) -> str:
        sent.append((asked_for[-1], action, dict(args)))
        return json.dumps({"message_id": "m-1", "sent": True})

    monkeypatch.setattr(google_tools, "_as_owner", as_owner)
    monkeypatch.setattr(google_tools, "_gmail", recorded)
    return sent


@pytest.fixture
def trialled(
    solution_spec_version: Callable[..., AgentVersion], seeded_creator: User
) -> Callable[[dict[str, Any]], AgentVersion]:
    def _make(workflow: dict[str, Any]) -> AgentVersion:
        return solution_spec_version(seeded_creator, SPEC, status=VersionStatus.WORKFLOW_CONFIRMED, workflow=workflow)

    return _make


async def _paused(
    client: AsyncClient, version: AgentVersion, token: str, scripted: Callable[..., ScriptedRun], **body: Any
) -> dict[str, Any]:
    """One trial turn up to its approval card."""
    scripted([[answers("Bản nháp thư.")], [fills(**APPROVED_ARGS)]])
    response = await trial_workflow(client, version, ASKED, token, **body)
    assert response.status_code == 200, response.text
    [card] = parse_sse(response.text, event="awaiting_approval")
    return card


def _send_lines(response: Any, event: str) -> list[dict[str, Any]]:
    return [line for line in parse_sse(response.text, event=event) if line["tool"] == SEND]


async def test_off_the_approved_email_is_mocked_as_before(
    client: AsyncClient,
    creator_token: str,
    trialled: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[..., ScriptedRun],
    gmail: list[tuple[int, str, dict[str, Any]]],
) -> None:
    """AC: "tắt → mail mô phỏng như cũ"."""
    version = trialled(draft_approve_send())
    card = await _paused(client, version, creator_token, scripted_chat_model_factory)

    scripted_chat_model_factory([[answers("Đã gửi (mô phỏng).")]])
    resumed = await decide(client, version, card["approval_id"], creator_token, approved=True)

    assert card["live_sends"] is False
    assert gmail == []
    [call] = _send_lines(resumed, "tool")
    assert call["simulated"] is True
    assert "live_in_sandbox" not in call


async def test_on_and_approved_the_email_reaches_the_real_mailbox_from_the_creators_account(
    client: AsyncClient,
    creator_token: str,
    trialled: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[..., ScriptedRun],
    gmail: list[tuple[int, str, dict[str, Any]]],
    db_session: Session,
) -> None:
    """AC: "bật + duyệt → mail tới hộp thư thật" — sent once, exactly as approved, flagged everywhere."""
    version = trialled(draft_approve_send())
    card = await _paused(client, version, creator_token, scripted_chat_model_factory, send_for_real=True)
    assert card["live_sends"] is True, "the card must say that approving it sends for real"

    scripted_chat_model_factory([[answers("Đã gửi thư cho khách.")]])
    resumed = await decide(client, version, card["approval_id"], creator_token, approved=True)

    assert resumed.status_code == 200, resumed.text
    assert gmail == [(version.agent_id, "send_email", APPROVED_ARGS)]
    [call] = _send_lines(resumed, "tool")
    [result] = _send_lines(resumed, "tool_result")
    assert (call["simulated"], call["live_in_sandbox"]) == (False, True)
    assert (result["ok"], result["simulated"], result["live_in_sandbox"]) == (True, False, True)

    # The stored trial says so too — the transcript is the evidence.
    record = parse_sse(resumed.text, event="sandbox_run")[0]
    stored = await client.get(
        f"/api/v1/agents/{version.agent_id}/sandbox-runs/{record['run_id']}", headers=auth_header(creator_token)
    )
    lines = [line for line in stored.json()["transcript"] if line.get("tool") == SEND]
    assert [(line["kind"], line["simulated"], line["live_in_sandbox"]) for line in lines] == [
        ("tool", False, True),
        ("tool_result", False, True),
    ]

    # And the PM's ledger: flagged, and still a rehearsal — never production.
    runs = db_session.scalars(select(AgentRun).where(AgentRun.version_id == version.id)).all()
    assert any(run.live_sends for run in runs)
    assert {run.trigger for run in runs} == {RunTrigger.CHAT_TEST.value}


async def test_on_and_rejected_nothing_is_sent(
    client: AsyncClient,
    creator_token: str,
    trialled: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[..., ScriptedRun],
    gmail: list[tuple[int, str, dict[str, Any]]],
) -> None:
    """AC: "bật + từ chối → không gửi"."""
    version = trialled(draft_approve_send())
    card = await _paused(client, version, creator_token, scripted_chat_model_factory, send_for_real=True)

    scripted_chat_model_factory([[answers("Không gửi thư.")]])
    resumed = await decide(client, version, card["approval_id"], creator_token, approved=False, reason="sai giọng")

    assert resumed.status_code == 200, resumed.text
    assert gmail == []
    assert _send_lines(resumed, "tool") == []


async def test_the_next_turn_is_a_rehearsal_again(
    client: AsyncClient,
    creator_token: str,
    trialled: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[..., ScriptedRun],
    gmail: list[tuple[int, str, dict[str, Any]]],
) -> None:
    """AC: "lượt sau mặc định tắt lại" — the switch is the turn's, not the thread's."""
    version = trialled(draft_approve_send())
    first = await _paused(client, version, creator_token, scripted_chat_model_factory, send_for_real=True)
    scripted_chat_model_factory([[answers("Đã gửi.")]])
    sent = await decide(client, version, first["approval_id"], creator_token, approved=True)
    thread = parse_sse(sent.text, event="thread")[0]["conversation_id"]
    assert len(gmail) == 1

    second = await _paused(client, version, creator_token, scripted_chat_model_factory, conversation_id=thread)
    scripted_chat_model_factory([[answers("Đã gửi (mô phỏng).")]])
    resumed = await decide(client, version, second["approval_id"], creator_token, approved=True)

    assert len(gmail) == 1, "a turn that did not ask to send for real sent anyway"
    assert [line["simulated"] for line in _send_lines(resumed, "tool")] == [True]


async def test_an_action_nobody_had_to_approve_stays_a_mock_even_when_switched_on(
    client: AsyncClient,
    creator_token: str,
    trialled: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[..., ScriptedRun],
    gmail: list[tuple[int, str, dict[str, Any]]],
) -> None:
    """The exception is for approved payloads only — `create_draft` (medium, ungated) is not one."""
    workflow = {
        "start_node_id": "draft",
        "nodes": [
            {"id": "draft", "type": "agent_llm", "config": {"prompt": "Soạn thư.", "model": "gpt-4o-mini"}},
            {"id": "save", "type": "mcp", "config": {"tool_id": GMAIL, "action": "create_draft"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "draft", "source_port": "done", "target_node_id": "save"},
            {"id": "e2", "source_node_id": "save", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {},
    }
    version = trialled(workflow)
    scripted_chat_model_factory(
        [
            [answers("Bản nháp.")],
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "create_draft", "args": APPROVED_ARGS, "id": "f", "type": "tool_call"}],
                )
            ],
        ]
    )

    response = await trial_workflow(client, version, ASKED, creator_token, send_for_real=True)

    assert response.status_code == 200, response.text
    assert gmail == []
    [call] = [line for line in parse_sse(response.text, event="tool") if line["tool"] == f"{GMAIL}.create_draft"]
    assert call["simulated"] is True
