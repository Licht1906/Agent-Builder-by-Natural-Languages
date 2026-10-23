"""The two Agent Builder fields this platform ported, and the one it did not.

OpenAI's Agent Builder configures its MCP node with, among other things, an
`allowed_tools` list and a `require_approval` of `"always"` | `"never"` | a
per-tool mapping. Both ideas landed here; neither landed literally, and the
differences are what these tests pin down:

- `require_approval` sits on the node, has `auto` and `always`, and has **no
  `never`** — it may tighten ADR-0023 §5's gate and never lift it.
- `allowed_tools` sits on the *connection* rather than the node, because a node
  here names exactly one action (ADR-0021 §2). Empty means every action, so a
  registry that predates the column keeps meaning what it meant.

No database and no socket: everything under test is either a pure function over
a document or a filter over a row's own columns.
"""

from typing import Any

import pytest

from src.db.models import ToolRegistration
from src.models.node_registry import McpApproval, McpConfig, NodeType
from src.models.workflow import Edge, NodeInstance, WorkflowJSON
from src.workflow.registrations import (
    ToolRegistrationError,
    _checked_allowlist,
    offered_actions,
    reviewed_actions,
)
from src.workflow.validation import graph_problems, graph_warnings

# ── require_approval ─────────────────────────────────────────────────────────


def workflow_with(mcp_config: dict[str, Any], *, approval_before: bool) -> WorkflowJSON:
    """A graph that reaches one `MCP` node, with or without a person in front of it."""
    act = NodeInstance(id="act", type=NodeType.MCP, config=mcp_config)
    end = NodeInstance(id="end", type=NodeType.END, config={})
    if not approval_before:
        return WorkflowJSON(
            start_node_id="act",
            nodes=[act, end],
            edges=[Edge(id="act->end", source_node_id="act", source_port="done", target_node_id="end")],
        )
    gate = NodeInstance(
        id="gate",
        type=NodeType.HUMAN_APPROVAL,
        config={"approver_role": "manager", "message_template": "Duyệt giúp việc này"},
    )
    return WorkflowJSON(
        start_node_id="gate",
        nodes=[gate, act, end],
        edges=[
            Edge(id="gate->act", source_node_id="gate", source_port="approved", target_node_id="act"),
            Edge(id="gate->end", source_node_id="gate", source_port="rejected", target_node_id="end"),
            Edge(id="act->end", source_node_id="act", source_port="done", target_node_id="end"),
        ],
    )


#: A connection nobody registered, so `resolved_action` answers `None` and the
#: platform has *no* computed opinion about the risk. That is deliberate: it
#: isolates `require_approval` as the only thing the refusal below can be
#: reacting to.
UNKNOWN_TOOL = {"tool_id": "khong-ai-dang-ky", "action": "doc_ticket"}


def test_default_is_auto_so_a_node_drawn_before_the_field_existed_still_means_what_it_meant() -> None:
    """An `MCP` node whose stored config has no `require_approval` parses as `auto`."""
    assert McpConfig.model_validate(UNKNOWN_TOOL).require_approval is McpApproval.AUTO


def test_there_is_no_never() -> None:
    """The value that would switch off ADR-0023 §5 is refused rather than quietly downgraded.

    Accepting it and treating it as `auto` would be worse than refusing: the
    form would say the gate is off while the validator kept it on, and the
    creator would find out by having a workflow refuse to confirm for a reason
    the screen contradicts.
    """
    with pytest.raises(ValueError):
        McpConfig.model_validate({**UNKNOWN_TOOL, "require_approval": "never"})


def test_a_stored_config_without_the_field_is_finished_not_half_filled() -> None:
    """The regression that adding this field caused, and the reason it is marked `OPTIONAL`.

    `unfilled_fields` reads the *stored* config dict, where a field added today
    is simply absent — so on the first reading it reported `require_approval`
    unfilled, which made every `MCP` node already drawn on the platform refuse
    to confirm. Absent is not unanswered here; it is `auto`.
    """
    graph = workflow_with(UNKNOWN_TOOL, approval_before=False)
    assert not [problem for problem in graph_problems(graph) if "chưa điền" in problem.message]


def test_always_refuses_a_graph_with_no_person_in_front_of_the_node() -> None:
    """The creator's own business rule, enforced with the same teeth as the platform's."""
    problems = graph_problems(workflow_with({**UNKNOWN_TOOL, "require_approval": "always"}, approval_before=False))
    assert any("require_approval" in problem.message and problem.node_id == "act" for problem in problems)


