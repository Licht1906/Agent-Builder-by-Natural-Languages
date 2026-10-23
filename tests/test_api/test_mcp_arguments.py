"""What an `MCP` node calls its action with, and what the chat screen is shown.

Two bugs from one field test, both reported by the creator of a party-invite
agent (gmail `send_email`, built on the canvas, deployed, chatted with):

1. *"đủ thông tin rồi mà nó bảo không gửi được"* — `single` mode handed every
   action `{"input": <last sentence>}`. `send_email` read `args["to"]`, found
   nothing, and refused. The recipient was in the conversation; nothing read it.
2. *"câu trả lời hiện trước rồi bị thay bằng JSON"* — the `if_else` router
   streamed its `{"holds": …}` deliberation as `token` events under its own id,
   and the chat screen, which shows the newest speaker, replaced the answer
   with it.

Read through HTTP (ADR-0048): both claims are about what the run *emits*, which
is the only vantage point the chat screen has.
"""

from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage

from src.db.models import AgentVersion, VersionStatus
from src.workflow.tools import GMAIL
from tests.conftest import ScriptedRun, parse_sse
from tests.test_api.test_workflow_approval import SPEC, answers, decides, trial_workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]


def calls_tool(name: str, **args: Any) -> AIMessage:
    """The model filling an action's arguments — a tool call, not prose."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1", "type": "tool_call"}])


@pytest.fixture
def confirmed(solution_spec_version: Callable[..., AgentVersion], seeded_creator: Any) -> Callable[..., AgentVersion]:
    def _make(workflow: dict[str, Any]) -> AgentVersion:
        return solution_spec_version(seeded_creator, SPEC, status=VersionStatus.WORKFLOW_CONFIRMED, workflow=workflow)

    return _make


def search_then_end() -> dict[str, Any]:
    """`AgentLlm → MCP(gmail.search_emails) → AgentLlm → End` — a read action with real parameters."""
    return {
        "start_node_id": "hieu",
        "nodes": [
            {"id": "hieu", "type": "agent_llm", "config": {"prompt": "Hiểu người dùng muốn tìm thư gì.", "model": "gpt-4o-mini"}},
            {"id": "tim", "type": "mcp", "config": {"tool_id": GMAIL, "action": "search_emails"}},
            {"id": "ke", "type": "agent_llm", "config": {"prompt": "Kể lại kết quả.", "model": "gpt-4o-mini"}},
            {"id": "xong", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "hieu", "source_port": "done", "target_node_id": "tim"},
            {"id": "e2", "source_node_id": "tim", "source_port": "done", "target_node_id": "ke"},
            {"id": "e3", "source_node_id": "ke", "source_port": "done", "target_node_id": "xong"},
        ],
        "metadata": {},
    }


def branch_then_answer() -> dict[str, Any]:
    """`AgentLlm → IfElse → AgentLlm / End` — the router sits between two speakers."""
    return {
        "start_node_id": "tra_loi",
        "nodes": [
            {"id": "tra_loi", "type": "agent_llm", "config": {"prompt": "Trả lời.", "model": "gpt-4o-mini"}},
            {"id": "re", "type": "if_else", "config": {"condition": "người dùng đã cho đủ thông tin"}},
            {"id": "chot", "type": "agent_llm", "config": {"prompt": "Chốt lại.", "model": "gpt-4o-mini"}},
            {"id": "xong", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "tra_loi", "source_port": "done", "target_node_id": "re"},
            {"id": "e2", "source_node_id": "re", "source_port": "true", "target_node_id": "chot"},
            {"id": "e3", "source_node_id": "re", "source_port": "false", "target_node_id": "xong"},
            {"id": "e4", "source_node_id": "chot", "source_port": "done", "target_node_id": "xong"},
        ],
        "metadata": {},
    }


async def test_a_single_action_with_parameters_is_called_with_typed_arguments(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """`single` mode: the action is fixed, the arguments are read off the conversation."""
    scripted_chat_model_factory(
        [
            [answers("Bạn muốn tìm các thư về hoá đơn tháng 8.")],
            [calls_tool("search_emails", query="hoá đơn tháng 8", max_results=5)],
            [answers("Tìm thấy 3 thư.")],
        ]
    )
    version = confirmed(search_then_end())

    response = await trial_workflow(client, version, "tìm giúp thư về hoá đơn tháng 8", creator_token)

    assert response.status_code == 200, response.text
    [call] = [line for line in parse_sse(response.text, event="tool") if line["tool"] == f"{GMAIL}.search_emails"]
    # Typed, from the schema — not `{"input": "<last sentence>"}`.
    assert call["args"] == {"query": "hoá đơn tháng 8", "max_results": 5}


async def test_the_argument_filler_sees_the_whole_conversation_not_the_last_sentence(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """The recipient given three turns ago must reach the filler.

    Asserted on what the filler was *handed*, which is the only thing that
    decides whether a value given earlier can be used at all.
    """
    version = confirmed(search_then_end())
    scripted_chat_model_factory([[answers("Được.")], [calls_tool("search_emails", query="x")], [answers("Xong.")]])
    first = await trial_workflow(client, version, "tôi cần tìm thư của anh Nam", creator_token)
    thread = parse_sse(first.text, event="thread")[0]["conversation_id"]

    _ids, models = scripted_chat_model_factory(
        [[answers("Rõ.")], [calls_tool("search_emails", query="anh Nam tháng 8")], [answers("Xong.")]]
    )
    await trial_workflow(client, version, "tháng 8 thôi", creator_token, conversation_id=thread)

    filler = models[1]
    handed = "\n".join(str(message.content) for message in filler.received_message_batches[0])
    assert "anh Nam" in handed, "the first turn's detail never reached the argument filler"
    assert "tháng 8" in handed


async def test_a_single_string_tool_keeps_its_legacy_payload(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """`get_profile` declares no parameters: no filler call, the old payload goes out.

    Pinned so the fix cannot have turned every connection written against
    `input` into one that now needs a model call it never asked for.
    """
    workflow = search_then_end()
    workflow["nodes"][1]["config"]["action"] = "get_profile"
    # Two model calls, not three — the MCP node must not consume a script.
    scripted_chat_model_factory([[answers("Ok.")], [answers("Xong.")]])
    version = confirmed(workflow)

    response = await trial_workflow(client, version, "tôi là ai", creator_token)

    assert response.status_code == 200, response.text
    [call] = [line for line in parse_sse(response.text, event="tool") if line["tool"] == f"{GMAIL}.get_profile"]
    assert list(call["args"]) == ["input"]
    assert [line["node_id"] for line in parse_sse(response.text, event="node")] == ["hieu", "tim", "ke", "xong"]


async def test_a_router_does_not_narrate_its_deliberation_as_the_agent_speaking(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """`token` means "the agent is saying this". A branch decision is not that."""
    scripted_chat_model_factory([[answers("Đã đủ địa chỉ, giờ và người nhận.")], [decides(True)], [answers("Chốt.")]])
    version = confirmed(branch_then_answer())

    response = await trial_workflow(client, version, "gửi thư mời", creator_token)

    assert response.status_code == 200, response.text
    spoken_by = {line["node_id"] for line in parse_sse(response.text, event="token")}
    assert "tra_loi" in spoken_by and "chot" in spoken_by
    assert "re" not in spoken_by, "the router's JSON reached the chat screen as speech"
    # The decision itself is still on the record — in the node event, where it belongs.
    [routed] = [line for line in parse_sse(response.text, event="node") if line["node_id"] == "re"]
    assert routed["port_taken"] == "true"
