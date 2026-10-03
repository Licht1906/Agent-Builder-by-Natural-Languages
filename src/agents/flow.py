"""A spec drawn as the graph it executes as — the *sơ đồ luồng* on the approval screen.

See #14 / ADR-0012. The approval dossier has to answer "what does this agent
actually do" before it asks an admin to say yes, and a list of steps is not that
answer: the order steps run in, and where the run ends, are the substance of what
is being reviewed.

Pure, and over `AgentSpec` rather than over an `AgentVersion` row, for the reason
`compare_versions` is: the shape is a property of the spec, and a diagram that
needed a database could not be drawn for a spec that has not been saved yet.

Successors come from `AgentSpec.next_step_id` and never from `Step.next`
directly. `next=None` means "the step after this one in the list", which is a
rule `AgentSpec` owns; re-deriving it here is how a diagram starts disagreeing
with the runtime about what an agent does — the one thing a diagram on an
approval screen cannot afford.
"""

from dataclasses import dataclass

from src.models.agent_spec import AgentSpec


@dataclass(frozen=True, slots=True)
class FlowNode:
    """One step, with the two things an admin judges it by.

    `model` and `tools` travel on the node rather than in a separate list because
    the question being asked is "which step wanted to send email", and a list of
    tools detached from the steps that asked for them cannot answer it.
    """

    step_id: str
    title: str
    model: str
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FlowEdge:
    """One join between two steps: this one runs, then that one."""

    from_step_id: str
    to_step_id: str


@dataclass(frozen=True, slots=True)
class FlowDiagram:
    """The whole graph. `nodes` in declaration order; `edges` in the order they leave those nodes.

    A step with no outgoing edge is where a run ends. Nothing here marks a
    "start" node: the run begins at the first step, which is `nodes[0]`, and a
    flag saying so would be a second statement of the same fact.
    """

    nodes: tuple[FlowNode, ...]
    edges: tuple[FlowEdge, ...]


def flow_of(spec: AgentSpec) -> FlowDiagram:
    """`spec` as nodes and edges.

    A back edge — a step whose successor is earlier in the list — is drawn like
    any other. `AgentSpec` permits one and `src.agents.costs` already accounts
    for it by refusing to quote a structural cost bound, so hiding it here would
    leave an admin reading a worst case they cannot account for.
    """
    return FlowDiagram(
        nodes=tuple(
            FlowNode(step_id=step.id, title=step.title, model=step.model, tools=tuple(step.tools))
            for step in spec.steps
        ),
        edges=tuple(
            FlowEdge(from_step_id=step.id, to_step_id=successor)
            for step in spec.steps
            if (successor := spec.next_step_id(step.id)) is not None
        ),
    )
