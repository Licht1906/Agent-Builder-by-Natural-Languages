"""One version, assembled into everything a person needs to decide about it.

See issue #14 / ADR-0012. The ticket's argument is that a review spread across
five screens is a review nobody does properly — so the dossier is one object and
it is built in one call, rather than a screen making six requests and hoping it
remembered them all.

Nothing here computes a verdict. The eval score comes from `src.evals.gate`, the
cost figures from `src.agents.costs`, the risk level of a tool from
`src.agents.tools.registry` (V1) or `src.workflow.tools` (V2), and the flow from
`src.agents.flow`. This module's whole job is gathering — the moment it starts
deciding anything, it becomes a second opinion on a question that already has an
answer somewhere else.

The one judgement it does make is **order**: high-risk tools first. "Tô đậm tool
rủi ro cao" is a claim about where attention goes, and attention goes top-down.

**#49 widened it to V2's documents.** A V2 version holds a solution spec and a
graph where a V1 one holds a pipeline (ADR-0020 §5, ADR-0022 §5), and asks for
*actions on connections* rather than tool names. Both shapes are gathered by the
one function, into one object with both halves on it, for the reason the object
exists at all: an admin decides about a version, and a second `build_v2_dossier`
beside this one would be two places to remember the next time the review gains a
field — the failure mode #14 exists to prevent, one level up.
"""

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.costs import CostFigure, estimated_cost, worst_case_cost
from src.agents.flow import FlowDiagram, flow_of
from src.agents.spend import latest_trial_cost
from src.agents.tools import TOOL_REGISTRY
from src.agents.tools.registry import ToolRisk, ToolSpec
from src.approvals.decisions import history_for
from src.db.models import Agent, AgentVersion, ApprovalDecision, EvalRun, SandboxRun, User
from src.evals.gate import score_for, version_blocker
from src.evals.results import Breakdown, Score, breakdown_of, latest_run
from src.evals.suite_changes import SuiteChanges, suite_changes
from src.models.agent_spec import AgentSpec
from src.models.node_registry import McpConfig, NodeType
from src.models.solution_spec import AgentSolutionSpecification
from src.models.workflow import WorkflowBudget, WorkflowJSON
from src.workflow.tools import RiskLevel, resolved_action


@dataclass(frozen=True, slots=True)
class RequestedTool:
    """One permission this version is asking for, and how worried to be about it.

    `risk`, `description` and `has_side_effects` are read off the registry, never
    off the spec. That is rule 1 of `src.agents.tools.registry`: risk is assigned
    by whoever wrote the tool, and nothing an LLM put in an `AgentSpec` can talk
    a tool into a milder one.

    `requested_by_step_ids` is here because "which step wanted to send email" is
    always the next question, and a flat list of tool names cannot answer it.
    """

    name: str
    description: str
    risk: ToolRisk
    has_side_effects: bool
    requested_by_step_ids: tuple[str, ...]

    @property
    def is_high_risk(self) -> bool:
        return self.risk is ToolRisk.HIGH


@dataclass(frozen=True, slots=True)
class RequestedAction:
    """One action a V2 workflow asks permission to call — see #49, ADR-0023 §2.

    V2's `RequestedTool`, and shaped by the two things ADR-0023 changed. A
    permission is a *pair* — the connection and the action on it — because a line
    saying only `google_workspace` would not say whether the run reads an inbox
    or sends an email, which is the whole difference the risk model turns on. And
    `scopes` is on it because a connection reaches outside the organisation
    through an OAuth grant rather than through a domain allowlist: it is the same
    question V1's `allowed_domains` answered, asked of the thing V2 actually
    connects to.

    `risk` and `requires_human_approval` are `None` together, and only for an
    action the catalogue has never heard of. That is a real state a *confirmed*
    graph can be in — `src.workflow.validation` warns about an unresolvable
    `MCP` node and deliberately does not refuse it — and it must not be flattened
    into `low`/`False`. "Nobody can vouch for this" and "this is safe" are
    opposite things to put in front of a reviewer.
    """

    tool_id: str
    action: str
    tool_name: str
    description: str
    risk: RiskLevel | None
    requires_human_approval: bool | None
    scopes: tuple[str, ...]
    requested_by_node_ids: tuple[str, ...]

    @property
    def name(self) -> str:
        """What the transcript calls it: `tool_id.action`.

        The runtime's spelling (`src.workflow.runtime`), not a second one — an
        admin matching a permission against a `tool` line in the transcript below
        it is doing so by eye, and two spellings would make that matching wrong
        exactly when it mattered.
        """
        return f"{self.tool_id}.{self.action}"

    @property
    def is_high_risk(self) -> bool:
        return self.risk is RiskLevel.HIGH

    @property
    def is_unresolved(self) -> bool:
        """Whether the catalogue could say anything at all about this action."""
        return self.risk is None


