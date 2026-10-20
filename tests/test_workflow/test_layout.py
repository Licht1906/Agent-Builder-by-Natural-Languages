"""`src.workflow.layout` — the placement pass a freshly composed graph goes through.

What these tests pin is the *reading order* of the result, not exact pixels:
control flows rightwards by depth, a branch's arms sit level with what feeds
them instead of in whatever order the document listed them, and every node —
reachable or not — ends up somewhere a person can see. Exact coordinates are
the module's own business; asserting them would re-state `COLUMN_WIDTH` in a
second place.
"""

from src.models.workflow import Edge, NodeInstance, WorkflowJSON
from src.workflow.layout import MARGIN, laid_out


def _node(node_id: str, node_type: str = "agent_llm") -> NodeInstance:
    return NodeInstance(id=node_id, type=node_type, config={})


def _edge(edge_id: str, source: str, target: str, port: str = "output") -> Edge:
    return Edge(id=edge_id, source_node_id=source, source_port=port, target_node_id=target)


def _positions(workflow: WorkflowJSON) -> dict[str, tuple[float, float]]:
    return {node.id: (node.position.x, node.position.y) for node in laid_out(workflow).nodes}


def test_depth_becomes_column() -> None:
    """A chain runs left to right, one column per step, all on one row."""
    workflow = WorkflowJSON(
        nodes=[_node("start"), _node("middle"), _node("finish", "end")],
        edges=[_edge("e1", "start", "middle"), _edge("e2", "middle", "finish")],
        start_node_id="start",
    )
    placed = _positions(workflow)
    assert placed["start"][0] < placed["middle"][0] < placed["finish"][0]
    assert placed["start"][1] == placed["middle"][1] == placed["finish"][1]


def test_crossed_branch_arms_untangle() -> None:
    """The document lists a branch's arms in the opposite order to the ports
    that feed them; barycenter ordering seats each arm level with its merge
    target instead of drawing an X across the column."""
    workflow = WorkflowJSON(
        nodes=[
            _node("start", "if_else"),
            # Deliberately listed low-arm-first: document order would place
            # `low` on the top row while its edges pull it to the bottom.
            _node("low"),
            _node("high"),
            _node("join_high"),
            _node("join_low"),
        ],
        edges=[
            _edge("e1", "start", "high", port="true"),
            _edge("e2", "start", "low", port="false"),
            _edge("e3", "high", "join_high"),
            _edge("e4", "low", "join_low"),
        ],
        start_node_id="start",
    )
    placed = _positions(workflow)
    # Same column, and the arm order follows the follow-up column it feeds.
    assert placed["high"][0] == placed["low"][0]
    assert (placed["high"][1] < placed["low"][1]) == (placed["join_high"][1] < placed["join_low"][1])
    # No crossing: each arm sits on the same row band as its continuation.
    assert placed["high"][1] == placed["join_high"][1]
    assert placed["low"][1] == placed["join_low"][1]


def test_short_column_is_centred_not_top_hung() -> None:
    """One node feeding two sits level with the middle of the pair, so both
    edges leave at symmetric angles instead of one horizontal and one steep."""
    workflow = WorkflowJSON(
        nodes=[_node("start", "if_else"), _node("top"), _node("bottom")],
        edges=[_edge("e1", "start", "top", port="true"), _edge("e2", "start", "bottom", port="false")],
        start_node_id="start",
    )
    placed = _positions(workflow)
    assert placed["start"][1] == (placed["top"][1] + placed["bottom"][1]) / 2


def test_every_node_is_placed_even_unreachable() -> None:
    """A stray node no edge reaches still gets a spot past the graph's end."""
    workflow = WorkflowJSON(
        nodes=[_node("start"), _node("finish", "end"), _node("stray")],
        edges=[_edge("e1", "start", "finish")],
        start_node_id="start",
    )
    placed = _positions(workflow)
    assert placed["stray"][0] > placed["finish"][0]


def test_nothing_lands_on_the_canvas_edge() -> None:
    """The top-left node keeps its whole box, ports included, inside the canvas."""
    workflow = WorkflowJSON(
        nodes=[_node("start"), _node("finish", "end")],
        edges=[_edge("e1", "start", "finish")],
        start_node_id="start",
    )
    for x, y in _positions(workflow).values():
        assert x >= MARGIN
        assert y >= MARGIN


def test_deterministic() -> None:
    """Same document in, same placement out — sweeps must not oscillate."""
    workflow = WorkflowJSON(
        nodes=[_node("start", "if_else"), _node("b"), _node("a"), _node("merge"), _node("finish", "end")],
        edges=[
            _edge("e1", "start", "a", port="true"),
            _edge("e2", "start", "b", port="false"),
            _edge("e3", "a", "merge"),
            _edge("e4", "b", "merge"),
            _edge("e5", "merge", "finish"),
        ],
        start_node_id="start",
    )
    assert _positions(workflow) == _positions(workflow)
