"""An approval is of a payload — DS-08 / ADR-0059 §7.

V2 puts no limit on who an email may go to, so the person deciding is the one
thing between a model and the outside world. These tests hold the three
promises that makes the approval worth something:

- the card shows the call verbatim — recipient, subject, body — before anyone
  decides;
- an approved call is sent exactly as shown, and the resumed run does not ask
  a model to write it again;
- anything else — a body a model rewrote after the yes, a second send on one
  approval — is refused before Gmail is reached, with the reason on the
  transcript.

Run through the production door (`deployed-workflow-chat`), because a trial
mocks `send_email` and never reaches Gmail at all; Gmail itself is replaced at
`src.workflow.google_tools` by a recorder, so "not called" is observed rather
than inferred.
"""

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from src.db.models import Agent, AgentVersion, Deployment, DeploymentAccessMode, User, VersionStatus
from src.workflow import google_tools
from src.workflow.approvals import action_hash, approved_action_hash
from src.workflow.tools import GMAIL
from tests.conftest import DEPLOYMENT_API_KEY, ScriptedRun, auth_header, discard_agent, hash_api_key, parse_sse
from tests.test_api.test_workflow_approval import SPEC, answers, decide, decides, pending, trial_workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

ASKED = "gửi thư cho khach@example.com báo khiếu nại đã được xử lý"
SEND = f"{GMAIL}.send_email"
APPROVED_ARGS = {"to": "khach@example.com", "subject": "Về khiếu nại", "body": "Khiếu nại của anh đã được xử lý."}


def fills(**args: Any) -> AIMessage:
    """The model filling `send_email`'s arguments."""
    return AIMessage(content="", tool_calls=[{"name": "send_email", "args": args, "id": "fill", "type": "tool_call"}])


def _agent_llm(node_id: str, prompt: str) -> dict[str, Any]:
    return {"id": node_id, "type": "agent_llm", "config": {"prompt": prompt, "model": "gpt-4o-mini"}}


def _edge(index: int, source: str, port: str, target: str) -> dict[str, Any]:
    return {"id": f"e{index}", "source_node_id": source, "source_port": port, "target_node_id": target}


def draft_approve_send(*, rewrite: bool = False) -> dict[str, Any]:
    """`AgentLlm → HumanApproval → [AgentLlm rewrite →] MCP(send_email) → AgentLlm → End`."""
    nodes = [
        _agent_llm("draft", "Soạn thư trả lời khách."),
        {
            "id": "approval",
            "type": "human_approval",
            "config": {"approver_role": "manager", "message_template": "Duyệt gửi thư?"},
        },
        {"id": "send", "type": "mcp", "config": {"tool_id": GMAIL, "action": "send_email"}},
        _agent_llm("report", "Báo lại chuyện gì đã xảy ra."),
        {"id": "finish", "type": "end", "config": {}},
    ]
    after_approval = "rewrite" if rewrite else "send"
    edges = [
        _edge(1, "draft", "done", "approval"),
        _edge(2, "approval", "approved", after_approval),
        _edge(3, "approval", "rejected", "report"),
        _edge(4, "send", "done", "report"),
        _edge(5, "report", "done", "finish"),
    ]
    if rewrite:
        nodes.insert(2, _agent_llm("rewrite", "Soạn lại thư cho hay hơn."))
        edges.append(_edge(6, "rewrite", "done", "send"))
    return {"start_node_id": "draft", "nodes": nodes, "edges": edges, "metadata": {}}