@dataclass(frozen=True, slots=True)
class Dossier:
    """Everything about one version that bears on whether it should be allowed to run.

    Deliberately wide. Each field is one of the things #14 lists, and a narrower
    object would only mean the screen fetching the rest itself — which is the
    scattering this ticket exists to end.

    Three of the fields are `None` for a real and different reason each:
    `sandbox_run` when nothing has been trialled, `eval_run`/`eval_score` when
    nothing has been graded, and `measured` when nothing has been spent. None of
    the three is ever a zero — "we ran it and it was free" is the one thing a
    measurement must not say when nothing was measured (ADR-0009).

    **The document fields come in two sets and a version has one of them** (#49).
    `spec`/`flow`/`tools`/`allowed_domains`/`estimate`/`worst_case` describe a V1
    pipeline; `solution_spec`/`workflow`/`actions`/`budget` describe V2's pair of
    documents. Which set is filled is read off the version rather than declared,
    exactly as `AgentVersion` itself holds whichever documents its stage has
    reached — and the absent set is `None`, never an empty stand-in, because a
    blank graph on the screen would read as a workflow nobody drew.
    """

    version: AgentVersion
    agent: Agent
    owner: User
    spec: AgentSpec | None
    flow: FlowDiagram | None
    tools: tuple[RequestedTool, ...]
    allowed_domains: tuple[str, ...]
    #: V2's business document — what the agent is *for*, in the creator's words.
    solution_spec: AgentSolutionSpecification | None
    #: V2's executable document: the confirmed graph, whole. Handed on rather
    #: than summarised, so the admin reads the same document the canvas drew and
    #: the runtime will walk (ADR-0022 §5).
    workflow: WorkflowJSON | None
    actions: tuple[RequestedAction, ...]
    #: What one run of the graph may spend. V2's answer to V1's `worst_case`, and
    #: a ceiling rather than a projection: nothing in this platform prices a graph
    #: yet, and a made-up estimate is worse than an honest bound (ADR-0009 §2).
    budget: WorkflowBudget | None
    sandbox_run: SandboxRun | None
    eval_run: EvalRun | None
    eval_score: Score | None
    #: Who graded what, on the run `eval_run` is. Beside the score rather than
    #: instead of it: `4/5` is one sentence over two kinds of verdict, and only
    #: one of them is the kind an `eval_override` may waive (ADR-0028 §6).
    eval_breakdown: Breakdown | None
    #: Why this **version** may not ask for approval, in the gate's own words, or
    #: `None`. About the version and not about `eval_run`: the gate reads the
    #: latest run only, so a dossier opened on a superseded run would otherwise
    #: show a green light over a version that is refused (ADR-0011 §5).
    blocked_reason: str | None
    #: `None` for a V2 version, whose ceiling is `budget`. Pricing a graph would
    #: mean walking branches and loops nobody has run yet, and #11's whole
    #: argument is that a figure is only worth showing beside the kind of claim it
    #: is making — a projection nobody can stand behind is not one.
    estimate: CostFigure | None
    worst_case: CostFigure | None
    measured: CostFigure | None
    decisions: tuple[ApprovalDecision, ...]
    #: EV-05 / ADR-0069 §6: what happened to the suite since the baseline — the
    #: system's retirements (and how many were failing), the creator's own
    #: edits and deletions, and the cases with no baseline yet.
    suite_changes: SuiteChanges | None = None


