"""Agent endpoints: a creator's workspace, plus the two doors into a running agent.

Almost everything here is scoped to the owner. That was the whole of ticket #4 —
list, read, rename — and the version lifecycle (#7), the sandbox (#9) and the
cost figures (#11) all arrived on top of `OwnedAgent` rather than re-deriving it.

#15 opens exactly three of these endpoints to the rest of the platform, and no
more. An agent published as a Deployment is a product other people are meant to
use: they may see that it is running (`GET /agents/running`), read its name, and
talk to it (`chat`, `invoke`). Its drafts, its version history, its trial
transcripts, its bill and its threads stay behind `OwnedAgent` — being able to
use something is not being able to look inside it.

The gate on those three is `AddressableAgent` (`src.api.deployment_access`), and
since #50 it is *published to you* rather than merely *published*: an agent
nobody deployed is 404 to a stranger exactly as it was before, and so is one
whose Deployment is paused or does not name them (ADR-0029 §2–§3). `invoke`
additionally answers to the Deployment's API key, which is the same door with a
second lock rather than a second way in.

Admins get no read-through here either. Reviewing another creator's work happens
on the approval endpoints (#14), where the reviewer's access is deliberate and
audited, rather than as a silent side effect of being an admin.
"""

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.conversations import find as find_conversation
from src.agents.conversations import list_for as list_conversations
from src.agents.conversations import record_turn
from src.agents.costs import CostFigure, estimated_cost, worst_case_cost
from src.agents.flow import FlowDiagram, flow_of
from src.agents.memory import get_checkpointer
from src.agents.portability import BUNDLE_FORMAT, BundleError, export_bundle, import_bundle
from src.agents.removal import AgentInUseError, delete_agent
from src.agents.runtime import (
    BUDGET_EXCEEDED_EVENT,
    COST_EVENT,
    Conversation,
    RunServices,
    get_clock,
    stream_agent_reply,
)
from src.agents.sandbox import stream_sandbox_run
from src.agents.sandbox import transcript_entry as pipeline_transcript_entry
from src.agents.spend import agent_spend, latest_trial_cost
from src.agents.threads import CONVERSATION_ID_PATTERN, RunIdentity, ThreadNamespace, ThreadRef
from src.agents.versions import (
    EdgeRef,
    SpecComparison,
    VersionLifecycleError,
    WorkflowComparison,
    compare_versions,
    list_versions,
    version_number,
)
from src.api.deployment_access import AddressableAgent, Caller, MeteredAgent, MeteredByAnyChannel
from src.approvals.dossier import RequestedTool, requested_tools
from src.auth.dependencies import CurrentUser
from src.db.base import get_db
from src.db.models import Agent, AgentVersion, Deployment, RunApproval, RunApprovalStatus, RunOutcome, SandboxRun, User
from src.deployments.publication import visible_to
from src.interview.generation import NoAssignableModelError, SpecGenerationError
from src.interview.refinement import BlankInstructionError, draft_from_refinement
from src.models.agent_spec import AgentSpec
from src.models.schemas import (
    AgentBundleRequest,
    AgentCostsResponse,
    AgentInvocationRequest,
    AgentInvocationResponse,
    AgentRenameRequest,
    AgentResponse,
    AgentVersionResponse,
    AgentVersionSpecResponse,
    ApprovalActionResponse,
    ChatMessageRequest,
    ConversationRenameRequest,
    ConversationResponse,
    CostFigureResponse,
    FlowDiagramResponse,
    FlowEdgeResponse,
    FlowNodeResponse,
    ImportedAgentResponse,
    InvokedStepResponse,
    InvokedToolResponse,
    PendingApprovalResponse,
    RefinedSpecResponse,
    RefineVersionRequest,
    RequestedToolResponse,
    RunningAgentResponse,
    SandboxRunRequest,
    SandboxRunResponse,
    SandboxTranscriptEntry,
    SpecComparisonResponse,
    ThreadHistoryResponse,
    ThreadStartedEvent,
    TranscriptTurnResponse,
    VersionComparisonResponse,
    VersionCostsResponse,
    VersionFlowResponse,
    WorkflowComparisonResponse,
    WorkflowEdgeRefResponse,
    WorkflowNodeDiffResponse,
)
from src.models.workflow import WorkflowMetadata
from src.pm.ledger import record_v2_run
from src.services.llm import ChatModelFactory, get_chat_model_factory
from src.services.tracing import Tracer, get_tracer, open_trace
from src.workflow.sandbox import transcript_entry as workflow_transcript_entry

#: The checkpointer, as an endpoint annotation.
Checkpointer = Annotated[BaseCheckpointSaver, Depends(get_checkpointer)]


@dataclass(frozen=True, slots=True)
class ThreadAddress:
    """What a request says about which conversation it means, and on whose behalf.

    The chat body and the history endpoint's path+query say the same two things
    in different shapes; this is where they become one thing.

    `caller` is #50's third part, and it is `None` for everything that happens in
    the `trial` namespace: a rehearsal belongs to the agent's creator by
    construction (#6), so there is nobody to tell apart. On a production thread
    there is — one Deployment serves many end-users — and the caller is baked
    into the thread's address so two of them cannot share a conversation even by
    sharing an id.
    """

    conversation_id: str | None
    namespace: ThreadNamespace
    caller: str | None = None


@dataclass(frozen=True, slots=True)
class Run:
    """One run this request is about to start, assembled from its three halves.

    `identity` is what the run is filed under afterwards (its costs, its trace),
    `services` is what it talks to while it happens, `conversation` is what it
    remembers. Every streaming endpoint needs all three and nothing needs a
    subset, so they travel as one rather than as three arguments threaded
    through two layers each.
    """

    identity: RunIdentity
    services: RunServices
    conversation: Conversation


@dataclass(frozen=True, slots=True)
class RunFactory:
    """The per-request seams, injected once and ready to be aimed at a conversation.

    A single dependency rather than one per seam on every streaming endpoint:
    they are always wanted together, and an endpoint that took the model factory
    and forgot the tracer would be an untraced run nobody noticed (#11's "mọi lần
    chạy đều xuất hiện trong trace").
    """

    model_factory: ChatModelFactory
    tracer: Tracer
    clock: Callable[[], float]

    def __call__(self, version_id: int, conversation: Conversation) -> Run:
        """Mint an identity for this run and open its trace under it.

        `open_trace` and not `self.tracer(...)`: starting a trace talks to the
        backend, so an unguarded call here would turn a dead observability host
        into a failed chat request.
        """
        return self.resumed(RunIdentity.for_run(conversation.thread, version_id), conversation)

    def resumed(self, identity: RunIdentity, conversation: Conversation) -> Run:
        """The same run carrying on, with the identity it already had.

        What a V2 run resumed after a `HumanApproval` pause needs (#47): the
        second leg is not a new run, so minting a fresh `RunIdentity` would file
        its costs under a different `trace_id` than the first leg's and leave the
        two halves of one execution unjoinable in the trace backend.
        """
        return Run(
            identity=identity,
            services=RunServices(
                model_factory=self.model_factory, trace=open_trace(self.tracer, identity), clock=self.clock
            ),
            conversation=conversation,
        )


def get_run_factory(
    model_factory: Annotated[ChatModelFactory, Depends(get_chat_model_factory)],
    tracer: Annotated[Tracer, Depends(get_tracer)],
    clock: Annotated[Callable[[], float], Depends(get_clock)],
) -> RunFactory:
    """FastAPI dependency: everything a run needs from outside itself."""
    return RunFactory(model_factory=model_factory, tracer=tracer, clock=clock)


