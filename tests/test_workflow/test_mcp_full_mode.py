"""`full` action mode — the port of Agent Builder's `allowed_tools`.

What these pin is the half that matters: the safety floor does not move when a
node stops naming one action. A `full` node whose allowed set contains a write
is guarded exactly as a `single` node naming that write would be, and narrowing
the set to reads is what lifts the gate — not the mode.
"""

from src.models.node_registry import NodeType, unfilled_fields
from src.models.workflow import WorkflowJSON
from src.workflow.tools import GMAIL, GOOGLE_DRIVE, RiskLevel, offered_actions, riskiest
from src.workflow.validation import graph_problems


def _graph(config: dict) -> WorkflowJSON:
    """One MCP node straight to `end`, with nobody standing in front of it."""
    return WorkflowJSON.model_validate(
        {
            "start_node_id": "act",
            "nodes": [{"id": "act", "type": "mcp", "config": config}, {"id": "z", "type": "end", "config": {}}],
            "edges": [{"id": "e1", "source_node_id": "act", "source_port": "done", "target_node_id": "z"}],
        }
    )


def _messages(workflow: WorkflowJSON) -> list[str]:
    return [problem.message for problem in graph_problems(workflow)]


def test_the_merged_google_workspace_connection_is_gone() -> None:
    """One connection per service. A compatibility alias beside them would be a
    second id answering the same question."""
    from src.workflow.tools import TOOL_DEFINITIONS

    assert {tool.id for tool in TOOL_DEFINITIONS.all()} == {GMAIL, GOOGLE_DRIVE}


def test_full_mode_does_not_demand_a_single_action() -> None:
    """A `full` node has no one action by definition — demanding one would
    strand a finished node at the confirm gate."""
    assert unfilled_fields(NodeType.MCP, {"tool_id": GMAIL, "action": "", "action_mode": "single"}) == ("action",)
    assert unfilled_fields(NodeType.MCP, {"tool_id": GMAIL, "action": "", "action_mode": "full"}) == ()


def test_allowed_actions_narrows_and_never_widens() -> None:
    tool, every = offered_actions(GMAIL)
    assert tool is not None
    narrowed = offered_actions(GMAIL, ["read_email", "khong-ton-tai"])
    assert narrowed is not None
    assert [action.name for action in narrowed[1]] == ["read_email"]
    assert len(every) > 1


def test_riskiest_is_the_maximum_not_the_average() -> None:
    """A set of four reads and one send is a node that can send."""
    _tool, actions = offered_actions(GMAIL)
    worst = riskiest(actions)
    assert worst is not None and worst.name == "send_email"
    assert worst.risk_level is RiskLevel.HIGH


def test_full_mode_over_a_write_needs_approval_upstream() -> None:
    problems = _messages(_graph({"tool_id": GMAIL, "action": "", "action_mode": "full"}))
    assert any("Human Approval" in message for message in problems)
    assert any("send_email" in message for message in problems)


def test_full_mode_narrowed_to_reads_needs_no_approval() -> None:
    """The gate follows the effect, not the mode — narrowing lifts it."""
    problems = _messages(
        _graph(
            {
                "tool_id": GMAIL,
                "action": "",
                "action_mode": "full",
                "allowed_actions": ["search_emails", "read_email"],
            }
        )
    )
    assert not any("Human Approval" in message for message in problems)


def test_single_mode_is_unchanged_by_any_of_this() -> None:
    guarded = _messages(_graph({"tool_id": GMAIL, "action": "send_email"}))
    assert any("Human Approval" in message for message in guarded)
    assert not any("Human Approval" in message for message in _messages(_graph({"tool_id": GMAIL, "action": "read_email"})))
