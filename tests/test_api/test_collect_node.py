"""The `collect` node (ADR-0052): what the action requires, read off its schema, checked in code.

Before it, "has the user given everything?" was an `AgentLlm` collector plus an
`IfElse` gate, both phrased by the meta agent. Every field failure of the
eleven Google agents lived in that prose: a gate demanding *email người chủ
trì* (no such parameter), a collector asking *bao nhiêu thư?* for an optional
count, a judge that said "chưa đủ" and took `true`. This node has no prompt
and no condition. These tests pin what replaces them:

- a required parameter the user never gave → the node asks for it, by name in
  the user's language, and the run ends (`incomplete`);
- everything there → `complete`, the values go to the action unchanged;
- an address the model *invented* for "anh Minh" is refused — `email` means
  verbatim in the user's words, with an `@`;
- an action with no required parameter costs nothing — no model call at all.

Read through HTTP like `test_mcp_arguments`: the claims are about what a run
emits, which is the only vantage point the chat screen has.
"""

from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage

from src.db.models import AgentVersion, VersionStatus
from src.models.node_registry import NodeType, outputs_of
from src.models.workflow import WorkflowJSON
from src.workflow.tools import GMAIL
from src.workflow.validation import graph_problems, graph_warnings
from tests.conftest import ScriptedRun, parse_sse
from tests.test_api.test_workflow_approval import SPEC, answers, trial_workflow

pytestmark = pytest.mark.usefixtures("default_platform_config")


