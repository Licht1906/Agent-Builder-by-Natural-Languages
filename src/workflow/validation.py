"""Whether a graph is a thing the runtime could honestly execute — ADR-0021 §5–6.

Five rules, and each one exists because breaking it produces a run nobody can
account for afterwards:

- the start is a node that is actually there (ADR-0021 §4);
- a cycle only ever enters through a `While` node's `loop` port, because that is
  the one port with a mandatory `max_iterations` behind it;
- an output port drives at most one edge — two edges out of one port is a fork
  nothing decided;
- every path from the start reaches an `End`;
- every node's `config` is the shape its type declares, with nothing left blank;
- no path reaches an `MCP` node whose action needs a person, without passing one.

**Reported, then refused — in that order, and by two different functions.**
`graph_problems` answers "what is wrong with this graph" and is safe to call at
any moment, including on a draft the creator is halfway through; the canvas
(#44) draws its rings from it. `ensure_confirmable` is the gate, and runs at
exactly one transition: `workflow_pending_confirmation → workflow_confirmed`
(ADR-0022 §6). Nothing here ever runs on a draft save, because a creator who
cannot save an unfinished graph cannot finish one.

`graph_warnings` is a third thing again: what is worth *saying* about a graph
that is nonetheless allowed. Since #47 it has one job left. ADR-0023 §5's
refusal is conditional on the *action's* `requires_human_approval`, which is now
a real question with a real answer (`src.workflow.tools`) — so an unguarded
`MCP` node whose action needs a person is refused outright. What remains a
warning is the case where the question cannot be asked at all: a `tool_id` or an
`action` nothing in the registry resolves, which is the ordinary state of a
freshly generated draft and of every creator-registered tool until that half of
the registry exists. Refusing on it would refuse graphs no ADR asked to refuse;
saying nothing would leave the canvas silent about a side effect nobody is
standing in front of.

Every problem carries the node or edge it is about, not just a sentence. A
canvas has to put a red ring somewhere, and re-deriving which node a sentence
was about by parsing the sentence is how that ring ends up on the wrong node.
"""

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from pydantic import ValidationError

from src.models.node_registry import (
    McpActionMode,
    McpApproval,
    McpConfig,
    NodeType,
    config_model_of,
    outputs_of,
    unfilled_fields,
)
from src.models.workflow import Edge, NodeInstance, WorkflowJSON
from src.workflow.tools import ToolAction, offered_actions, resolved_action, riskiest


@dataclass(frozen=True, slots=True)
class GraphProblem:
    """One thing wrong with a graph, and where on the canvas it is.

    `message` is written for the creator, in the language the rest of the
    platform speaks to them in. `node_id`/`edge_id` are for the canvas, which
    needs to mark the thing rather than read about it.
    """

    message: str
    node_id: str | None = None
    edge_id: str | None = None


class WorkflowGraphError(ValueError):
    """A workflow was asked to be confirmed while it still has problems. User-facing."""


def graph_problems(workflow: WorkflowJSON) -> tuple[GraphProblem, ...]:
    """Everything wrong with `workflow`, in the order a creator would fix it.

    Structural problems that are local to one node or edge come first, then the
    two whole-graph judgements (cycles, termination). The order matters on a
    canvas that lists them: a dangling edge is a thing to reconnect, while "this
    branch never ends" is a thing to think about, and burying the former under
    the latter is how a creator fixes the wrong problem first.

    A start node nobody can find stops the walk short. Cycle and termination
    both mean "from the start", and with no start there is no claim to make —
    reporting that one missing field is more useful than reporting that every
    node in the graph is unreachable from a node that does not exist.
    """
    return _problems(workflow, blanks=True)


def structural_problems(workflow: WorkflowJSON) -> tuple[GraphProblem, ...]:
    """`graph_problems` less the one complaint a fresh draft is *supposed* to make.

    The same rules, asked of the graph's **shape** alone: ids, config types,
    edges, ports, cycles, termination, guarded side effects. What it leaves out
    is `Node 'x' còn chưa điền: ...` — a field nobody has filled yet, which
    ADR-0022 §5 calls the ordinary state of a draft and the canvas is built to
    show.

    It exists for one caller: the Workflow Composer (ADR-0041), which asks a
    model to draw a graph and has to decide whether to hand the reply back for
    another attempt. Retrying on a blank field would be asking the model to
    invent the very things ADR-0025 §5 forbids it from inventing — a creator's
    file key, an MCP action — while retrying on a branch that reaches no `End`
    is asking it to fix a graph it genuinely drew wrong. One list is the
    composer's business and the other is the creator's.
    """
    return _problems(workflow, blanks=False)