def approve_then_loop() -> dict[str, Any]:
    """One approval in front of a `While` whose body sends — the graph validates, the second send must not happen."""
    return {
        "start_node_id": "draft",
        "nodes": [
            _agent_llm("draft", "Soạn thư."),
            {
                "id": "approval",
                "type": "human_approval",
                "config": {"approver_role": "manager", "message_template": "Duyệt?"},
            },
            {"id": "again", "type": "while", "config": {"condition": "còn phải gửi", "max_iterations": 2}},
            {"id": "send", "type": "mcp", "config": {"tool_id": GMAIL, "action": "send_email"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            _edge(1, "draft", "done", "approval"),
            _edge(2, "approval", "approved", "again"),
            _edge(3, "approval", "rejected", "finish"),
            _edge(4, "again", "loop", "send"),
            _edge(5, "send", "done", "again"),
            _edge(6, "again", "exit", "finish"),
        ],
        "metadata": {},
    }


@pytest.fixture
def gmail(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Every Gmail call the run really made, `(action, args)` — Google itself never reached."""
    sent: list[tuple[str, dict[str, Any]]] = []

    def recorded(action: str, args: Any, headers: Any) -> str:
        sent.append((action, dict(args)))
        return json.dumps({"message_id": "m-1", "sent": True})

    monkeypatch.setattr(google_tools, "_as_owner", lambda agent_id, call: call({}))
    monkeypatch.setattr(google_tools, "_gmail", recorded)
    return sent


@pytest.fixture
def deployed(db_session: Session, seeded_creator: User) -> Iterator[Callable[[dict[str, Any]], AgentVersion]]:
    """A published agent running `workflow` — the door a real send goes through."""
    made: list[Agent] = []

    def _make(workflow: dict[str, Any]) -> AgentVersion:
        agent = Agent(owner_id=seeded_creator.id, name="Agent gui thu")
        db_session.add(agent)
        db_session.flush()
        version = AgentVersion(
            agent_id=agent.id, spec=None, solution_spec=SPEC, workflow=workflow, status=VersionStatus.APPROVED
        )
        db_session.add(version)
        db_session.flush()
        db_session.add(
            Deployment(
                agent_id=agent.id,
                agent_version_id=version.id,
                access_mode=DeploymentAccessMode.PRIVATE,
                api_key_hash=hash_api_key(DEPLOYMENT_API_KEY),
                created_by=seeded_creator.id,
            )
        )
        db_session.commit()
        made.append(agent)
        return version

    yield _make
    for agent in made:
        discard_agent(db_session, agent)


async def _start(client: AsyncClient, version: AgentVersion, token: str) -> dict[str, Any]:
    """Ask the published agent, and return the one approval it stopped at."""
    started = await client.post(
        f"/api/v1/agents/{version.agent_id}/deployed-workflow-chat", json={"message": ASKED}, headers=auth_header(token)
    )
    assert started.status_code == 200, started.text
    [waiting] = await pending(client, version, token)
    return waiting


def _send_results(response: Any) -> list[dict[str, Any]]:
    return [line for line in parse_sse(response.text, event="tool_result") if line["tool"] == SEND]


async def test_the_card_shows_the_call_verbatim_before_anyone_decides(
    client: AsyncClient,
    creator_token: str,
    deployed: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    gmail: list[tuple[str, dict[str, Any]]],
) -> None:
    scripted_chat_model_factory([[answers("Bản nháp thư.")], [fills(**APPROVED_ARGS)]])
    version = deployed(draft_approve_send())

    waiting = await _start(client, version, creator_token)

    assert waiting["actions"] == [
        {
            "node_id": "send",
            "tool": SEND,
            "action": "send_email",
            "args": APPROVED_ARGS,
            "hash": action_hash(SEND, APPROVED_ARGS),
        }
    ]
    assert gmail == [], "nothing may be sent before the decision"


async def test_an_approved_call_is_sent_exactly_as_shown_without_writing_it_again(
    client: AsyncClient,
    creator_token: str,
    deployed: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    gmail: list[tuple[str, dict[str, Any]]],
) -> None:
    """AC: "duyệt rồi gửi đúng payload → gửi". The resumed leg's only model call is `report`'s."""
    scripted_chat_model_factory([[answers("Bản nháp thư.")], [fills(**APPROVED_ARGS)]])
    version = deployed(draft_approve_send())
    waiting = await _start(client, version, creator_token)

    _ids, models = scripted_chat_model_factory([[answers("Đã gửi thư cho khách.")]])
    resumed = await decide(client, version, waiting["approval_id"], creator_token, approved=True)

    assert resumed.status_code == 200, resumed.text
    assert gmail == [("send_email", waiting["actions"][0]["args"])]
    assert [line["ok"] for line in _send_results(resumed)] == [True]
    assert len(models) == 1, "the resumed run asked a model to write the email a second time"


async def test_a_body_a_model_rewrote_after_the_yes_is_refused_and_gmail_is_not_called(
    client: AsyncClient,
    creator_token: str,
    deployed: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    gmail: list[tuple[str, dict[str, Any]]],
) -> None:
    """AC: "duyệt một mail rồi để model đổi nội dung trước khi gửi → bị từ chối, không gọi Gmail"."""
    scripted_chat_model_factory([[answers("Bản nháp thư.")], [fills(**APPROVED_ARGS)]])
    version = deployed(draft_approve_send(rewrite=True))
    waiting = await _start(client, version, creator_token)

    rewritten = {**APPROVED_ARGS, "body": "Chúng tôi từ chối khiếu nại của anh."}
    scripted_chat_model_factory([[answers("Bản soạn lại.")], [fills(**rewritten)], [answers("Không gửi được.")]])
    resumed = await decide(client, version, waiting["approval_id"], creator_token, approved=True)

    assert resumed.status_code == 200, resumed.text
    assert gmail == []
    [refused] = _send_results(resumed)
    assert refused["ok"] is False
    assert "khác với payload đã được duyệt" in refused["result"]
    assert "body" in refused["result"]


async def test_a_rewrite_that_leaves_the_call_as_approved_still_sends(
    client: AsyncClient,
    creator_token: str,
    deployed: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    gmail: list[tuple[str, dict[str, Any]]],
) -> None:
    """A node speaking after the yes is not by itself a refusal — only a different call is."""
    scripted_chat_model_factory([[answers("Bản nháp thư.")], [fills(**APPROVED_ARGS)]])
    version = deployed(draft_approve_send(rewrite=True))
    waiting = await _start(client, version, creator_token)

    scripted_chat_model_factory([[answers("Giữ nguyên.")], [fills(**APPROVED_ARGS)], [answers("Đã gửi.")]])
    resumed = await decide(client, version, waiting["approval_id"], creator_token, approved=True)

    assert resumed.status_code == 200, resumed.text
    assert gmail == [("send_email", APPROVED_ARGS)]


async def test_one_approval_licenses_one_call_not_every_pass_of_a_loop(
    client: AsyncClient,
    creator_token: str,
    deployed: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    gmail: list[tuple[str, dict[str, Any]]],
) -> None:
    """ "Node action rủi ro không chạy được nếu không có một lần duyệt khớp" — the second pass has none left."""
    scripted_chat_model_factory([[answers("Bản nháp thư.")], [fills(**APPROVED_ARGS)]])
    version = deployed(approve_then_loop())
    waiting = await _start(client, version, creator_token)

    scripted_chat_model_factory([[decides(True)], [decides(True)], [fills(**APPROVED_ARGS)]])
    resumed = await decide(client, version, waiting["approval_id"], creator_token, approved=True)

    assert resumed.status_code == 200, resumed.text
    assert gmail == [("send_email", APPROVED_ARGS)]
    first, second = _send_results(resumed)
    assert first["ok"] is True
    assert second["ok"] is False
    assert "không có lần duyệt nào còn hiệu lực" in second["result"]


async def test_a_trial_shows_the_same_card_and_refuses_the_same_rewrite(
    client: AsyncClient,
    creator_token: str,
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """A rehearsal stops where production stops and refuses what production refuses — before its mock."""
    version = solution_spec_version(
        seeded_creator, SPEC, status=VersionStatus.WORKFLOW_CONFIRMED, workflow=draft_approve_send(rewrite=True)
    )
    scripted_chat_model_factory([[answers("Bản nháp thư.")], [fills(**APPROVED_ARGS)]])
    paused = await trial_workflow(client, version, ASKED, creator_token)
    [card] = parse_sse(paused.text, event="awaiting_approval")
    assert [action["args"] for action in card["actions"]] == [APPROVED_ARGS]

    rewritten = {**APPROVED_ARGS, "to": "nguoi-khac@example.com"}
    scripted_chat_model_factory([[answers("Bản soạn lại.")], [fills(**rewritten)], [answers("Không gửi được.")]])
    resumed = await decide(client, version, card["approval_id"], creator_token, approved=True)

    [refused] = _send_results(resumed)
    assert refused["ok"] is False
    assert refused["simulated"] is False, "a refused call is not a mock of a send"
    assert "khác ở: to" in refused["result"]


async def test_the_ledger_answers_only_for_an_approved_row_and_its_own_node(
    client: AsyncClient,
    creator_token: str,
    deployed: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    gmail: list[tuple[str, dict[str, Any]]],
) -> None:
    """The database's half of the check: a pending row approves nothing, and a row approves only its own node's call."""
    scripted_chat_model_factory([[answers("Bản nháp thư.")], [fills(**APPROVED_ARGS)]])
    version = deployed(draft_approve_send())
    waiting = await _start(client, version, creator_token)

    assert approved_action_hash(waiting["approval_id"], "send") is None

    scripted_chat_model_factory([[answers("Đã gửi.")]])
    await decide(client, version, waiting["approval_id"], creator_token, approved=True)

    assert approved_action_hash(waiting["approval_id"], "send") == action_hash(SEND, APPROVED_ARGS)
    assert approved_action_hash(waiting["approval_id"], "report") is None


def test_the_hash_is_canonical_and_exact() -> None:
    """Key order does not matter; one character of the body does, and so does the action."""
    same = action_hash(SEND, dict(reversed(list(APPROVED_ARGS.items()))))
    assert same == action_hash(SEND, APPROVED_ARGS)
    assert action_hash(SEND, {**APPROVED_ARGS, "body": APPROVED_ARGS["body"] + " "}) != same
    assert action_hash(f"{GMAIL}.create_draft", APPROVED_ARGS) != same