def calls_tool(name: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1", "type": "tool_call"}])


@pytest.fixture
def confirmed(solution_spec_version: Callable[..., AgentVersion], seeded_creator: Any) -> Callable[..., AgentVersion]:
    def _make(workflow: dict[str, Any]) -> AgentVersion:
        return solution_spec_version(seeded_creator, SPEC, status=VersionStatus.WORKFLOW_CONFIRMED, workflow=workflow)

    return _make


def collect_then(action: str, *, approval: bool = False, agent_writes: list[str] | None = None) -> dict[str, Any]:
    """`Collect → [HumanApproval →] MCP(gmail.<action>) → AgentLlm → End`, with `incomplete` → End.

    The approval is there when the action is `high` risk (`send_email`): the
    validator refuses a path to such an action with nobody in front of it.
    """
    nodes = [
        {
            "id": "thu_thap",
            "type": "collect",
            "config": {"action_node": "hanh_dong", "agent_writes": agent_writes or []},
        },
        {"id": "hanh_dong", "type": "mcp", "config": {"tool_id": GMAIL, "action": action}},
        {"id": "ke", "type": "agent_llm", "config": {"prompt": "Kể lại kết quả.", "model": "gpt-4o-mini"}},
        {"id": "xong", "type": "end", "config": {}},
    ]
    edges = [
        {"id": "e2", "source_node_id": "thu_thap", "source_port": "incomplete", "target_node_id": "xong"},
        {"id": "e3", "source_node_id": "hanh_dong", "source_port": "done", "target_node_id": "ke"},
        {"id": "e4", "source_node_id": "ke", "source_port": "done", "target_node_id": "xong"},
    ]
    if approval:
        nodes.insert(
            1,
            {
                "id": "duyet",
                "type": "human_approval",
                "config": {"approver_role": "manager", "message_template": "Gửi?"},
            },
        )
        edges += [
            {"id": "e1", "source_node_id": "thu_thap", "source_port": "complete", "target_node_id": "duyet"},
            {"id": "e5", "source_node_id": "duyet", "source_port": "approved", "target_node_id": "hanh_dong"},
            {"id": "e6", "source_node_id": "duyet", "source_port": "rejected", "target_node_id": "xong"},
        ]
    else:
        edges.append(
            {"id": "e1", "source_node_id": "thu_thap", "source_port": "complete", "target_node_id": "hanh_dong"}
        )
    return {"start_node_id": "thu_thap", "nodes": nodes, "edges": edges, "metadata": {}}


def node_events(text: str, node_id: str) -> list[dict[str, Any]]:
    return [line for line in parse_sse(text, event="node") if line["node_id"] == node_id]


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


def test_collect_is_the_ninth_type_with_two_ports() -> None:
    assert NodeType.COLLECT.value == "collect"
    assert outputs_of(NodeType.COLLECT) == ("complete", "incomplete")


def test_a_collector_must_point_at_an_mcp_node_and_is_idle_in_front_of_full_mode() -> None:
    graph = collect_then("search_emails")
    graph["nodes"][0]["config"]["action_node"] = "ke"
    [problem] = [p for p in graph_problems(WorkflowJSON.model_validate(graph)) if p.node_id == "thu_thap"]
    assert "không phải một node mcp" in problem.message

    # A `full` mode target is idle, not wrong: the runtime passes the collector
    # through, so the graph confirms — the canvas warns instead.
    graph = collect_then("search_emails")
    graph["nodes"][1]["config"] = {
        "tool_id": GMAIL,
        "action": "",
        "action_mode": "full",
        "allowed_actions": ["send_email"],
    }
    parsed = WorkflowJSON.model_validate(graph)
    assert not [p for p in graph_problems(parsed) if p.node_id == "thu_thap"]
    [warning] = [w for w in graph_warnings(parsed) if w.node_id == "thu_thap"]
    assert "chế độ full" in warning.message

    assert not [
        p for p in graph_problems(WorkflowJSON.model_validate(collect_then("search_emails"))) if p.node_id == "thu_thap"
    ]


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


async def test_a_missing_required_parameter_is_asked_for_and_the_run_ends(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """`send_email` needs `to`, `subject`, `body`; the user gave a subject and a body, no address."""
    scripted_chat_model_factory(
        [
            [calls_tool("send_email", subject="Họp tuần sau", body="Mời anh họp 9h thứ Hai.")],  # the reading
        ]
    )
    version = confirmed(collect_then("send_email", approval=True))

    response = await trial_workflow(
        client, version, "Gửi thư mời họp tuần sau, 9h thứ Hai, cho anh Minh", creator_token
    )

    assert response.status_code == 200, response.text
    [collect] = node_events(response.text, "thu_thap")
    assert collect["port_taken"] == "incomplete"
    # Deterministic, from the parameter labels: it cannot ask for more than what is missing.
    assert collect["content"] == "Bạn vui lòng cho tôi biết email người nhận."
    assert not parse_sse(response.text, event="tool"), "nothing was sent"
    assert not node_events(response.text, "ke")


async def test_everything_present_goes_through_and_the_action_gets_the_checked_values(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """`search_emails` requires `query`; the user gave it, so the search runs with the collector's reading of it."""
    scripted_chat_model_factory(
        [
            [calls_tool("search_emails", query="hoá đơn tháng 8", max_results=3)],
            # The action's own fill reads the conversation again; the
            # collector's checked values must win over its second reading.
            [calls_tool("search_emails", query="hoá đơn", max_results=9)],
            [answers("Tìm thấy 3 thư.")],
        ]
    )
    version = confirmed(collect_then("search_emails"))

    response = await trial_workflow(client, version, "tìm giúp 3 thư về hoá đơn tháng 8", creator_token)

    assert response.status_code == 200, response.text
    [collect] = node_events(response.text, "thu_thap")
    assert collect["port_taken"] == "complete"
    assert collect["content"] == "query: hoá đơn tháng 8"
    [call] = parse_sse(response.text, event="tool")
    assert call["args"] == {"query": "hoá đơn tháng 8", "max_results": 3}
    assert [line["content"] for line in node_events(response.text, "ke")] == ["Tìm thấy 3 thư."]


async def test_an_id_the_model_invented_is_refused_because_the_user_never_typed_it(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """`read_email` marks `message_id` verbatim: "thư anh Minh gửi hôm qua" contains no id, whatever the model writes."""
    scripted_chat_model_factory([[calls_tool("read_email", message_id="18c2f9a0b1e4d7c3")]])
    version = confirmed(collect_then("read_email"))

    response = await trial_workflow(client, version, "đọc giúp thư anh Minh gửi hôm qua", creator_token)

    [collect] = node_events(response.text, "thu_thap")
    assert collect["port_taken"] == "incomplete"
    assert not parse_sse(response.text, event="tool")


async def test_an_address_the_model_invented_is_not_an_address_the_user_gave(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """The user said "anh Minh"; the model obligingly wrote `minh@example.com`. Refused: not in the user's words."""
    scripted_chat_model_factory(
        [
            [calls_tool("send_email", to="minh@example.com", subject="Họp", body="Mời anh họp.")],
        ]
    )
    version = confirmed(collect_then("send_email", approval=True))

    response = await trial_workflow(client, version, "Gửi thư mời họp cho anh Minh", creator_token)

    [collect] = node_events(response.text, "thu_thap")
    assert collect["port_taken"] == "incomplete"
    assert not parse_sse(response.text, event="tool")


async def test_a_name_is_not_an_email_even_when_it_is_verbatim(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    scripted_chat_model_factory(
        [
            [calls_tool("send_email", to="anh Minh", subject="Họp", body="Mời anh họp.")],
            [answers("Email của anh Minh?")],
        ]
    )
    version = confirmed(collect_then("send_email", approval=True))

    response = await trial_workflow(client, version, "Gửi thư mời họp cho anh Minh", creator_token)

    [collect] = node_events(response.text, "thu_thap")
    assert collect["port_taken"] == "incomplete"


async def test_a_parameter_a_later_node_writes_is_not_asked_of_the_user(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """`agent_writes: ["subject", "body"]` — the graph drafts the letter; the user only owes the address.

    Asserted up to the approval pause: the collector's summary names the one
    thing it collected, and the run has reached the gate rather than ending on
    a question about prose the agent was built to write.
    """
    scripted_chat_model_factory([[calls_tool("send_email", to="lan@example.com")]])
    version = confirmed(collect_then("send_email", approval=True, agent_writes=["subject", "body"]))

    response = await trial_workflow(client, version, "Mời chị Lan lan@example.com dự tiệc", creator_token)

    assert response.status_code == 200, response.text
    [collect] = node_events(response.text, "thu_thap")
    assert collect["port_taken"] == "complete"
    assert collect["content"] == "to: lan@example.com"
    assert parse_sse(response.text, event="awaiting_approval")


async def test_an_action_with_no_required_parameter_passes_through_without_a_model_call(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """`create_draft` requires nothing, so the collector is free: the first model call is the action's own fill."""
    scripted_chat_model_factory([[calls_tool("create_draft", subject="Nháp")], [answers("Đã lưu nháp.")]])
    version = confirmed(collect_then("create_draft"))

    response = await trial_workflow(client, version, "Soạn nháp thư tiêu đề Nháp", creator_token)

    assert response.status_code == 200, response.text
    [collect] = node_events(response.text, "thu_thap")
    assert collect["port_taken"] == "complete"
    assert collect["content"] == ""
    [call] = parse_sse(response.text, event="tool")
    assert call["args"] == {"subject": "Nháp"}


async def test_the_actions_own_fill_also_drops_an_address_the_user_never_typed(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """`create_draft` requires nothing, so no collector stands in the way — the fill wrote `to: "anh Minh"`.

    Google answered HTTP 400 "Invalid To header" in the baseline run. The same
    verbatim/email check the collector applies now applies to what the fill
    writes on its own: the name is dropped, the draft is created without it.
    """
    scripted_chat_model_factory(
        [
            [calls_tool("create_draft", to="anh Minh", subject="Thư mời họp", body="Kính gửi anh Minh…")],
            [answers("Đã lưu nháp.")],
        ]
    )
    version = confirmed(collect_then("create_draft"))

    response = await trial_workflow(client, version, "Soạn nháp thư mời họp cho anh Minh", creator_token)

    assert response.status_code == 200, response.text
    [call] = parse_sse(response.text, event="tool")
    assert call["args"] == {"subject": "Thư mời họp", "body": "Kính gửi anh Minh…"}