def requested_tools(spec: AgentSpec) -> tuple[RequestedTool, ...]:
    """Every tool `spec` asks for, high risk first, then alphabetically.

    One entry per tool rather than one per step: an admin is deciding whether
    this agent may send email at all, and seeing `send_email` three times is
    three readings of one permission.

    The sort is stable and total, so two people reading the same dossier read it
    in the same order — a list that reordered itself between loads would make
    "the second one" mean nothing.
    """
    steps_by_tool: dict[str, list[str]] = {}
    for step in spec.steps:
        for name in step.tools:
            steps_by_tool.setdefault(name, []).append(step.id)

    tools = [_permission(TOOL_REGISTRY.get(name), step_ids) for name, step_ids in steps_by_tool.items()]
    return tuple(sorted(tools, key=lambda tool: (not tool.is_high_risk, tool.name)))


def _permission(tool: ToolSpec, step_ids: list[str]) -> RequestedTool:
    """One catalogue entry, as the permission a version is asking for."""
    return RequestedTool(
        name=tool.name,
        description=tool.description,
        risk=tool.risk,
        has_side_effects=tool.has_side_effects,
        requested_by_step_ids=tuple(step_ids),
    )


def requested_actions(workflow: WorkflowJSON) -> tuple[RequestedAction, ...]:
    """Every action `workflow` asks to call, the ones worth reading first at the top.

    Read off the `MCP` nodes rather than off a declared list, because there is no
    declared list to read: ADR-0022 §3 left `allowed_tools` out of
    `WorkflowMetadata` precisely so that what a graph may call is what a graph
    *contains*. A permission list assembled from anything else would be a second
    account of the graph, free to disagree with it.

    Ordered high risk first, then whatever the catalogue could not explain, then
    the rest by name. The middle rank is #49's one addition to V1's rule and it
    follows from the same argument: an unresolvable action is not a mild one, it
    is one nobody has assessed, and a reviewer should meet it before they meet
    the reads.

    One entry per action, with every node that wanted it named — V1's argument
    unchanged. An admin is deciding whether this agent may send email at all.
    """
    nodes_by_action: dict[tuple[str, str], list[str]] = {}
    for node in workflow.nodes_of_type(NodeType.MCP):
        try:
            config = McpConfig.model_validate(node.config)
        except ValidationError:
            # A malformed `config` is a draft the canvas already reports on
            # (`src.workflow.validation`), and there is no action here to ask the
            # catalogue about. The node is still on the graph the admin reads.
            continue
        if not config.tool_id or not config.action:
            # A node nobody has finished configuring, which `McpConfig` accepts
            # by design — every field defaults to blank so a mid-edit draft
            # saves (`src.models.node_registry`). It is not a permission, and
            # listing it would print an action called `.` and flag it as one the
            # registry could not resolve: an unconfigured node reported as an
            # unvouched-for tool. Confirmation refuses the blanks
            # (`unfilled_fields`), so this can only be a version that never got
            # that far — and the empty node is visible on the graph above.
            continue
        nodes_by_action.setdefault((config.tool_id, config.action), []).append(node.id)

    actions = [_action_permission(pair, node_ids) for pair, node_ids in nodes_by_action.items()]
    return tuple(sorted(actions, key=lambda item: (not item.is_high_risk, not item.is_unresolved, item.name)))


