"""Where the nodes of a freshly drawn graph sit — the one left-to-right pass.

The Workflow Composer (ADR-0041) hands back a graph with no coordinates on it,
and it has to: a model asked for `position` writes plausible round numbers that
overlap, and asking it not to bother is what `src.workflow.refinement` already
does for the same reason. So somebody has to place the nodes, once, before the
creator first sees them.

**Only for a graph nobody has arranged yet.** A workflow the creator has opened
on the canvas carries coordinates *they* chose, and ADR-0027 is explicit that
re-opening it must not undo that — which is why `refinement` places only the
nodes an edit introduced and leaves every existing one where it was. This module
is the other case, and the only one: a graph that has never been on a screen.

The rule is depth from the start node, because that is what a reader of a
workflow is actually following. A node's column is how many steps of control
separate it from where the run begins, so control flows rightwards across the
canvas and a branch's two arms sit in the same column as each other rather than
wherever the document happened to list them.

Within a column, nodes sit in **barycenter order**: each is pulled toward the
average height of the nodes feeding it, sweep after sweep, which is the classic
Sugiyama crossing-reduction step. Document order — what this module used before
— draws an X across the canvas the moment the composer lists a branch's arms in
the other order; barycenter order untangles exactly that. Columns are then
centred on a shared horizontal axis, so a short column sits level with the
middle of a tall one instead of hanging from its top — edges run near-horizontal
and the graph reads as one flow rather than a staircase.
"""

from collections import defaultdict, deque

from src.models.workflow import Position, WorkflowJSON

#: How far apart two columns sit, and two rows within a column. Wide enough that
#: a node box (210 units on the canvas) and the edge leaving it are both readable
#: at once; tall enough that a branch's two arms do not touch.
COLUMN_WIDTH = 260
ROW_HEIGHT = 160

#: Where the top-left of the graph lands. Not zero: a node flush against the
#: canvas edge has its input port half outside the scrollable area, which reads
#: as a port that cannot be wired.
MARGIN = 60

#: How many barycenter sweeps to run. Two passes each way settles every graph a
#: composer actually emits (they are small and mostly tree-shaped); the cap only
#: exists so a pathological cycle of ties cannot oscillate forever.
SWEEPS = 4


def laid_out(workflow: WorkflowJSON) -> WorkflowJSON:
    """`workflow` with every node placed by its distance from `start_node_id`.

    Total, never partial: every node comes back with a position, including ones
    no edge reaches. An unreachable node is a mistake the validator reports and
    the creator fixes on the canvas, and a node they cannot *see* is one they
    cannot fix — so it is placed below the graph rather than left at the origin
    under whatever else landed there.
    """
    columns = _columns(workflow)
    ordered = _ordered_rows(workflow, columns)

    tallest = max((len(rows) for rows in ordered.values()), default=1)
    positions: dict[str, Position] = {}
    for column, rows in ordered.items():
        # Centre the column on the axis of the tallest one, snapped to half-row
        # steps so two adjacent columns never sit an unreadable few pixels apart.
        offset = (tallest - len(rows)) / 2
        for row, node_id in enumerate(rows):
            positions[node_id] = Position(
                x=float(MARGIN + column * COLUMN_WIDTH),
                y=float(MARGIN + (offset + row) * ROW_HEIGHT),
            )

    placed = [node.model_copy(update={"position": positions[node.id]}) for node in workflow.nodes]
    return workflow.model_copy(update={"nodes": placed})


def _columns(workflow: WorkflowJSON) -> dict[str, int]:
    """Each node's column: its distance from the start, breadth-first.

    Breadth-first rather than longest-path, so a node that both a branch and its
    bypass lead to sits next to the *earlier* of the two. Longest-path would push
    it right until it cleared every arm, opening a gap across the middle of the
    graph that means nothing to the person reading it.

    A node nothing reaches gets the column after the deepest one that is
    reachable, which puts every stray node together in one place off the end
    instead of scattered through the graph at column zero.
    """
    onward: defaultdict[str, list[str]] = defaultdict(list)
    for edge in workflow.edges:
        onward[edge.source_node_id].append(edge.target_node_id)

    depth = {workflow.start_node_id: 0}
    pending = deque([workflow.start_node_id])
    while pending:
        node_id = pending.popleft()
        for target in onward[node_id]:
            if target not in depth:
                depth[target] = depth[node_id] + 1
                pending.append(target)

    stray = max(depth.values(), default=-1) + 1
    return {node.id: depth.get(node.id, stray) for node in workflow.nodes}


def _ordered_rows(workflow: WorkflowJSON, columns: dict[str, int]) -> dict[int, list[str]]:
    """Each column's nodes, top to bottom, after the barycenter sweeps.

    A sweep walks the columns in one direction and stable-sorts each by the mean
    row of its neighbours in the column the sweep came from — forward sweeps
    follow the edges, backward sweeps follow them in reverse, which is how a
    node with only outgoing edges still gets pulled level with what it feeds.
    A node with no neighbour in that direction keeps the row it had (its own
    current row is its key), so ties and strays never jump around between
    sweeps; with the sort stable, the whole pass is deterministic.
    """
    incoming: defaultdict[str, list[str]] = defaultdict(list)
    outgoing: defaultdict[str, list[str]] = defaultdict(list)
    for edge in workflow.edges:
        incoming[edge.target_node_id].append(edge.source_node_id)
        outgoing[edge.source_node_id].append(edge.target_node_id)

    ordered: defaultdict[int, list[str]] = defaultdict(list)
    for node in workflow.nodes:  # document order seeds the very first sweep
        ordered[columns[node.id]].append(node.id)
    sequence = sorted(ordered)

    for sweep in range(SWEEPS):
        forward = sweep % 2 == 0
        walk = sequence if forward else list(reversed(sequence))
        neighbours = incoming if forward else outgoing
        for column in walk:
            rows = {node_id: row for column_rows in ordered.values() for row, node_id in enumerate(column_rows)}
            ordered[column].sort(
                key=lambda node_id: _barycenter(neighbours[node_id], rows, fallback=rows[node_id])
            )

    return {column: ordered[column] for column in sequence}


def _barycenter(neighbour_ids: list[str], rows: dict[str, int], *, fallback: int) -> float:
    """The mean row of `neighbour_ids`, or `fallback` when there are none to average.

    A neighbour with no row is skipped rather than looked up, because an edge may
    name a node that does not exist: `rows` is built from the graph's *nodes*
    while `neighbour_ids` comes from its *edges*, and a composer that writes
    `... → agent_report_rejected` without ever defining that node produces
    exactly that mismatch (measured twice on 2026-08-30). Indexing blind turned
    it into `KeyError: 'agent_report_rejected'` out of the middle of layout —
    which the draft endpoint has no handler for, so the creator got a bare HTTP
    500 for a graph whose real problem `graph_problems` already states in one
    sentence (*"Cạnh 'e7' trỏ tới node 'agent_report_rejected' không có thật"*).
    Placing is not the place to refuse a graph; laying it out anyway is what
    lets the creator see that sentence and fix the edge on the canvas.
    """
    known = [rows[neighbour] for neighbour in neighbour_ids if neighbour in rows]
    if not known:
        return float(fallback)
    return sum(known) / len(known)