def _problems(workflow: WorkflowJSON, *, blanks: bool) -> tuple[GraphProblem, ...]:
    """Every rule in `graph_problems`' own order, with the blank-field report optional."""
    problems = [
        *_duplicate_ids(workflow),
        *_node_configs(workflow, blanks=blanks),
        *_collect_targets(workflow),
        *_edge_endpoints(workflow),
        *_port_fan_out(workflow),
    ]
    if workflow.node(workflow.start_node_id) is None:
        problems.append(GraphProblem(f"Node bắt đầu {workflow.start_node_id!r} không có trong workflow"))
        return tuple(problems)
    problems.extend(_cycles(workflow))
    problems.extend(_termination(workflow))
    problems.extend(_unguarded_side_effects(workflow))
    return tuple(problems)


def graph_warnings(workflow: WorkflowJSON) -> tuple[GraphProblem, ...]:
    """What is worth telling the creator about `workflow` without refusing it.

    One warning: an unguarded `MCP` node whose `{tool_id, action}` resolves to
    nothing in the registry. The question ADR-0023 §5 asks — *does this action
    need a person?* — has no answer for a tool nobody has registered, and both
    ways of pretending it does are worse than saying so. Refusing would block a
    freshly generated draft, whose `tool_id` the creator has not yet bound on the
    canvas; staying silent would leave the canvas quiet about a side effect
    nobody is standing in front of, which is the case #44's third acceptance
    criterion names by example.

    An unguarded node whose action *does* resolve is no longer here at all — it
    is a refusal in `graph_problems` (`_unguarded_side_effects`), which is where
    ADR-0023 §5 always intended it to be once the registry existed.

    Kept a `GraphProblem` rather than a type of its own. It carries the same two
    things — a sentence and the node it is about — and a second dataclass
    differing in nothing but its name would make every consumer handle two
    shapes to draw one ring.
    """
    return (
        *_fallback_notice(workflow),
        *_idle_collectors(workflow),
        *(
            GraphProblem(
                f"Node {node_id!r} gọi tool có thể gây tác dụng phụ, nhưng không phải đường nào tới nó "
                f"cũng đi qua một bước Human Approval — chưa tra được tool này trong registry nên chưa "
                f"khẳng định được là có bắt buộc duyệt hay không; cân nhắc thêm bước duyệt phía trước",
                node_id=node_id,
            )
            for node_id, config, action in _unguarded_actions(workflow)
            if action is None and config.require_approval is not McpApproval.ALWAYS
        ),
        *_raw_payload_as_answer(workflow),
        *_unreachable_nodes(workflow),
    )


