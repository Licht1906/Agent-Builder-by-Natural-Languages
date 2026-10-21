"""A graph that answers a person with a machine payload.

The bug this pins was reported as *"agent chạy bị trả lời bằng json"*, and the
runtime was innocent: on `mcp → end` the last thing written into `messages` is
the tool's own response body, so the agent's reply to a human really is `{}`.

Nothing was wrong with the run. What was wrong is that **nothing said so** —
`graph_problems` was clean, confirm passed, and the creator found out by reading
JSON in a chat bubble. Only `agent_llm` and `mcp` write user-visible messages
(`runtime.py`), so "what will this agent actually say" is decidable from the
document alone, and therefore belongs in the validator.

A **warning**, not a refusal, and deliberately: an agent whose whole job is to
file a ticket and stop is a legitimate graph, and its raw result may be exactly
what the caller wants. The platform's job is to make sure nobody ships that
shape *by accident*.
"""

from typing import Any

from src.models.node_registry import NodeType
from src.models.workflow import Edge, NodeInstance, WorkflowJSON
from src.workflow.validation import graph_problems, graph_warnings

# `auto`, not `always`: `always` without a gate in front of it is its own
# (correct) refusal, and a fixture that trips two rules at once cannot show which
# one this file is about.
MCP_CONFIG: dict[str, Any] = {"tool_id": "registered-1", "action": "search_orders", "require_approval": "auto"}
PROSE = "Trả lời khách bằng lời, dựa trên kết quả tra cứu."


def _end() -> NodeInstance:
    return NodeInstance(id="xong", type=NodeType.END, config={})


def _mcp(node_id: str = "goi_erp") -> NodeInstance:
    return NodeInstance(id=node_id, type=NodeType.MCP, config=MCP_CONFIG)


def _says_raw(workflow: WorkflowJSON) -> list[str]:
    """Node ids the validator warns will answer with raw tool output."""
    return [w.node_id for w in graph_warnings(workflow) if w.node_id and "JSON" in w.message.upper()]


def test_an_mcp_node_wired_straight_to_end_is_flagged() -> None:
    """The reported graph, exactly: one MCP call, then stop."""
    workflow = WorkflowJSON(
        start_node_id="goi_erp",
        nodes=[_mcp(), _end()],
        edges=[Edge(id="e1", source_node_id="goi_erp", source_port="done", target_node_id="xong")],
    )

    assert _says_raw(workflow) == ["goi_erp"]
    # A warning, not a refusal — this graph is still confirmable.
    assert not [p for p in graph_problems(workflow) if p.node_id == "goi_erp"]


def test_a_step_that_phrases_the_result_clears_it() -> None:
    """The fix a creator would make: put something in front of the door."""
    workflow = WorkflowJSON(
        start_node_id="goi_erp",
        nodes=[
            _mcp(),
            NodeInstance(id="tra_loi", type=NodeType.AGENT_LLM, config={"prompt": PROSE, "model": "gpt-4o-mini"}),
            _end(),
        ],
        edges=[
            Edge(id="e1", source_node_id="goi_erp", source_port="done", target_node_id="tra_loi"),
            Edge(id="e2", source_node_id="tra_loi", source_port="done", target_node_id="xong"),
        ],
    )

    assert _says_raw(workflow) == []


def test_a_branch_between_the_call_and_the_door_does_not_hide_it() -> None:
    """`if_else` writes no message, so the MCP payload is still what comes out.

    The check walks back through nodes that say nothing rather than looking only
    at edges into `end` — otherwise one branch node is enough to slip a raw
    payload past the warning, which is the failure mode that makes shallow
    versions of this check worthless.
    """
    workflow = WorkflowJSON(
        start_node_id="goi_erp",
        nodes=[
            _mcp(),
            NodeInstance(id="re_nhanh", type=NodeType.IF_ELSE, config={"condition": "có đơn hàng"}),
            _end(),
        ],
        edges=[
            Edge(id="e1", source_node_id="goi_erp", source_port="done", target_node_id="re_nhanh"),
            Edge(id="e2", source_node_id="re_nhanh", source_port="true", target_node_id="xong"),
            Edge(id="e3", source_node_id="re_nhanh", source_port="false", target_node_id="xong"),
        ],
    )

    assert _says_raw(workflow) == ["goi_erp"]


def test_a_graph_that_never_calls_a_tool_is_left_alone() -> None:
    """No MCP node, nothing to warn about — the check must not fire on prose."""
    workflow = WorkflowJSON(
        start_node_id="tra_loi",
        nodes=[
            NodeInstance(id="tra_loi", type=NodeType.AGENT_LLM, config={"prompt": PROSE, "model": "gpt-4o-mini"}),
            _end(),
        ],
        edges=[Edge(id="e1", source_node_id="tra_loi", source_port="done", target_node_id="xong")],
    )

    assert _says_raw(workflow) == []


def test_a_node_nothing_reaches_is_named_rather_than_ignored() -> None:
    """A stored graph had an armed `mcp` node no edge touched; every gate was silent.

    It is a warning (the canvas must stay saveable mid-edit), but it must be
    said: an unreachable side-effect node looks like part of the agent and
    never fires.
    """
    workflow = WorkflowJSON(
        start_node_id="tra_loi",
        nodes=[
            NodeInstance(id="tra_loi", type=NodeType.AGENT_LLM, config={"prompt": PROSE, "model": "gpt-4o-mini"}),
            _end(),
            _mcp("mo_coi"),
        ],
        edges=[Edge(id="e1", source_node_id="tra_loi", source_port="done", target_node_id="xong")],
    )

    orphaned = [w.node_id for w in graph_warnings(workflow) if "không nối vào đâu" in w.message]
    assert orphaned == ["mo_coi"]
    assert not [p for p in graph_problems(workflow) if p.node_id == "mo_coi"]