def _action_permission(pair: tuple[str, str], node_ids: list[str]) -> RequestedAction:
    """One `{tool_id, action}` as the permission a version is asking for.

    Everything but the pair itself comes from the catalogue, and stays empty when
    the catalogue has nothing to say. Never from the node: a workflow describing
    its own permission is the shape ADR-0023 keeps out of the risk model, where
    what an action costs to be wrong is derived from its effect and not from the
    words around it (§3).
    """
    tool_id, action_name = pair
    resolved = resolved_action(tool_id, action_name)
    if resolved is None:
        return RequestedAction(
            tool_id=tool_id,
            action=action_name,
            tool_name="",
            description="",
            risk=None,
            requires_human_approval=None,
            scopes=(),
            requested_by_node_ids=tuple(node_ids),
        )
    connection, catalogued = resolved
    return RequestedAction(
        tool_id=tool_id,
        action=action_name,
        tool_name=connection.name,
        description=catalogued.description,
        risk=catalogued.risk_level,
        requires_human_approval=catalogued.requires_human_approval,
        scopes=tuple(connection.credential.scopes),
        requested_by_node_ids=tuple(node_ids),
    )


def latest_sandbox_run(db: Session, version_id: int) -> SandboxRun | None:
    """The most recent trial run of `version_id`, whether it succeeded or not.

    A failed run is evidence, not the absence of it — the transcript of the run
    that went wrong is precisely what a reviewer is looking for. Filtering to
    successes would show them the last run that *worked* and hide the one that
    did not.
    """
    return db.execute(
        select(SandboxRun).where(SandboxRun.version_id == version_id).order_by(SandboxRun.id.desc()).limit(1)
    ).scalar_one_or_none()


def build_dossier(db: Session, version: AgentVersion) -> Dossier:
    """Gather everything about `version` in one pass, whichever documents it holds.

    Every stored document is re-validated on the way through, for the same reason
    `src.api.agent_routes.validated_spec` does it before a run: a stored payload
    that no longer parses must not be rendered as though it were a spec. Here it
    would be worse than at run time — an admin would be approving a description
    of an agent rather than the agent.

    A column that is empty is a different thing from one that will not parse, and
    only the second is a failure. A V2 version has no `spec` and never will
    (ADR-0020 §5); asking it for one and refusing when it has none would put the
    whole V2 pipeline out of reach of the only screen that can approve it.
    """
    spec = AgentSpec.model_validate(version.spec) if version.spec is not None else None
    workflow = WorkflowJSON.model_validate(version.workflow) if version.workflow is not None else None
    solution_spec = (
        AgentSolutionSpecification.model_validate(version.solution_spec)
        if version.solution_spec is not None
        else None
    )
    agent = db.get(Agent, version.agent_id)
    if agent is None:  # pragma: no cover — a version cannot outlive its agent
        raise LookupError(f"version {version.id} không còn agent nào sở hữu")
    owner = db.get(User, agent.owner_id)
    if owner is None:  # pragma: no cover — an agent cannot outlive its owner
        raise LookupError(f"agent {agent.id} không còn chủ sở hữu")

    eval_run = latest_run(db, version.id)
    eval_score = score_for(db, eval_run) if eval_run is not None else None
    return Dossier(
        version=version,
        agent=agent,
        owner=owner,
        spec=spec,
        flow=flow_of(spec) if spec is not None else None,
        tools=requested_tools(spec) if spec is not None else (),
        allowed_domains=tuple(spec.allowed_domains) if spec is not None else (),
        solution_spec=solution_spec,
        workflow=workflow,
        actions=requested_actions(workflow) if workflow is not None else (),
        budget=workflow.metadata.budget if workflow is not None else None,
        sandbox_run=latest_sandbox_run(db, version.id),
        eval_run=eval_run,
        eval_score=eval_score,
        eval_breakdown=breakdown_of(eval_run) if eval_run is not None else None,
        # `version_blocker`, not the score alone: since #48 a version can be
        # blocked by a rule read off its own eval transcript, which no override
        # waives (ADR-0028 §6). A dossier reporting "nothing blocking" beside a
        # version `request_approval` refuses is the divergence `src.evals.gate`
        # exists to prevent.
        blocked_reason=version_blocker(db, version.id),
        suite_changes=suite_changes(db, version) if workflow is not None else None,
        estimate=estimated_cost(spec) if spec is not None else None,
        worst_case=worst_case_cost(spec) if spec is not None else None,
        measured=latest_trial_cost(db, version.id),
        decisions=tuple(history_for(db, version.id)),
    )