#: Declare this on an endpoint that starts a run and both seams come with it.
StartRun = Annotated[RunFactory, Depends(get_run_factory)]


router = APIRouter(prefix="/agents", tags=["agents"])


def get_owned_agent(agent_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> Agent:
    """The agent at `agent_id` if the caller owns it, otherwise 404.

    404 rather than 403 on purpose: telling a caller "this exists but is not
    yours" leaks that some other creator owns an agent with that id.
    """
    agent = db.get(Agent, agent_id)
    if agent is None or agent.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy agent")
    return agent


#: Declare this type on any endpoint that acts on one agent and the ownership
#: check comes with it — there is nothing to remember and nothing to forget.
OwnedAgent = Annotated[Agent, Depends(get_owned_agent)]


@router.get("", response_model=list[AgentResponse])
def list_my_agents(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> Sequence[Agent]:
    """The caller's own agents — never anyone else's.

    Still owner-scoped after #15. This endpoint answers "what am I building",
    which is a different question from "what can I use" — `GET /agents/running`
    answers that one, and merging them would leave a creator's workspace filling
    up with other people's finished agents.
    """
    return db.execute(select(Agent).where(Agent.owner_id == user.id).order_by(Agent.id)).scalars().all()


@router.get("/running", response_model=list[RunningAgentResponse])
def list_running_agents(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> list[RunningAgentResponse]:
    """Every running agent **this caller may actually use** — see #15, narrowed by #50.

    Declared before `GET /{agent_id}`, and it has to be: routes match in
    declaration order, and the other one would swallow `running` as an id and
    answer 422.

    Until #50 this was "every agent with a version deployed", full stop.
    That is now the wrong list twice over: a paused Deployment is not running,
    and a `private` or `selected_users` one is not running *for the person
    asking*. The condition comes from `visible_to`, the same one
    `get_addressable_agent` gates the chat door with (ADR-0036 §3), so a row on this screen and
    a 404 from that door cannot disagree — an agent listed here is one the reader
    can open.

    The owner's email is joined in rather than looked up per row: this list grows
    with the platform, and a lookup inside the loop would be one query per running
    agent on a screen everybody opens. It is unpaginated for the same reason it is
    cheap — one row per *reachable* Deployment, which is a small fraction of the
    agents that exist. The day that stops being true this is the endpoint to page,
    not the screen to trim.

    `agent_version_id` is selected off the Deployment rather than read back off
    `agent`, so the value the response carries is the one the `WHERE` clause
    filtered on — and the property that would answer it does a second query per
    row.

    `disclaimer` (RAG-14 / #100) rides along for the same reason: `ChatPage` is
    a stranger to the agent's own workspace and has no `OwnedAgent` door to read
    the workflow through, so this is the one place it can learn what the
    creator configured. A version whose `workflow` does not parse — or has
    none, the pre-#96 shape this table can still hold — reads as no disclaimer
    rather than a 500 a chat window has no way to recover from.
    """
    rows = db.execute(
        select(Agent.id, Agent.name, Agent.owner_id, User.email, Deployment.agent_version_id, AgentVersion.workflow)
        .join(User, Agent.owner_id == User.id)
        .join(Deployment, Deployment.agent_id == Agent.id)
        .join(AgentVersion, AgentVersion.id == Deployment.agent_version_id)
        .where(visible_to(user))
        .order_by(Agent.id)
    ).all()
    return [
        RunningAgentResponse(
            id=agent_id,
            name=name,
            owner_id=owner_id,
            owner_email=owner_email,
            deployed_version_id=version_id,
            runs_workflow=workflow is not None,
            disclaimer=_disclaimer_of(workflow),
        )
        for agent_id, name, owner_id, owner_email, version_id, workflow in rows
    ]


@router.post("/import", response_model=ImportedAgentResponse, status_code=status.HTTP_201_CREATED)
def import_agent_bundle(
    user: CurrentUser,
    payload: AgentBundleRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ImportedAgentResponse:
    """Create an agent from an exported bundle — the other end of `.../export`.

    Declared before `GET /{agent_id}` for `list_running_agents`' reason: routes
    match in declaration order, and a later `/{agent_id}` would take `import` for
    an id. A POST and a GET would not actually collide here, but the next person
    adding `GET /import` should not have to discover that.

    **The only way into this platform that skips the interview**, and the only
    one that should be. The interview exists to *produce* a specification out of
    a conversation; a bundle already carries one, and re-interviewing somebody
    about a document they are holding would be theatre. What it does not skip is
    the model allowlist — `src.agents.portability` re-applies it and reports
    every node it had to move, so an import cannot smuggle in a model an admin
    withdrew (ADR-0010 §4).

    The new version lands at `workflow_pending_confirmation`: an imported graph
    is one this deployment has never validated, naming documents and connections
    that may only exist where it came from. Landing it editable is what puts
    those on the canvas as problems to fix instead of inside a confirmed version
    nobody checked.

    A file this build does not understand is a 422 rather than a partially
    applied import — see `BUNDLE_FORMAT`.
    """
    if payload.format != BUNDLE_FORMAT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"File có format {payload.format!r}, bản dựng này chỉ đọc được {BUNDLE_FORMAT!r} — "
                "xuất lại từ deployment nguồn thay vì sửa tay"
            ),
        )
    imported = import_bundle(
        db,
        owner_id=user.id,
        name=payload.name,
        spec=payload.solution_spec,
        workflow=payload.workflow,
    )
    return ImportedAgentResponse(
        agent=AgentResponse.model_validate(imported.agent),
        version_id=imported.version.id,
        remapped_models=imported.remapped_models,
    )


def _disclaimer_of(workflow: dict[str, Any] | None) -> str:
    """`workflow["metadata"]["disclaimer"]`, or `""` for anything that is not honestly that."""
    if not workflow:
        return ""
    try:
        return WorkflowMetadata.model_validate(workflow.get("metadata") or {}).disclaimer
    except ValidationError:
        return ""


@router.get("/{agent_id}", response_model=AgentResponse)
def read_agent(agent: AddressableAgent) -> Agent:
    """One agent: the caller's own, or one that is running and therefore theirs to use.

    Open to a stranger because a chat window needs a name at the top of it, and
    a screen that could talk to an agent but not say which agent it was talking
    to would be a worse answer than the 404 it replaced.
    """
    return agent


@router.patch("/{agent_id}", response_model=AgentResponse)
def rename_my_agent(
    agent: OwnedAgent,
    payload: AgentRenameRequest,
    db: Annotated[Session, Depends(get_db)],
) -> Agent:
    """Rename one of the caller's own agents."""
    agent.name = payload.name
    db.commit()
    db.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_agent(agent: OwnedAgent, db: Annotated[Session, Depends(get_db)]) -> None:
    """Delete one of the caller's own agents, with everything that is only about it.

    `OwnedAgent`, so somebody else's agent is a 404 and not a refusal — the same
    rule the rest of this module follows, and the one that matters most here:
    "this exists but is not yours" on a *delete* would be a way to find out which
    ids are worth attacking.

    409 while the agent is actively serving, carrying the sentence that says what
    to do instead (pause it). Not a 403: the caller is entirely entitled to delete
    their own agent, and what is being refused is the timing.

    204 and no body. There is nothing left to describe, and a payload echoing back
    what was just deleted is a payload nobody can do anything with.
    """
    try:
        delete_agent(db, agent)
    except AgentInUseError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


def get_agent_version_or_404(db: Session, agent: Agent, version_id: int) -> AgentVersion:
    """`version_id` if it belongs to `agent`, otherwise 404 — same "don't leak existence" rule as `get_owned_agent`."""
    version = db.get(AgentVersion, version_id)
    if version is None or version.agent_id != agent.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy version")
    return version


def get_owned_version(version_id: int, agent: OwnedAgent, db: Annotated[Session, Depends(get_db)]) -> AgentVersion:
    """The version at `{agent_id}/versions/{version_id}` if the caller owns the agent.

    `OwnedAgent` does the ownership half, so this only adds "and that version is
    this agent's". Declared as a dependency rather than called by hand because
    eval (#13) added a whole router of version-scoped endpoints, and an
    ownership check that each of them has to remember is one that eventually
    goes missing from the newest.
    """
    return get_agent_version_or_404(db, agent, version_id)


#: Declare this type on any endpoint under `/{agent_id}/versions/{version_id}`
#: and both checks — the agent is yours, the version is that agent's — come with it.
OwnedVersion = Annotated[AgentVersion, Depends(get_owned_version)]


@router.get("/{agent_id}/versions", response_model=list[AgentVersionResponse])
def list_my_agents_versions(agent: OwnedAgent, db: Annotated[Session, Depends(get_db)]) -> list[AgentVersionResponse]:
    """Every version of the caller's own agent, oldest first, flagging which one is deployed.

    `number` is the position in this list rather than a call to `version_number`
    per row: the list is already the ordering that function derives, so counting
    it here is the same answer without a query each time round the loop.
    """
    return [
        AgentVersionResponse(
            id=version.id,
            number=number,
            status=version.status,
            rejection_reason=version.rejection_reason,
            is_deployed=version.id == agent.deployed_version_id,
            created_at=version.created_at,
        )
        for number, version in enumerate(list_versions(db, agent), start=1)
    ]


@router.get("/{agent_id}/versions/compare", response_model=VersionComparisonResponse)
def compare_my_agents_versions(
    agent: OwnedAgent,
    db: Annotated[Session, Depends(get_db)],
    version_a_id: Annotated[int, Query(description="Id của version thứ nhất")],
    version_b_id: Annotated[int, Query(description="Id của version thứ hai")],
) -> VersionComparisonResponse:
    """Diff two versions of the caller's own agent — 404 if either id isn't one of the agent's versions.

    Both ids are resolved through `get_agent_version_or_404` scoped to `agent`
    first, so `compare_versions`'s own "different agent" check can never fire
    here — that check exists for direct callers of the service module.

    Both documents are diffed in one call rather than one endpoint per document,
    because "what changed between these two" is one question a creator asks once
    (#55). Which halves come back is a fact about the versions, not about the
    request: a V1 pair answers with a pipeline diff and a null workflow, a V2
    pair the other way round, and a pair where only one side has a document says
    so in `skipped`.

    The 409 is for the one refusal that *can* fire: two versions with no document
    between them have nothing to compare, and asking is a question with no answer
    rather than a bad request body.
    """
    version_a = get_agent_version_or_404(db, agent, version_a_id)
    version_b = get_agent_version_or_404(db, agent, version_b_id)
    try:
        diff = compare_versions(version_a, version_b)
    except VersionLifecycleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return VersionComparisonResponse(
        version_a_id=version_a.id,
        version_b_id=version_b.id,
        spec=_spec_comparison_response(diff.spec),
        workflow=_workflow_comparison_response(diff.workflow),
        skipped=diff.skipped,
    )


def _spec_comparison_response(diff: SpecComparison | None) -> SpecComparisonResponse | None:
    """V1's half of a comparison on the wire, or `None` when neither version had a pipeline."""
    if diff is None:
        return None
    return SpecComparisonResponse(
        changed_fields=diff.changed_fields,
        added_step_ids=list(diff.added_step_ids),
        removed_step_ids=list(diff.removed_step_ids),
        changed_steps=diff.changed_steps,
    )


def _workflow_comparison_response(diff: WorkflowComparison | None) -> WorkflowComparisonResponse | None:
    """V2's half of a comparison on the wire, or `None` when neither version had a graph."""
    if diff is None:
        return None
    return WorkflowComparisonResponse(
        changed_fields=diff.changed_fields,
        added_node_ids=list(diff.added_node_ids),
        removed_node_ids=list(diff.removed_node_ids),
        changed_nodes={
            node_id: WorkflowNodeDiffResponse(
                changed_fields=node.changed_fields, changed_config_keys=list(node.changed_config_keys)
            )
            for node_id, node in diff.changed_nodes.items()
        },
        added_edges=[_edge_ref_response(edge) for edge in diff.added_edges],
        removed_edges=[_edge_ref_response(edge) for edge in diff.removed_edges],
    )


def _edge_ref_response(edge: EdgeRef) -> WorkflowEdgeRefResponse:
    """One connection of a graph on the wire."""
    return WorkflowEdgeRefResponse(
        source_node_id=edge.source_node_id, source_port=edge.source_port, target_node_id=edge.target_node_id
    )


@router.get("/{agent_id}/versions/{version_id}", response_model=AgentVersionSpecResponse)
def read_my_agents_version(
    agent: OwnedAgent,
    version: OwnedVersion,
) -> AgentVersionSpecResponse:
    """One version and the spec it holds — what the wizard's spec screen reads (#19).

    Declared **after** `/versions/compare`, and it has to be: routes match in
    declaration order, and this one would otherwise swallow `compare` as a
    version id and answer 422.

    The spec goes back through `validated_spec` on the way out rather than being
    handed over as stored JSON. It is the same defense in depth ADR-0003 §7 asks
    of the runtime, for the same reason: a row written before a validator existed
    must not reach a reader either — a screen rendering an invalid spec would
    show a creator an agent that cannot run, and say nothing about it.

    Owner-only, unlike `GET /agents/{agent_id}`. What a stranger may see of a
    running agent is its name and that it is running (#15); the prompts inside it
    are the thing the creator wrote, and a deployed agent is not a published
    source listing.
    """
    return AgentVersionSpecResponse(
        id=version.id,
        number=version_number(version),
        agent_id=agent.id,
        status=version.status,
        rejection_reason=version.rejection_reason,
        is_deployed=version.id == agent.deployed_version_id,
        spec=validated_spec(version, subject="Spec của version"),
        created_at=version.created_at,
    )


def flow_diagram_response(flow: FlowDiagram) -> FlowDiagramResponse:
    """One `FlowDiagram` on the wire.

    Here rather than beside either caller because both the creator's diagram
    (#20) and the admin's dossier (#14) draw the same graph, and two renderings
    of one shape is how the two sides of a review start seeing different agents.
    """
    return FlowDiagramResponse(
        nodes=[
            FlowNodeResponse(step_id=node.step_id, title=node.title, model=node.model, tools=list(node.tools))
            for node in flow.nodes
        ],
        edges=[FlowEdgeResponse(from_step_id=edge.from_step_id, to_step_id=edge.to_step_id) for edge in flow.edges],
    )


def requested_tool_responses(tools: Sequence[RequestedTool]) -> list[RequestedToolResponse]:
    """The permission list on the wire, in the order `requested_tools` put it in.

    The order is not re-established here: high risk first is a judgement
    `src.approvals.dossier` makes once, and a second sort at the edge would be a
    second opinion about where attention should go.
    """
    return [
        RequestedToolResponse(
            name=tool.name,
            description=tool.description,
            risk=tool.risk,
            has_side_effects=tool.has_side_effects,
            requested_by_step_ids=list(tool.requested_by_step_ids),
        )
        for tool in tools
    ]


@router.get("/{agent_id}/versions/{version_id}/export")
def export_my_agents_version(agent: OwnedAgent, version: OwnedVersion) -> dict[str, object]:
    """This version as a portable JSON bundle — the file `POST /agents/import` reads.

    `OwnedAgent`, so this is the creator's own agent and nobody else's. An
    exported bundle is the spec and the graph, which together are the prompts —
    the thing `GET /agents/running` deliberately does not hand a stranger.

    No `response_model`, and that absence is deliberate rather than an oversight.
    The body **is** the two stored documents dumped by their own models, and
    declaring a third schema over them is how a bundle starts differing from the
    documents it claims to be: FastAPI would filter the response against that
    schema, so a field added to `WorkflowJSON` and forgotten here would vanish
    from every export silently. `export_bundle` re-validates both documents on
    the way out, which is the check worth having.

    A version with only half its documents is a 409 rather than half a file —
    see `export_bundle`.
    """
    try:
        return export_bundle(agent, version)
    except BundleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.get("/{agent_id}/versions/{version_id}/flow", response_model=VersionFlowResponse)
def read_my_agents_version_flow(version: OwnedVersion) -> VersionFlowResponse:
    """One version as the graph it runs as, and every permission it asks for — see #20.

    Separate from `GET /versions/{version_id}` rather than folded into it, and
    the split follows what each answer is for. The version read is the whole
    spec, prompts and all, and the history screen deliberately does not pull it
    down to draw a list of statuses; the same argument applies in reverse here —
    a diagram is an outline, and an outline that arrived with every prompt
    attached would be the expensive read pretending to be the cheap one.

    Two things on this response are ones a browser must never work out for
    itself, which is the reason the endpoint exists at all:

    - **The edges.** `next=None` means "the step after this one in the list", and
      that rule belongs to `AgentSpec.next_step_id`. Re-deriving it in the client
      is how a diagram starts disagreeing with the runtime about what an agent
      does — the one thing a diagram may not do.
    - **The risk levels.** They are read off `TOOL_REGISTRY` (rule 1 of
      `src.agents.tools.registry`), so nothing an LLM wrote into a spec can talk
      a tool into a milder one. A frontend that coloured tools by a list of its
      own would eventually disagree with the platform about what is dangerous.

    Same spec, re-validated the same way (`validated_spec`), so the diagram and
    the step list on the screen above it cannot describe two different agents.
    Owner-only for the reason the version read is: an outline of the prompts is
    still the prompts. `OwnedVersion` alone carries both halves of that — the
    agent is yours, the version is that agent's — so unlike its sibling this
    endpoint does not declare the agent it never reads.
    """
    spec = validated_spec(version, subject="Spec của version")
    return VersionFlowResponse(
        version_id=version.id,
        flow=flow_diagram_response(flow_of(spec)),
        tools=requested_tool_responses(requested_tools(spec)),
    )


def sse_line(event: str, data: Mapping[str, object]) -> str:
    """One SSE frame. Shared with V2's workflow run stream, so both name events alike."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def validated_spec(version: AgentVersion, *, subject: str) -> AgentSpec:
    """`version`'s stored spec, re-validated before anything executes it.

    Defense in depth, per ADR-0003 §7: a spec is validated on the way into the
    database, but a row that predates that gate — or was written by a migration
    — must still not reach the runtime unchecked. 422 with the validation error,
    since this is decidable before the stream opens.

    A version with **no** pipeline at all is answered by name rather than with a
    pydantic dump about a missing document. Since #42 that is an ordinary state,
    not a corrupt row: a V2 version at the spec stage describes a business
    problem and has no `AgentSpec` until the Workflow Generator has run.
    """
    if version.spec is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{subject} chưa tồn tại: version đang ở trạng thái {version.status.value!r}",
        )
    try:
        return AgentSpec.model_validate(version.spec)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{subject} không hợp lệ: {exc}",
        ) from None


@router.post(
    "/{agent_id}/versions/{version_id}/refine",
    response_model=RefinedSpecResponse,
    status_code=status.HTTP_201_CREATED,
)
async def refine_my_agents_version(
    agent: OwnedAgent,
    version: OwnedVersion,
    payload: RefineVersionRequest,
    db: Annotated[Session, Depends(get_db)],
    model_factory: Annotated[ChatModelFactory, Depends(get_chat_model_factory)],
) -> RefinedSpecResponse:
    """Change this agent with one sentence, saved as a new draft version — see #18.

    The **only** way to edit an agent, and deliberately so. The flow diagram is
    read-only and there is no form editor: a second edit path would be one a
    creator could use just before saying something in chat, and the chat turn —
    which rewrites the whole spec from the one it was given — would silently
    swallow it.

    Nothing that exists is touched. The version named in the path keeps its spec
    and its status, and if it is the deployed one it goes on serving real users
    while this runs: `create_draft` never moves the pointer (ADR-0005 §2). So a
    creator can refine the agent that is live without the fear that makes people
    stop refining.

    Four refusals, three statuses, chosen by what the reader does next. A request
    that did not say what to change is a 422 — the ordinary "fix this and send it
    again", and no call was made. A platform with no assignable model left is a
    409, because the way out is an admin widening the allowlist, not a retry. A
    meta-agent that could not write a valid spec in its allotted attempts is a 502
    with a sentence saying the current version is untouched — which it is. The
    last is the same 422 net `generate_my_interviews_spec` explains: an admin
    narrowing the allowlist between validation and save.
    """
    try:
        refined = await draft_from_refinement(
            db,
            agent,
            validated_spec(version, subject="Version đang được tinh chỉnh"),
            payload.instruction,
            model_factory=model_factory,
        )
    except BlankInstructionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    except NoAssignableModelError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except SpecGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    except VersionLifecycleError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return RefinedSpecResponse(
        agent_id=refined.agent_id,
        version_id=refined.id,
        status=refined.status,
        spec=AgentSpec.model_validate(refined.spec),
        created_at=refined.created_at,
        refined_from_version_id=version.id,
    )


def conversation_for(agent: Agent, checkpointer: BaseCheckpointSaver, request: ThreadAddress) -> Conversation:
    """The conversation this request addresses — a new one when the client didn't name one.

    `agent.id` comes from the ownership-checked path parameter, never from the
    request, so a caller cannot address another creator's conversation by naming
    its id.
    """
    if request.conversation_id is None:
        thread = ThreadRef.new(agent.id, namespace=request.namespace, caller=request.caller)
    else:
        thread = ThreadRef(
            agent_id=agent.id,
            conversation_id=request.conversation_id,
            namespace=request.namespace,
            caller=request.caller,
        )
    return Conversation(thread=thread, checkpointer=checkpointer)


def thread_line(conversation: Conversation, trace_id: str | None = None) -> str:
    """The `thread` event every run opens with — which conversation it is happening on, and which run this is.

    First, so a client that let the server pick the conversation id knows it
    before any answer arrives and can send it back to stay in the same
    conversation. Sent on every turn, not just the first, so a client that lost
    its state (a reload, a reconnect) can always tell where it is. `trace_id`
    is the run's own name (ADR-0053): the thumbs a person leaves on the answer
    is filed under it, because the ledger row is not written until the stream
    has ended.
    """
    thread = conversation.thread
    return sse_line(
        "thread",
        ThreadStartedEvent(
            conversation_id=thread.conversation_id, namespace=thread.namespace, trace_id=trace_id
        ).model_dump(mode="json"),
    )


@dataclass(slots=True)
class RunLedgerEntry:
    """What one run cost and how it ended, held until it has done both — see #11, #55.

    Production runs are most of an agent's bill and nothing else writes them down:
    `sandbox_runs` only ever knew about rehearsals. Without this, "chi phí thực tế
    tích luỹ theo từng agent" (#11) would report the agents being built and stay
    silent about the ones being used.

    Held rather than written on the spot, and that is the part #55 changed. A run
    announces its cost and *then* dies — `stream_agent_reply` emits the `cost`
    event on the failing path too — so a row written the moment the figure arrives
    is a row that says a broken run went fine. Correcting it afterwards was the
    alternative and it is worse: it leaves the ledger briefly wrong on every
    failure, and permanently wrong whenever the correction is the thing that
    fails. One run, one row, written once the run has finished having opinions.

    `note` takes a plain mapping rather than an `AgentEvent`, because V2's
    workflow runs stream a `WorkflowEvent` — a different closed vocabulary
    (`node` where V1 says `step`) carrying the *same* `cost` payload
    (`CostFigure.as_payload()`). Both are `{event, data}` at the point that
    matters, and two copies of this class would be two places an agent's bill
    could quietly stop being written.
    """

    run: Run
    #: What the person asked and what the agent is called — the two things
    #: the PM's ledger (`agent_runs`, ADR-0053) keeps that the bill never did.
    #: The bill is what a run *cost*; the ledger is what it *was*.
    message: str = ""
    agent_name: str = ""
    cost: CostFigure | None = None
    #: How the run stopped, when it stopped some way other than finishing or
    #: dying: `budget_exceeded` and `awaiting_approval` are events, not
    #: exceptions, and a ledger that only looked at the exception filed both as
    #: successes — a paused run as *two* of them, one per leg.
    ending: RunOutcome | None = None
    #: The run's own record, line by line, built from the same events the
    #: browser renders through the same two mappers the sandbox uses — so what
    #: the PM reads afterwards is what the user saw (ADR-0053 §1).
    transcript: list[SandboxTranscriptEntry] = field(default_factory=list)

    def note(self, event: Mapping[str, Any]) -> None:
        """Keep the figure if this event is the one carrying it, how the run stopped if it says, and the line it adds to the record."""
        if event["event"] == COST_EVENT:
            self.cost = CostFigure.from_payload(event["data"])
        elif event["event"] == "budget_exceeded":
            self.ending = RunOutcome.BUDGET_STOPPED
        elif event["event"] == "awaiting_approval":
            self.ending = RunOutcome.NEEDS_APPROVAL
        line = _ledger_line(event)
        if line is not None:
            self.transcript.append(line)

    async def bank(self, error: BaseException | None = None) -> None:
        """Write the run down — bill and record together — in the runtime's own words if it died.

        A run with no cost event never reached the runtime's accounting at all —
        it was refused before it started — and there is nothing to write. A row
        of zeroes would claim a run happened.
        """
        if self.cost is not None:
            await record_v2_run(
                self.run.identity,
                self.cost,
                error=str(error) if error is not None else None,
                ending=RunOutcome.ERROR if error is not None else self.ending,
                agent_name=self.agent_name,
                input_message=self.message,
                transcript=self.transcript,
            )


def _ledger_line(event: Mapping[str, Any]) -> SandboxTranscriptEntry | None:
    """`event` as a transcript line, whichever runtime spoke it.

    V2's mapper first (it answers to `node_id`), then V1's (`step_id`): the
    two vocabularies name different documents and neither mapper accepts the
    other's lines, so the first that returns a line is the one that owns it.
    A mapper that raises on a malformed payload loses that line, not the run.
    """
    try:
        line = workflow_transcript_entry(event)  # type: ignore[arg-type]
    except (KeyError, ValueError, TypeError):
        line = None
    if line is not None:
        return line
    try:
        return pipeline_transcript_entry(event)  # type: ignore[arg-type]
    except (KeyError, ValueError, TypeError):
        return None


async def _chat_event_stream(spec: AgentSpec, message: str, run: Run) -> AsyncIterator[str]:
    """SSE lines: a `thread` event, then the run's own events as they happen — see ADR-0003, ADR-0006.

    Everything after it comes from the runtime already named (`token`, `tool`,
    `tool_result`, `step`, `cost`); this layer relabels nothing, so what a
    browser sees is what `src.agents.runtime` decided to say.

    A tool or model failure mid-run becomes an `error` event rather than a
    broken stream, since by this point the response has already started and an
    HTTP status code can no longer change. The run is banked after that, not
    before — a run that broke halfway still cost real money, and the sentence it
    broke with is the thing its owner's dashboard is looking for (#55).

    `finally`, and not simply after the `except`: a client that closes the tab
    has this generator shut down at whichever `yield` is pending, and the last
    pending `yield` is the one carrying the `cost` line. Without the `finally`
    the run that just finished would go unbilled because nobody was left watching
    it — which is exactly the traffic #11 said was most of an agent's bill.
    """
    entry = RunLedgerEntry(run, message=message)
    failure: Exception | None = None
    yield thread_line(run.conversation, run.identity.trace_id)
    try:
        async for event in stream_agent_reply(
            spec, message, services=run.services, conversation=run.conversation, sandbox=False
        ):
            entry.note(event)
            yield sse_line(event["event"], event["data"])
    except Exception as exc:  # noqa: BLE001 — surfaced to the client as a structured SSE event
        failure = exc
        yield sse_line("error", {"message": str(exc)})
    finally:
        await entry.bank(failure)


def deployed_version(agent: Agent) -> AgentVersion:
    """The version this agent is actually serving, or a 409 saying it is serving none.

    409 and not 404: the agent exists and the caller is entitled to address it —
    there is simply nothing behind it yet, and "publish one" is a different next
    move from "you have the wrong id". Only the owner can ever see this refusal,
    since an agent with no Deployment is a 404 to everybody else.

    The single place both doors into a running agent read the pointer, so chat
    and `invoke` can never disagree about which version is live. Since #50 the
    pointer is the Deployment's, and a *paused* one still answers here — the
    version it published has not been un-chosen (ADR-0029 §2), and the only
    caller who gets this far on a paused Deployment is its owner.
    """
    if agent.deployed_version is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent chưa có version nào được deploy")
    return agent.deployed_version


def _addressed_namespace(agent: Agent, user: User, requested: ThreadNamespace) -> ThreadNamespace:
    """`requested`, unless it is the owner's rehearsal space and the caller is not the owner.

    `trial` is where a creator's test chatter lives so it stays out of the
    agent's real history (#6). While chat was behind `OwnedAgent` that separation
    needed no guard — the only caller was the owner. Widening chat to the whole
    platform (#15) removed that guarantee and nothing replaced it, so a stranger
    could write turns into a thread only the creator can read back: the creator
    would open a rehearsal and find messages nobody on their team sent.

    403 rather than 404, and this is the one place in this module where that is
    right. Every other refusal here hides whether something exists; this one
    hides nothing — the agent is deployed, the caller was told so by
    `GET /agents/running`, and what they are being refused is a *mode*, not an
    object.
    """
    if requested is ThreadNamespace.TRIAL and agent.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Thread thử là chỗ tập dượt của người tạo — chỉ họ mới chat trên đó",
        )
    return requested


def _thread_caller(namespace: ThreadNamespace, user: User) -> str | None:
    """Who a thread in `namespace` belongs to, when that is a question worth asking.

    Production threads are per end-user since #50 (ADR-0036 §6). Trial threads
    are not: they are the creator's own rehearsal space, shared with the sandbox
    runs that write into them (#9), and stamping a caller onto one would split a
    creator's chat window away from the trial runs it is meant to sit beside.

    Built through `Caller` rather than formatting the key here, so a browser
    session and an API call cannot end up meaning two different things by "the
    same caller".
    """
    return None if namespace is ThreadNamespace.TRIAL else Caller.signed_in(user).key


@router.post("/{agent_id}/chat")
async def chat_with_agent(
    agent: MeteredAgent,
    user: CurrentUser,
    payload: ChatMessageRequest,
    start_run: StartRun,
    checkpointer: Checkpointer,
    db: Annotated[Session, Depends(get_db)],
) -> StreamingResponse:
    """Chat with a running agent. Streams one SSE event per step.

    Open to anyone signed in, not just the owner (#15): the deployed pointer is
    what makes an agent a product, and the point of deploying is that other
    people can use it. `AddressableAgent` is what keeps that from also exposing
    the agents nobody has deployed.

    The version that answers is always `agent.deployed_version`, never the newest
    draft. That is the guarantee the whole version lifecycle exists for — a
    creator may refine their agent all day and the people talking to it are not
    interrupted, because a draft serves nobody until it is approved *and* pointed
    at (ADR-0005 §2, §3).

    The turn is remembered on its thread (#6): send the same `conversation_id`
    back to continue the conversation, omit it to start an empty one. An omitted
    id is minted server-side and announced on the stream, so no caller can land
    in someone else's conversation without already knowing its id. The `trial`
    namespace stays the owner's — see `_addressed_namespace`.

    Rejections happen before the stream opens, so they arrive as an ordinary
    HTTP status rather than an error buried inside an SSE body: no deployed
    version is a 409, a `trial` thread asked for by a stranger is a 403, and a
    stored spec that fails to validate is a 422 — the runtime never sees a spec
    it hasn't first checked for itself (see ADR-0003).

    The conversation is addressed **per caller** since #50: several end-users
    share one Deployment, and none of them can land in another's history even by
    sending back an id they were not given.

    The turn is also filed in the sidebar index (`src.agents.conversations`), and
    filed **here** rather than inside the stream: this is the last point where a
    failure can still become an HTTP status. A row written from inside the
    generator would land after the response had already begun, so a person whose
    conversation never appeared in their sidebar would have no error to show for
    it — and the commit is what makes the conversation findable at all, which is
    the whole feature.
    """
    version = deployed_version(agent)
    spec = validated_spec(version, subject="Version đã deploy")
    namespace = _addressed_namespace(agent, user, payload.namespace)
    conversation = conversation_for(
        agent, checkpointer, ThreadAddress(payload.conversation_id, namespace, _thread_caller(namespace, user))
    )
    record_turn(db, agent_id=agent.id, user_id=user.id, thread=conversation.thread, message=payload.message)
    db.commit()
    run = start_run(version.id, conversation)
    return StreamingResponse(_chat_event_stream(spec, payload.message, run), media_type="text/event-stream")


async def _invocation(spec: AgentSpec, message: str, run: Run) -> AgentInvocationResponse:
    """Run `spec` to completion and gather the whole run into one payload.

    Reads the very same event stream the chat window watches, rather than a
    second execution path for API callers: an integration and a browser asking
    the same agent the same thing must not be able to get different answers, and
    two paths is how they eventually do.

    `tool_result` is what fills `tools`, not `tool`: the announcement exists so a
    *live* reader sees a call before it returns (#8), which is a distinction with
    no meaning once the run is over and the whole thing arrives at once.

    A run that dies is banked on its way out and then re-raised unchanged, so the
    500 this endpoint answers with lands on the owner's dashboard as the failure
    it was (#55). The alternative — letting the exception skip the ledger — would
    make an integration failing on every call the one thing an owner could not
    see, and it is the only thing they would want to.
    """
    steps: list[InvokedStepResponse] = []
    tools: list[InvokedToolResponse] = []
    stopped_reason: str | None = None
    cost: CostFigureResponse | None = None
    entry = RunLedgerEntry(run)

    try:
        async for event in stream_agent_reply(
            spec, message, services=run.services, conversation=run.conversation, sandbox=False
        ):
            entry.note(event)
            data = event["data"]
            if event["event"] == "step":
                steps.append(InvokedStepResponse(step_id=data["step_id"], title=data["title"], content=data["content"]))
            elif event["event"] == "tool_result":
                tools.append(
                    InvokedToolResponse(
                        step_id=data["step_id"], tool=data["tool"], ok=data["ok"], result=data["result"]
                    )
                )
            elif event["event"] == BUDGET_EXCEEDED_EVENT:
                stopped_reason = data["message"]
            elif event["event"] == COST_EVENT:
                cost = CostFigureResponse.model_validate(data)
    except Exception as exc:
        await entry.bank(exc)
        raise
    await entry.bank()

    return AgentInvocationResponse(
        agent_id=run.conversation.thread.agent_id,
        version_id=run.identity.version_id,
        conversation_id=run.conversation.thread.conversation_id,
        # Blank steps are dropped rather than joined as empty lines: a step whose
        # whole job was to call a tool contributes nothing a caller can show.
        answer="\n\n".join(step.content for step in steps if step.content.strip()),
        steps=steps,
        tools=tools,
        stopped_reason=stopped_reason,
        cost=cost,
    )


@router.post("/{agent_id}/invoke", response_model=AgentInvocationResponse)
async def invoke_agent(
    addressed: MeteredByAnyChannel,
    payload: AgentInvocationRequest,
    start_run: StartRun,
    checkpointer: Checkpointer,
) -> AgentInvocationResponse:
    """Call a running agent and get the finished answer back as JSON — see #15, #50.

    The API door onto the same agent the chat window talks to, so a creator's own
    team can put it inside their product. It is nearly free, and that is the
    honest reason it exists: the agent already runs in the platform, so this is
    one more entrance rather than one more runtime.

    Two credentials open it, and this is the endpoint where ADR-0029 §5's "both
    channels, always" is actually true: a browser session as before, or the
    Deployment's own API key in `X-API-Key`. The key admits no *user*, so the
    conversation it opens is the integration's rather than anybody's — which is
    exactly what an integration wants, and what keeps its history out of the
    end-users'.

    Not a stream, on purpose. A caller integrating an agent into a backend wants
    one request and one result; the streaming endpoint stays for readers who are
    watching it happen. Both go through the same deployed version, the same
    memory and the same cost recording — a call made here shows up on the agent's
    bill exactly like a chat turn (#11), because it spent exactly the same money.

    A run that dies mid-way is a 500 carrying the runtime's own sentence: unlike
    the stream, nothing has been sent yet, so the failure can still be an HTTP
    status instead of an event nobody parses. A run stopped by its own budget is
    **not** a failure — it produced real output and says why it stopped, in
    `stopped_reason` (#10).
    """
    agent = addressed.agent
    version = deployed_version(agent)
    spec = validated_spec(version, subject="Version đã deploy")
    conversation = conversation_for(
        agent,
        checkpointer,
        ThreadAddress(payload.conversation_id, ThreadNamespace.PRODUCTION, addressed.caller.key),
    )
    run = start_run(version.id, conversation)
    try:
        return await _invocation(spec, payload.message, run)
    except Exception as exc:  # noqa: BLE001 — the caller gets the reason, not a bare 500
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Lần chạy thất bại: {exc}"
        ) from None


async def _sandbox_event_stream(spec: AgentSpec, message: str, run: Run) -> AsyncIterator[str]:
    """SSE lines for a trial run: the thread, the run's own events, then its record.

    The same event names a production chat streams, so a client renders a trial
    run with the code it already has — plus the closing `sandbox_run` event
    naming the stored transcript. Failures are already handled by
    `stream_sandbox_run`, which turns a dead run into an `error` event *and* a
    `failed` record; there is nothing left for this layer to catch — and the same
    module banks what the trial cost, alongside its transcript.
    """
    yield thread_line(run.conversation, run.identity.trace_id)
    async for event in stream_sandbox_run(
        spec, message, identity=run.identity, services=run.services, conversation=run.conversation
    ):
        yield sse_line(event["event"], event["data"])


@router.post("/{agent_id}/versions/{version_id}/sandbox-runs")
async def trial_run_my_agents_version(
    agent: OwnedAgent,
    version: OwnedVersion,
    payload: SandboxRunRequest,
    start_run: StartRun,
    checkpointer: Checkpointer,
) -> StreamingResponse:
    """Trial one of the caller's own versions in the sandbox — see #9 / ADR-0007.

    Any version may be trialled, in any status: a creator retrying after a bad
    run must not be refused for running twice, and a version already past
    `draft` is simply left where it is (see `src.agents.sandbox`).

    The thread is always `trial`, never the caller's choice — that is what keeps
    test chatter out of the agent's real conversation history.
    """
    spec = validated_spec(version, subject="Version")
    conversation = conversation_for(agent, checkpointer, ThreadAddress(payload.conversation_id, ThreadNamespace.TRIAL))
    run = start_run(version.id, conversation)
    return StreamingResponse(_sandbox_event_stream(spec, payload.message, run), media_type="text/event-stream")


@router.get("/{agent_id}/sandbox-runs/{run_id}", response_model=SandboxRunResponse)
def read_my_agents_sandbox_run(
    agent: OwnedAgent,
    run_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> SandboxRun:
    """One recorded trial run, read back in full — what an approval request attaches.

    Scoped to the agent in the path, so a run is only readable through the agent
    it belongs to: same "don't leak existence" rule as `get_owned_agent`.
    """
    run = db.get(SandboxRun, run_id)
    if run is None or run.version.agent_id != agent.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy lần chạy thử")
    return run


def cost_figure_response(cost: CostFigure) -> CostFigureResponse:
    """A figure on the wire, with the words that say what it is.

    Built from `CostFigure.as_payload()` rather than field by field, so this
    endpoint and the `cost` event a run streams describe a number identically —
    two hand-written renderings would eventually disagree about one of them.
    """
    return CostFigureResponse.model_validate(cost.as_payload())


def version_costs_response(
    version_id: int, *, estimate: CostFigure | None, worst_case: CostFigure | None, measured: CostFigure | None
) -> VersionCostsResponse:
    """The three figures for one version, side by side. Any of them may be absent.

    One builder rather than one per screen: the creator's cost view (#11) and the
    approval dossier (#14) show the same three numbers, and a reader who found a
    ceiling on one screen and not the other would have no way to tell which
    screen was wrong.

    All three are optional since #49, because a V2 version has the two
    projections missing and the measurement present: both projections are
    computed from an `AgentSpec` it does not have, while its trial run spent real
    money and wrote a real `run_costs` row. Refusing to build the object without
    the projections would have dropped the one figure that is a fact.
    """
    return VersionCostsResponse(
        version_id=version_id,
        estimate=cost_figure_response(estimate) if estimate is not None else None,
        worst_case=cost_figure_response(worst_case) if worst_case is not None else None,
        measured=cost_figure_response(measured) if measured is not None else None,
    )


@router.get("/{agent_id}/versions/{version_id}/costs", response_model=VersionCostsResponse)
def read_my_agents_version_costs(
    version: OwnedVersion,
    db: Annotated[Session, Depends(get_db)],
) -> VersionCostsResponse:
    """The three cost figures for one version — see #11 / ADR-0009.

    Three fields and not one number, because the three are different kinds of
    claim: a guess, a ceiling the runtime enforces, and a measurement of a run
    that happened. Merging them is the fastest way to make a reader disbelieve
    all three, so the API refuses to offer a merged one.

    The estimate and the ceiling are computed on the spot from the version's own
    immutable spec — there is nothing to cache and nothing to go stale. The
    measurement is looked up, because it is a fact about a run rather than a
    property of the spec.
    """
    spec = validated_spec(version, subject="Version")
    return version_costs_response(
        version.id,
        estimate=estimated_cost(spec),
        worst_case=worst_case_cost(spec),
        measured=latest_trial_cost(db, version.id),
    )


@router.get("/{agent_id}/costs", response_model=AgentCostsResponse)
def read_my_agents_costs(agent: OwnedAgent, db: Annotated[Session, Depends(get_db)]) -> AgentCostsResponse:
    """What this agent has actually cost so far, across every run it has ever had.

    Zeroes for an agent that has never run, not a 404: "nothing yet" is a true
    and useful answer, and an error here would make a creator's cost screen fail
    for exactly the agents they just created.
    """
    totals = agent_spend(db, agent_ids=[agent.id])
    if not totals:
        return AgentCostsResponse(agent_id=agent.id, runs=0, tokens=0, usd=0.0, trial_usd=0.0, production_usd=0.0)
    spend = totals[0]
    return AgentCostsResponse(
        agent_id=spend.agent_id,
        runs=spend.runs,
        tokens=spend.tokens,
        usd=spend.usd,
        trial_usd=spend.trial_usd,
        production_usd=spend.production_usd,
    )


@router.get("/{agent_id}/threads/{conversation_id}", response_model=ThreadHistoryResponse)
async def read_my_agents_thread(
    agent: OwnedAgent,
    user: CurrentUser,
    checkpointer: Checkpointer,
    conversation_id: Annotated[str, Path(pattern=CONVERSATION_ID_PATTERN.pattern)],
    namespace: Annotated[ThreadNamespace, Query(description="`trial` để đọc thread của lần chạy thử")] = (
        ThreadNamespace.PRODUCTION
    ),
) -> ThreadHistoryResponse:
    """Read one thread back, for tracing what an agent actually said.

    Behind `OwnedAgent` like every other agent endpoint, so a conversation is
    only ever readable by the creator who owns the agent it happened on — and
    since #50, only their *own* conversations on it. A production thread is
    addressed per caller (ADR-0036 §6), so the owner reading back
    this endpoint sees what they themselves said to their agent, not what its
    end-users did. Reading other people's conversations would be a surveillance
    surface nobody asked for; what an owner needs about a run they did not have
    is in the trace and the cost ledger (#11), which is where it belongs.

    A thread nobody has used is not an error — it is an empty conversation, and
    a 404 here would let a caller probe which conversation ids exist.
    """
    conversation = conversation_for(
        agent, checkpointer, ThreadAddress(conversation_id, namespace, _thread_caller(namespace, user))
    )
    return ThreadHistoryResponse(
        conversation_id=conversation.thread.conversation_id,
        namespace=conversation.thread.namespace,
        turns=[TranscriptTurnResponse(**turn) for turn in await conversation.transcript()],
    )


# ─────────────────────────────────────────────────────────────────────────────
# The chat sidebar — going back into a conversation.
#
# On `AddressableAgent` rather than `OwnedAgent`, which is the split that matters
# here. `read_my_agents_thread` above is a *creator* tracing their own agent, so
# it is owner-only; these are an end-user reaching their own conversations on
# somebody else's published agent, which is the ordinary case for a deployed
# product and one an owner-only rule cannot express.
#
# Widening the dependency widens nothing else, because none of these four take
# the caller's word for whose conversation it is: the index is queried by
# `user_id` from the session, and the transcript is read from a checkpoint key
# that already has the caller baked into it (ADR-0036 §6). An id belonging to
# somebody else therefore reads back as an empty conversation rather than as
# theirs — the isolation is the address, not a check beside it.
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/{agent_id}/conversations", response_model=list[ConversationResponse])
def list_my_conversations(
    agent: AddressableAgent,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    namespace: Annotated[ThreadNamespace, Query(description="`trial` để xem các lần chạy thử của chính chủ agent")] = (
        ThreadNamespace.PRODUCTION
    ),
) -> list[ConversationResponse]:
    """This person's conversations on this agent, most recent first.

    Never a 404 for "you have none": an empty list is the correct answer for
    somebody who has not talked to this agent yet, and it is also what the
    sidebar draws on first open. Turning that into an error would make the
    ordinary first visit look like a failure.

    `namespace` goes through `_addressed_namespace`, so asking for `trial` on an
    agent that is not yours is the same 403 the chat endpoint gives — the
    rehearsal space stays the creator's on every door into it, not just the one
    that writes.
    """
    addressed = _addressed_namespace(agent, user, namespace)
    return [
        ConversationResponse.model_validate(conversation)
        for conversation in list_conversations(db, agent_id=agent.id, user_id=user.id, namespace=addressed)
    ]


@router.get("/{agent_id}/conversations/{conversation_id}/messages", response_model=ThreadHistoryResponse)
async def read_my_conversation(
    agent: AddressableAgent,
    user: CurrentUser,
    checkpointer: Checkpointer,
    db: Annotated[Session, Depends(get_db)],
    conversation_id: Annotated[str, Path(pattern=CONVERSATION_ID_PATTERN.pattern)],
    namespace: Annotated[ThreadNamespace, Query(description="`trial` để đọc thread của lần chạy thử")] = (
        ThreadNamespace.PRODUCTION
    ),
) -> ThreadHistoryResponse:
    """What was said in one of this person's conversations — what the panel renders.

    The transcript still comes from the checkpoint (ADR-0004 §4) and not from the
    index: the index holds a label, and a screen that drew the conversation from
    it would be drawing something nobody said.

    Deliberately **not** joined against the index first. The checkpoint key is
    already per caller, so an id somebody else owns reads back empty either way,
    and requiring an index row would mean a conversation that predates this
    feature could no longer be opened even by the person who had it.
    """
    addressed = _addressed_namespace(agent, user, namespace)
    conversation = conversation_for(
        agent, checkpointer, ThreadAddress(conversation_id, addressed, _thread_caller(addressed, user))
    )
    return ThreadHistoryResponse(
        conversation_id=conversation.thread.conversation_id,
        namespace=conversation.thread.namespace,
        turns=[TranscriptTurnResponse(**turn) for turn in await conversation.transcript()],
        pending_approval=_pending_pause_of(db, agent, conversation.thread),
    )


def _pending_pause_of(db: Session, agent: Agent, thread: ThreadRef) -> PendingApprovalResponse | None:
    """The approval this thread's latest run is stopped at, or `None`.

    Read from `run_approvals` rather than from the checkpoint: the checkpoint
    knows it is interrupted but not which row a decision has to be written on,
    and the row is what both the buttons and the "đang chờ" card need.

    Keyed on the thread and not only on the agent, for the reason the thread is
    addressed per caller (ADR-0036 §6): two end-users of one Deployment each
    have their own conversation, and a pause in one must never show up as a
    card in the other's. The caller part of the address was added to the row
    by `66bab42a57ec`; a row written before that has no caller and matches no
    production thread here, which is the safe direction to be wrong in.
    """
    row = db.scalars(
        select(RunApproval)
        .where(
            RunApproval.agent_id == agent.id,
            RunApproval.conversation_id == thread.conversation_id,
            RunApproval.namespace == thread.namespace.value,
            RunApproval.caller == thread.caller,
            RunApproval.status == RunApprovalStatus.PENDING,
        )
        .order_by(RunApproval.created_at.desc(), RunApproval.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return PendingApprovalResponse(
        approval_id=row.id,
        node_id=row.node_id,
        approver_role=row.approver_role,
        message=row.message,
        actions=[ApprovalActionResponse.model_validate(action) for action in row.actions or []],
    )


@router.patch("/{agent_id}/conversations/{conversation_id}", response_model=ConversationResponse)
def rename_my_conversation(
    agent: AddressableAgent,
    user: CurrentUser,
    payload: ConversationRenameRequest,
    db: Annotated[Session, Depends(get_db)],
    conversation_id: Annotated[str, Path(pattern=CONVERSATION_ID_PATTERN.pattern)],
    namespace: Annotated[ThreadNamespace, Query()] = ThreadNamespace.PRODUCTION,
) -> ConversationResponse:
    """Give one conversation a name that means something to its owner.

    404 when the row is not this person's, which is the same answer as "no such
    conversation" — the rule the rest of this module follows, and the one that
    keeps a rename from being a way to discover that somebody else's
    conversation exists.

    Only the label moves. Renaming does not touch `last_message_at`, so tidying
    up old conversations does not reshuffle them to the top of the list.
    """
    addressed = _addressed_namespace(agent, user, namespace)
    conversation = find_conversation(
        db, agent_id=agent.id, user_id=user.id, conversation_id=conversation_id, namespace=addressed
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy cuộc trò chuyện")
    conversation.title = payload.title
    db.commit()
    db.refresh(conversation)
    return ConversationResponse.model_validate(conversation)


@router.delete("/{agent_id}/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_my_conversation(
    agent: AddressableAgent,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    conversation_id: Annotated[str, Path(pattern=CONVERSATION_ID_PATTERN.pattern)],
    namespace: Annotated[ThreadNamespace, Query()] = ThreadNamespace.PRODUCTION,
) -> None:
    """Take one conversation out of the sidebar.

    **The index row goes; the checkpoint stays.** This is a deliberate limit, not
    an oversight. LangGraph owns the checkpoint tables and migrates them itself
    (ADR-0004 §2), so deleting a thread's rows from here would mean this module
    reaching into a schema it does not own and issuing DELETEs the library has
    never promised will keep working.

    What that means for the person pressing the button: the conversation stops
    being listed and stops being reachable through any screen, but the turns
    remain in the checkpointer and would come back if the same id were addressed
    again. It is "remove from my list", not "erase". A real erase needs a
    supported deletion path from LangGraph, and promising one in the UI before
    that exists would be promising something this code does not do.
    """
    conversation = find_conversation(
        db,
        agent_id=agent.id,
        user_id=user.id,
        conversation_id=conversation_id,
        namespace=_addressed_namespace(agent, user, namespace),
    )
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy cuộc trò chuyện")
    db.delete(conversation)
    db.commit()
