"""Auto-repairing a composed graph that ends its answer on a raw tool payload.

`raw_payload_as_answer` (`src.workflow.validation`) refuses a composed graph
whose last word before `end` is an `mcp` node's own JSON — a rule the composer's
own prompt already states (principle 3) and already tells the model how to fix,
word for word: *"thêm một bước Agent/LLM ở giữa nó và bước Kết thúc"*.

The measured problem was that asking twice was not reliable. A live battery of
14 diverse compositions found this the dominant failure — 4 of 6 rejections
across 28 real attempts were exactly this, on graphs that were otherwise
correct. `_narrated` is that same fix, done as graph surgery with no model call,
so the platform stops gambling a paid retry on a rule its own validator can
already both detect and repair.

These tests exercise `_narrated` directly — no model, no HTTP — because the
property under test is about the graph transformation, not about a prompt.
`tests/test_api/test_workflow_draft.py` pins the one thing this module cannot:
that the repair actually replaces a redraw in the full `compose_workflow` loop.
"""

from src.models.workflow import WorkflowJSON
from src.workflow.composition import _narrated

ALLOWLIST = ["gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o-mini"]


def _graph(nodes, edges, start="a"):
    return WorkflowJSON.model_validate({"start_node_id": start, "nodes": nodes, "edges": edges, "metadata": {}})


def test_an_mcp_node_ending_the_answer_gets_a_narrator_spliced_in():
    """The measured failure, closed: `mcp -> end` becomes `mcp -> agent_llm -> end`."""
    graph = _graph(
        nodes=[{"id": "fetch", "type": "mcp", "config": {}}, {"id": "done", "type": "end", "config": {}}],
        edges=[{"id": "e1", "source_node_id": "fetch", "source_port": "done", "target_node_id": "done"}],
        start="fetch",
    )

    fixed = _narrated(graph, ALLOWLIST)

    by_id = {n.id: n for n in fixed.nodes}
    assert len(fixed.nodes) == 3
    (new_id,) = set(by_id) - {"fetch", "done"}
    assert by_id[new_id].type.value == "agent_llm"
    assert by_id[new_id].config["prompt"]
    assert by_id[new_id].config["model"] in ALLOWLIST
    path = {e.source_node_id: e for e in fixed.edges}
    assert path["fetch"].target_node_id == new_id
    assert path[new_id].target_node_id == "done"


def test_a_graph_that_already_narrates_is_left_exactly_alone():
    """No false positives: an `mcp` node feeding an `agent_llm` is already correct."""
    graph = _graph(
        nodes=[
            {"id": "fetch", "type": "mcp", "config": {}},
            {"id": "say", "type": "agent_llm", "config": {"prompt": "kể lại", "model": "gpt-4.1-mini"}},
            {"id": "done", "type": "end", "config": {}},
        ],
        edges=[
            {"id": "e1", "source_node_id": "fetch", "source_port": "done", "target_node_id": "say"},
            {"id": "e2", "source_node_id": "say", "source_port": "done", "target_node_id": "done"},
        ],
        start="fetch",
    )

    fixed = _narrated(graph, ALLOWLIST)

    assert fixed == graph


def test_only_the_branch_that_actually_ends_raw_is_touched():
    """A branching graph with one honest arm and one raw one: only the raw one changes.

    Pinned because the naive fix — "any graph containing a flagged node gets a
    narrator" — would touch the wrong edge on a graph with more than one path to
    `end`, and a branching composition (`if_else`, `while`) is an ordinary shape
    here, not an edge case.
    """
    graph = _graph(
        nodes=[
            {"id": "route", "type": "if_else", "config": {"condition": "khẩn cấp?"}},
            {"id": "post", "type": "mcp", "config": {}},
            {"id": "say_ok", "type": "agent_llm", "config": {"prompt": "trả lời", "model": "gpt-4.1-mini"}},
            {"id": "done", "type": "end", "config": {}},
        ],
        edges=[
            {"id": "e1", "source_node_id": "route", "source_port": "true", "target_node_id": "post"},
            {"id": "e2", "source_node_id": "route", "source_port": "false", "target_node_id": "say_ok"},
            {"id": "e3", "source_node_id": "post", "source_port": "done", "target_node_id": "done"},
            {"id": "e4", "source_node_id": "say_ok", "source_port": "done", "target_node_id": "done"},
        ],
        start="route",
    )

    fixed = _narrated(graph, ALLOWLIST)

    assert len(fixed.nodes) == 5
    assert len(fixed.edges) == 5
    # `say_ok`'s honest edge into `done` is untouched, id and all.
    assert any(e.id == "e4" and e.target_node_id == "done" for e in fixed.edges)
    # `post`'s edge no longer points straight at `done`.
    assert not any(e.source_node_id == "post" and e.target_node_id == "done" for e in fixed.edges)


def test_two_flagged_nodes_each_get_their_own_narrator_with_unique_ids():
    """Two independent raw endings — the id-collision path, exercised for real."""
    graph = _graph(
        nodes=[
            {"id": "route", "type": "if_else", "config": {"condition": "x"}},
            {"id": "a", "type": "mcp", "config": {}},
            {"id": "b", "type": "mcp", "config": {}},
            {"id": "done", "type": "end", "config": {}},
        ],
        edges=[
            {"id": "e1", "source_node_id": "route", "source_port": "true", "target_node_id": "a"},
            {"id": "e2", "source_node_id": "route", "source_port": "false", "target_node_id": "b"},
            {"id": "e3", "source_node_id": "a", "source_port": "done", "target_node_id": "done"},
            {"id": "e4", "source_node_id": "b", "source_port": "done", "target_node_id": "done"},
        ],
        start="route",
    )

    fixed = _narrated(graph, ALLOWLIST)

    added = {n.id for n in fixed.nodes} - {"route", "a", "b", "done"}
    assert len(added) == 2
    assert len({e.id for e in fixed.edges}) == len(fixed.edges), "edge ids must stay unique"


def test_the_narrator_never_names_a_model_outside_the_allowlist():
    """The repair must obey the same admin allowlist the composer itself is bound by."""
    graph = _graph(
        nodes=[{"id": "fetch", "type": "mcp", "config": {}}, {"id": "done", "type": "end", "config": {}}],
        edges=[{"id": "e1", "source_node_id": "fetch", "source_port": "done", "target_node_id": "done"}],
        start="fetch",
    )

    fixed = _narrated(graph, ["gpt-4o-mini"])

    narrator = next(n for n in fixed.nodes if n.type.value == "agent_llm")
    assert narrator.config["model"] == "gpt-4o-mini"


def test_a_graph_with_nothing_flagged_is_returned_unchanged_by_identity():
    """The common case — most compositions never trip this rule — costs nothing extra."""
    graph = _graph(
        nodes=[
            {"id": "think", "type": "agent_llm", "config": {"prompt": "trả lời", "model": "gpt-4.1-mini"}},
            {"id": "done", "type": "end", "config": {}},
        ],
        edges=[{"id": "e1", "source_node_id": "think", "source_port": "done", "target_node_id": "done"}],
        start="think",
    )

    assert _narrated(graph, ALLOWLIST) is graph