def test_always_is_satisfied_by_an_approval_on_the_path() -> None:
    """It asks for a person, not for the node to be undrawable."""
    problems = graph_problems(workflow_with({**UNKNOWN_TOOL, "require_approval": "always"}, approval_before=True))
    assert not [problem for problem in problems if "require_approval" in problem.message]


def test_auto_leaves_an_unresolvable_action_a_warning_rather_than_a_refusal() -> None:
    """Unchanged behaviour, asserted so the new branch cannot have widened the old rule.

    The platform cannot say whether an unregistered connection's action writes,
    so it says so — and says it as a warning, which is a sentence beside the
    node rather than a graph that will not confirm.
    """
    graph = workflow_with(UNKNOWN_TOOL, approval_before=False)
    assert not [problem for problem in graph_problems(graph) if problem.node_id == "act"]
    assert [warning for warning in graph_warnings(graph) if warning.node_id == "act"]


def test_always_replaces_that_warning_instead_of_doubling_up_on_it() -> None:
    """One node, one sentence. The refusal already says everything the warning would.

    Left in, the creator would read "we could not check this connection" beside
    a node that is being refused for a reason that has nothing to do with the
    connection — two messages about one node, disagreeing about what is wrong.

    Scoped to the *connection* warning rather than to "any warning on this node",
    which is what it originally said. The wider form was over-broad by accident:
    it also forbade warnings that say something else entirely and agree with the
    refusal about nothing, because they are about nothing it is about. The rule
    being defended is "do not say two contradictory things about one node", not
    "a refused node is exempt from every other true observation".
    """
    graph = workflow_with({**UNKNOWN_TOOL, "require_approval": "always"}, approval_before=False)
    connection_warnings = [
        warning for warning in graph_warnings(graph) if warning.node_id == "act" and "registry" in warning.message
    ]
    assert not connection_warnings


# ── allowed_actions (Agent Builder's `allowed_tools`) ─────────────────────────


def registration(*, actions: list[str], allowed: list[str]) -> ToolRegistration:
    """An unsaved registration row — every column this filter reads, and nothing else.

    Built rather than persisted because `offered_actions` is a function of the
    row's own columns. SQLAlchemy defaults only fire on INSERT, so each one is
    set here explicitly; a `None` left behind would be testing the ORM.
    """
    return ToolRegistration(
        submitted_by=1,
        name="CRM nội bộ",
        server_url="https://mcp.example.test/rpc",
        credential_kind="none",
        actions=[
            {"name": name, "description": "", "input_schema": {}, "output_schema": {}, "read_only": True}
            for name in actions
        ],
        allowed_actions=allowed,
        risk_overrides={},
        mock_response_examples={},
    )


def test_empty_allowlist_means_every_action_not_no_action() -> None:
    """The one reading a set intersection would get wrong, and it would get it wrong silently.

    Every connection registered before the column existed stores `[]`, so
    reading that as "nothing permitted" would empty the canvas dropdown of every
    connection on the platform at once.
    """
    row = registration(actions=["read_ticket", "search_ticket"], allowed=[])
    assert [action.name for action in offered_actions(row)] == ["read_ticket", "search_ticket"]


def test_a_narrowed_connection_offers_only_what_it_was_narrowed_to() -> None:
    """`allowed_tools`, as the canvas dropdown and the graph validator both see it."""
    row = registration(actions=["read_ticket", "search_ticket", "close_ticket"], allowed=["read_ticket"])
    assert [action.name for action in offered_actions(row)] == ["read_ticket"]


def test_the_full_report_from_the_server_is_still_readable() -> None:
    """A reviewing admin has to see what was left out, or they are approving a narrowing blind.

    This is why there are two functions rather than one filtered flag:
    `reviewed_actions` is the review screen's list and `offered_actions` is
    everybody else's.
    """
    row = registration(actions=["read_ticket", "close_ticket"], allowed=["read_ticket"])
    assert [action.name for action in reviewed_actions(row)] == ["read_ticket", "close_ticket"]


def test_an_action_the_server_never_reported_cannot_be_allowed() -> None:
    """Refused, not dropped.

    A silently ignored name is an allowlist that looks wider than it is: a
    creator who mistypes one of three actions has permitted two and believes
    they permitted three, with nothing on the screen to say which.
    """
    with pytest.raises(ToolRegistrationError, match="close_tickets"):
        _checked_allowlist(["read_ticket", "close_tickets"], known={"read_ticket", "close_ticket"})


def test_an_allowlist_is_stored_sorted_and_deduplicated() -> None:
    """Two requests naming the same actions store the same list.

    A registration's diff should not show a change nobody made — the same
    reason `risk_overrides` is keyed rather than positional.
    """
    assert _checked_allowlist(["b", "a", "b"], known={"a", "b"}) == ["a", "b"]