def _fallback_notice(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """Why this draft is a template and not a graph drawn for the creator's own problem.

    `metadata.fallback_reason` is written by the Generator when the composer
    could not produce a valid graph (`src.workflow.generator._composed`). A
    warning and not a problem: the template confirms like any other graph; what
    the creator needs is to know that it *is* one, and why, before they spend
    an hour editing a shape that was never about their business.
    """
    if workflow.metadata.fallback_reason:
        yield GraphProblem(
            "Không soạn được workflow riêng cho bài toán này nên đây là mẫu gần nhất — sửa tiếp trên canvas, "
            f"hoặc viết rõ hơn phần hành động trong đặc tả rồi soạn lại. Lý do: {workflow.metadata.fallback_reason}"
        )


def _unreachable_nodes(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """A node the start can never reach — drawn, configured, and never run.

    Found in a stored graph: an `mcp` node on `google_drive` in `full` mode,
    sitting beside a wired `agent_llm → end`, with no edge touching it. Every
    gate passed, because every gate asks about paths from the start and this
    node was on none. The creator who drew it almost certainly believed it was
    part of the agent.

    A warning, not a refusal: a node parked on the canvas while its creator
    rewires around it is a legitimate mid-edit state, and refusing to save
    would be refusing the canvas. But it must be *said* — an unreachable
    side-effect node is the most misleading object a graph can hold, an action
    that looks armed and never fires.
    """
    reachable: set[str] = set()
    frontier = [workflow.start_node_id]
    outgoing: dict[str, list[str]] = {}
    for edge in workflow.edges:
        outgoing.setdefault(edge.source_node_id, []).append(edge.target_node_id)
    while frontier:
        node_id = frontier.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        frontier.extend(outgoing.get(node_id, ()))
    for node in workflow.nodes:
        if node.id not in reachable:
            yield GraphProblem(
                f"Node {node.id!r} không nối vào đâu từ bước bắt đầu nên sẽ không bao giờ chạy — "
                f"nối nó vào workflow, hoặc xoá đi nếu không dùng",
                node_id=node.id,
            )


#: The two node types that write into `messages`, i.e. that decide what the
#: agent actually *says* (`runtime.py`: `_run_agent_llm` and `_run_mcp`). Every
#: other type either files evidence (`knowledge_search` → `findings`) or only
#: moves control, so it cannot be the last word.
_SPEAKING_TYPES = frozenset({NodeType.AGENT_LLM, NodeType.MCP})


def raw_payload_as_answer(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """`_raw_payload_as_answer`, public for the composer, which refuses what the canvas only warns about."""
    return _raw_payload_as_answer(workflow)


def _raw_payload_as_answer(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """An `MCP` node that is the last thing to speak before the run ends.

    Reported after a real run answered a person with `{}`. Nothing had gone
    wrong: on `mcp → end` the last message is the tool's own response body, so
    the reply *is* the payload. The gap was that nothing said so — the graph
    passed `graph_problems`, passed confirm, and the creator found out by
    reading JSON in a chat bubble.

    Walks **backwards from every `end` node through the nodes that say nothing**
    rather than looking only at edges into `end`. Checking incident edges alone
    would let a single `if_else` between the call and the door hide the payload,
    and a check that one branch node defeats is not worth the line it costs.

    A warning rather than a refusal, deliberately: ending on an action is a real
    shape — an agent whose job is to file the ticket and stop is finished when
    the ticket is filed, and its caller may want the raw result. What the
    platform owes the creator is that nobody ships that shape by accident.
    """
    seen: set[str] = set()
    speakers: set[str] = set()
    frontier = [node.id for node in workflow.nodes if node.type is NodeType.END]
    incoming: dict[str, list[str]] = {}
    for edge in workflow.edges:
        incoming.setdefault(edge.target_node_id, []).append(edge.source_node_id)

    while frontier:
        node_id = frontier.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        for source_id in incoming.get(node_id, ()):
            source = workflow.node(source_id)
            if source is None:
                continue
            if source.type in _SPEAKING_TYPES:
                # It has the last word on this path; stop walking past it.
                speakers.add(source_id)
            else:
                frontier.append(source_id)

    for node in workflow.nodes:
        if node.id in speakers and node.type is NodeType.MCP:
            yield GraphProblem(
                f"Câu trả lời cuối cùng của agent sẽ là dữ liệu thô do node {node.id!r} trả về "
                f"(thường là JSON), vì sau nó không còn bước nào diễn đạt lại thành lời. "
                f"Nếu người dùng là người đọc, thêm một bước Agent/LLM ở giữa nó và bước Kết thúc",
                node_id=node.id,
            )


def _unguarded_side_effects(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """ADR-0023 §5's refusal: a side effect a person was supposed to stand in front of.

    Enforced at save time rather than caught at run time because that is what
    gives the rule teeth. A `requires_human_approval` checked only when the run
    reaches the node would let a workflow be confirmed, evaluated and sent for
    approval while carrying an ungated `send_email`, and the failure would arrive
    the first time somebody actually ran it — after the email.

    Conditioned on the *action*, never on the node type, which is the whole
    difference from a blanket rule: an `MCP` node reading an inbox is `low` risk
    and needs nobody's permission, and refusing it would be a stricter rule than
    any ADR asked for. `requires_human_approval` is itself derived from the
    action's effect (`src.workflow.tools`), so what is being enforced here is
    still an effect rather than a claim.

    Silent about an action nobody can resolve — that is `graph_warnings`'.

    **`require_approval: always` is refused on the same footing**, and is the
    one reading here that does not consult the registry at all: the creator has
    said a person stands in front of this node, and whether the action happens
    to be a read the platform would have waved through is beside their point.
    It is checked first so that a node carrying both reasons is refused with the
    one the creator can act on — they set that field; they did not set the
    action's risk. `McpApproval` has no `never`, so nothing on this path can
    lift the gate, only add one.
    """
    for node_id, config, action in _unguarded_actions(workflow):
        if config.require_approval is McpApproval.ALWAYS:
            yield GraphProblem(
                f"Node {node_id!r} được đặt là luôn phải có người duyệt (require_approval = 'always'), "
                f"nên mọi đường tới nó đều phải đi qua một bước Human Approval — hiện có đường không đi qua",
                node_id=node_id,
            )
        elif action is not None and action.requires_human_approval:
            yield GraphProblem(
                f"Node {node_id!r} gọi action {action.name!r} có tác dụng phụ mức {action.risk_level.value!r}, "
                f"nên mọi đường tới nó đều phải đi qua một bước Human Approval — hiện có đường không đi qua",
                node_id=node_id,
            )


def _unguarded_actions(workflow: WorkflowJSON) -> Iterator[tuple[str, McpConfig, ToolAction | None]]:
    """Each unguarded `MCP` node with its config and the action it names.

    One walk answering every question asked of it, so the refusal and the
    warning cannot disagree about which nodes are unguarded — they are readings
    of one list, split only by whether the registry could say anything about the
    action.

    The config travels with the action because `require_approval` is a fact
    about the *node* while `requires_human_approval` is a fact about the action,
    and the refusal below turns on either of them. Re-parsing the config in a
    second walk would be a second chance to disagree about which nodes those
    are.
    """
    for node_id in unguarded_mcp_nodes(workflow):
        node = workflow.node(node_id)
        if node is None:
            continue
        try:
            config = McpConfig.model_validate(node.config)
        except ValidationError:
            # Already reported by `_node_configs`; a second sentence about the
            # same malformed config would only crowd out the first.
            continue
        yield node_id, config, guarded_action(config)


def guarded_action(config: McpConfig) -> ToolAction | None:
    """The action an `MCP` node's approval gate is keyed to, whichever mode it is in.

    `single` names one action and that is the one. `full` names a *set*, and the
    gate has to answer for the worst of it — a set of four reads and one
    `send_email` is a node that can send an email, so it is guarded exactly as a
    node naming `send_email` would be (`tools.riskiest`).

    `None` when the registry can say nothing: an unbound `tool_id`, an action
    name nobody registered, or a `full` node whose allowed set has been narrowed
    to nothing. Callers read `None` as "not resolvable yet", which is a warning
    on the canvas rather than a refusal — the same answer this function has
    always given for an unbound node.
    """
    if config.action_mode is McpActionMode.FULL:
        offered = offered_actions(config.tool_id, config.allowed_actions)
        return None if offered is None else riskiest(offered[1])
    resolved = resolved_action(config.tool_id, config.action)
    return None if resolved is None else resolved[1]


def ensure_confirmable(workflow: WorkflowJSON) -> None:
    """Raise `WorkflowGraphError` unless `workflow` is ready to become immutable.

    Every problem in one refusal rather than the first one found. A creator
    fixing a graph one rejection at a time is a creator making four round trips
    to learn four things the validator already knew on the first.
    """
    problems = graph_problems(workflow)
    if problems:
        raise WorkflowGraphError("Workflow chưa hợp lệ: " + "; ".join(problem.message for problem in problems))


def approval_guards_every_mcp_path(workflow: WorkflowJSON) -> bool:
    """Whether a person stands between the start and every `MCP` node in `workflow`.

    ADR-0023 §5's structural invariant, asked of a graph rather than enforced on
    one — which is what lets template selection (ADR-0025 §3) check it *before*
    generating a draft. Refusing at selection time avoids handing a creator a
    workflow that can never pass confirmation, and it is the same question the
    graph validator will eventually refuse a save on, asked in one place so the
    two answers cannot differ.

    Walked forward from the start carrying "has a `HumanApproval` been passed",
    because the claim is about *paths*, not about the graph containing an
    approval node somewhere. A node is re-walked when it is arrived at in the
    other state, which is finite for the same reason the state itself is: there
    are two states, so each node is walked at most twice, loop or no loop.

    Only the `approved` port carries the guard onward. Leaving a `HumanApproval`
    node by `rejected` is a person saying *no*, and a path that reached the side
    effect that way would have routed through an approval without ever having
    one — the letter of the rule met, its point missed entirely.

    Vacuously true for a graph with no `MCP` node — a workflow with no side
    effect has nothing to guard.

    Deliberately blind to *which* action each node calls, unlike
    `_unguarded_side_effects` which is the confirm-time refusal built on this
    walk. Template selection (ADR-0025 §3) asks this before a workflow exists at
    all — the template's `MCP` node has an empty `tool_id` at that point — so a
    question conditioned on a resolved action would have nothing to resolve.
    What selection is actually checking is the *shape*: does this template put a
    person in front of its side effect, whatever that side effect turns out to
    be.
    """
    return not unguarded_mcp_nodes(workflow)


def unguarded_mcp_nodes(workflow: WorkflowJSON) -> tuple[str, ...]:
    """The `MCP` nodes some path reaches without passing a `HumanApproval` first.

    The per-node form of ADR-0023 §5's invariant, and the one the walk actually
    computes — `approval_guards_every_mcp_path` is this asked as a yes/no. Named
    nodes rather than a bool because a canvas has to ring *which* side effect is
    unguarded: a graph with one guarded `MCP` node and one exposed one would
    otherwise get both ringed, and the creator would go and re-check the node
    that was already fine.

    Walked forward from the start carrying "has a `HumanApproval` been passed",
    because the claim is about *paths*, not about the graph containing an
    approval node somewhere. A node is re-walked when it is arrived at in the
    other state, which is finite for the same reason the state itself is: there
    are two states, so each node is walked at most twice, loop or no loop.

    Only the `approved` port carries the guard onward. Leaving a `HumanApproval`
    node by `rejected` is a person saying *no*, and a path that reached the side
    effect that way would have routed through an approval without ever having
    one — the letter of the rule met, its point missed entirely.

    In document order, so the canvas lists two unguarded nodes the way the
    document holds them rather than in whatever order the walk happened to
    reach them.
    """
    leaving: defaultdict[str, list[Edge]] = defaultdict(list)
    for edge in _valid_edges(workflow):
        leaving[edge.source_node_id].append(edge)

    exposed: set[str] = set()
    seen: set[tuple[str, bool]] = {(workflow.start_node_id, False)}
    pending = [(workflow.start_node_id, False)]
    while pending:
        node_id, approved = pending.pop()
        node = workflow.node(node_id)
        if node is None:
            continue
        if node.type is NodeType.MCP and not approved:
            exposed.add(node_id)
        for edge in leaving[node_id]:
            onward = approved or (node.type is NodeType.HUMAN_APPROVAL and edge.source_port == "approved")
            if (edge.target_node_id, onward) not in seen:
                seen.add((edge.target_node_id, onward))
                pending.append((edge.target_node_id, onward))
    return tuple(node.id for node in workflow.nodes_of_type(NodeType.MCP) if node.id in exposed)


def _duplicate_ids(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """Two nodes, or two edges, answering to one id.

    An edge naming a duplicated id points at both nodes, and the runtime would
    silently pick whichever it walked into first — the kind of ambiguity that
    shows up as a run doing something the diagram does not.
    """
    for node_id in _repeated(node.id for node in workflow.nodes):
        yield GraphProblem(f"Có hai node cùng id {node_id!r}", node_id=node_id)
    for edge_id in _repeated(edge.id for edge in workflow.edges):
        yield GraphProblem(f"Có hai cạnh cùng id {edge_id!r}", edge_id=edge_id)


def _node_configs(workflow: WorkflowJSON, *, blanks: bool = True) -> Iterator[GraphProblem]:
    """Each node's `config` against the shape `NODE_REGISTRY` declares for its type.

    Two failures, deliberately distinguished. A config of the wrong *shape* is a
    document written by something that does not know the registry. A config of
    the right shape with blanks in it is the ordinary state of a fresh draft —
    the Generator leaves placeholders where its LLM had nothing to say, and the
    creator fills them on the canvas. Both refuse confirmation; only the second
    one is expected to be common, so it names the fields still waiting.

    `blanks=False` drops the second report and keeps the first, which is the
    whole of `structural_problems` — see its docstring for who asks that way and
    why the two are worth telling apart.
    """
    for node in workflow.nodes:
        try:
            config_model_of(node.type)(**node.config)
        except (ValidationError, TypeError) as exc:
            yield GraphProblem(f"Cấu hình của node {node.id!r} không đúng dạng của {node.type.value}: {exc}", node.id)
            continue
        waiting = unfilled_fields(node.type, node.config) if blanks else ()
        if waiting:
            yield GraphProblem(f"Node {node.id!r} còn chưa điền: {', '.join(waiting)}", node.id)


def _collect_targets(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """A `collect` node's `action_node` must name an `mcp` node in single mode — ADR-0052.

    What the collector asks for is read off the action bound there, so a
    reference to nothing or to a node of another type is a collector with
    nothing to read — a problem. A blank `action_node` is the draft's ordinary
    state and is reported by `unfilled_fields` like every other blank, not
    twice here. A target in `full` mode is a *warning* (`_idle_collectors`),
    not a problem: the runtime passes such a collector through at no cost, so
    the graph works; the creator just gains nothing from the node.
    """
    for node in workflow.nodes:
        if node.type is not NodeType.COLLECT:
            continue
        target_id = str(node.config.get("action_node") or "").strip()
        if not target_id:
            continue
        target = workflow.node(target_id)
        if target is None or target.type is not NodeType.MCP:
            yield GraphProblem(
                f"Node {node.id!r} trỏ tới {target_id!r}, không phải một node mcp có trong workflow", node.id
            )


def _idle_collectors(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """A `collect` node in front of a `full` mode `mcp` node checks nothing — worth saying, not refusing.

    The meta agent draws a collector before every user-driven action without
    knowing which mode the creator will bind (ADR-0052); when they choose
    `full`, the model picks the action and fills its arguments at run time and
    the collector has no fixed schema to read. The runtime lets it through
    (`_run_collect`), so the graph confirms and runs; this line tells the
    creator the node is idle rather than letting them believe it guards.
    """
    for node in workflow.nodes:
        if node.type is not NodeType.COLLECT:
            continue
        target = workflow.node(str(node.config.get("action_node") or "").strip())
        if target is not None and str(target.config.get("action_mode", "")) == McpActionMode.FULL.value:
            yield GraphProblem(
                f"Node {node.id!r} đứng trước node mcp {target.id!r} ở chế độ full nên không kiểm được gì — "
                "bỏ node này, hoặc chuyển node mcp sang một action cố định",
                node.id,
            )


def _edge_endpoints(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """Each edge's two ends: nodes that exist, leaving a port its type actually has."""
    for edge in workflow.edges:
        source = workflow.node(edge.source_node_id)
        if source is None:
            yield GraphProblem(f"Cạnh {edge.id!r} đi ra từ node {edge.source_node_id!r} không có thật", edge_id=edge.id)
        elif edge.source_port not in outputs_of(source.type):
            yield GraphProblem(
                f"Cạnh {edge.id!r} đi ra từ cổng {edge.source_port!r}, "
                f"nhưng node {source.id!r} loại {source.type.value} không có cổng đó",
                node_id=source.id,
                edge_id=edge.id,
            )
        if workflow.node(edge.target_node_id) is None:
            yield GraphProblem(f"Cạnh {edge.id!r} trỏ tới node {edge.target_node_id!r} không có thật", edge_id=edge.id)


def _port_fan_out(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """One edge per output port, counted straight off `(source_node_id, source_port)`.

    A count rather than a traversal, which is what ADR-0022 §2 bought by keeping
    edges in their own list instead of inside the node they leave.
    """
    counts: defaultdict[tuple[str, str], int] = defaultdict(int)
    for edge in workflow.edges:
        counts[(edge.source_node_id, edge.source_port)] += 1
    for (node_id, port), count in counts.items():
        if count > 1:
            yield GraphProblem(
                f"Cổng {port!r} của node {node_id!r} nối tới {count} cạnh, mỗi cổng chỉ được nối một cạnh",
                node_id=node_id,
            )


def _cycles(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """The first cycle that does not enter through a `While` node's `loop` port.

    Found by deleting exactly those back-edges and looking for a cycle in what
    is left. That is the rule stated as an algorithm rather than approximated by
    one: a cycle surviving the deletion is a cycle with no `max_iterations`
    behind it, which is the unbounded loop ADR-0021 §5 refuses.

    One cycle, not all of them, unlike every other check here. Cycles overlap —
    a single stray edge can close a dozen of them — so listing every one would
    bury the graph's other problems under a dozen sentences about one edge.
    """
    edges = [edge for edge in workflow.edges if not _is_loop_back_edge(workflow, edge)]
    onward = _adjacency(workflow, edges)
    walked: set[str] = set()
    on_path: set[str] = set()

    def walk(node_id: str) -> str | None:
        walked.add(node_id)
        on_path.add(node_id)
        for target in onward[node_id]:
            if target in on_path:
                return target
            if target not in walked:
                found = walk(target)
                if found is not None:
                    return found
        on_path.discard(node_id)
        return None

    for node in workflow.nodes:
        if node.id in walked:
            continue
        closing = walk(node.id)
        if closing is not None:
            yield GraphProblem(
                f"Graph có chu trình quay lại node {closing!r} mà không đi qua cổng 'loop' của một node While — "
                f"chỉ While mới được lặp, vì chỉ nó có trần số vòng",
                node_id=closing,
            )
            return


def _termination(workflow: WorkflowJSON) -> Iterator[GraphProblem]:
    """Every path leaving `start_node_id` reaches an `End`.

    Two halves of one promise. A port with no edge on it is a path that stops
    where nothing was decided, so it is reported at the port. A node that is
    wired but from which no `End` is reachable is a path that runs forever, and
    it is found by asking which nodes reach an `End` at all — walking backwards
    from every `End` once, rather than walking forwards down paths that may
    loop. Revisiting is what a loop *is* here; proving reachability from the far
    end sidesteps it without a special case.
    """
    reaches_end = _nodes_that_reach_end(workflow)
    for node in _reachable_from_start(workflow):
        wired = {edge.source_port for edge in workflow.edges if edge.source_node_id == node.id}
        for port in outputs_of(node.type):
            if port not in wired:
                yield GraphProblem(
                    f"Cổng {port!r} của node {node.id!r} chưa nối đi đâu, nên có nhánh không tới được End",
                    node_id=node.id,
                )
        if node.id not in reaches_end:
            yield GraphProblem(f"Từ node {node.id!r} không có đường nào tới được End", node_id=node.id)


def _nodes_that_reach_end(workflow: WorkflowJSON) -> set[str]:
    """The nodes from which some `End` node is reachable, walking edges backwards."""
    backward: defaultdict[str, list[str]] = defaultdict(list)
    for edge in _valid_edges(workflow):
        backward[edge.target_node_id].append(edge.source_node_id)
    found = {node.id for node in workflow.nodes_of_type(NodeType.END)}
    pending = list(found)
    while pending:
        for source in backward[pending.pop()]:
            if source not in found:
                found.add(source)
                pending.append(source)
    return found


def _reachable_from_start(workflow: WorkflowJSON) -> Iterator[NodeInstance]:
    """Every node control can arrive at from `start_node_id`, `End` nodes excepted.

    `End` is left out because it is the thing being reached: it has no ports to
    wire and nothing further to demand of it.
    """
    onward = _adjacency(workflow, list(_valid_edges(workflow)))
    seen = {workflow.start_node_id}
    pending = [workflow.start_node_id]
    while pending:
        node = workflow.node(pending.pop())
        if node is None:
            continue
        if node.type is not NodeType.END:
            yield node
        for target in onward[node.id]:
            if target not in seen:
                seen.add(target)
                pending.append(target)


def _valid_edges(workflow: WorkflowJSON) -> Iterator[Edge]:
    """The edges whose two ends are both real nodes.

    A dangling edge is already reported by `_edge_endpoints`; letting it into a
    traversal would only produce a second, less useful sentence about the same
    mistake.
    """
    return (
        edge
        for edge in workflow.edges
        if workflow.node(edge.source_node_id) is not None and workflow.node(edge.target_node_id) is not None
    )


def _adjacency(workflow: WorkflowJSON, edges: list[Edge]) -> defaultdict[str, list[str]]:
    """Who each node can send control to, over `edges`."""
    onward: defaultdict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if workflow.node(edge.source_node_id) is not None and workflow.node(edge.target_node_id) is not None:
            onward[edge.source_node_id].append(edge.target_node_id)
    return onward


def _is_loop_back_edge(workflow: WorkflowJSON, edge: Edge) -> bool:
    """Whether `edge` is the one kind of edge allowed to close a cycle."""
    source = workflow.node(edge.source_node_id)
    return source is not None and source.type is NodeType.WHILE and edge.source_port == "loop"


def _repeated(values: Iterable[str]) -> list[str]:
    """The values appearing more than once, each named once."""
    counts: defaultdict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return [value for value, count in counts.items() if count > 1]
