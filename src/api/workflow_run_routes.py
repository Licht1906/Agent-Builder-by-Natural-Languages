"""Chatting with a confirmed workflow — the door onto V2's runtime, #45.

One endpoint, and it is deliberately not `POST /agents/{id}/chat`. V1's chat door
answers from `agent.deployed_version`, which is the pointer the whole version
lifecycle exists to protect: only an *approved* version ever sits behind it
(ADR-0005). A workflow that has just been confirmed is three gates short of that
— no sandbox evidence, no evaluation, no admin decision — so it gets its own
door, addressed by version and open only to the creator who owns it.

That is what "deployed for test" means here, and the thread namespace says the
same thing: every turn lands in `trial`, never in an agent's production history.
The real `Deployment` (ADR-0029, #50) is what opens a workflow to anybody else.

Everything downstream of the ownership check is V1's machinery unchanged —
`RunFactory` mints the identity and opens the trace, `Conversation` addresses the
checkpointer, `sse_line` writes the frames, `RunLedgerEntry` banks the
bill. The only V2-specific parts are which document is executed and which
vocabulary its events speak.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from src.agents.conversations import record_turn
from src.agents.runtime import Conversation
from src.agents.threads import ThreadNamespace
from src.agents.versions import WORKFLOW_RUNNABLE
from src.api.agent_routes import (
    Checkpointer,
    OwnedAgent,
    OwnedVersion,
    Run,
    RunLedgerEntry,
    StartRun,
    ThreadAddress,
    conversation_for,
    deployed_version,
    sse_line,
    thread_line,
)
from src.api.deployment_access import Caller, MeteredAgent
from src.auth.dependencies import CurrentUser
from src.datasources.graph import data_query_configs
from src.datasources.reader import DatabaseReader, get_database_reader
from src.db.base import get_db
from src.db.models import Agent, AgentVersion
from src.models.schemas import ApprovalDecisionRequest, AskerResponse, SandboxRunRequest, WorkflowChatRequest
from src.models.workflow import WorkflowJSON
from src.workflow.approvals import (
    ApprovalDecisionError,
    ParkedLeg,
    approval_for_decision,
    approval_payload,
    decide_approval,
    parked_leg,
    pending_approvals,
)
from src.workflow.generator import WorkflowStageError, workflow_of
from src.workflow.knowledge import KnowledgeStore, get_knowledge_store
from src.workflow.runtime import Resumption, UnsupportedNodeError, ensure_runnable, stream_workflow_reply
from src.workflow.sandbox import stream_workflow_sandbox_run
from src.workflow.validation import WorkflowGraphError

router = APIRouter(prefix="/agents", tags=["workflow"])

#: Where a `KnowledgeSearch` node's passages come from, as an endpoint annotation.
#: A dependency rather than something the runtime reaches for, so a test grounds a
#: run in documents it wrote itself — see `src.workflow.knowledge`.
Knowledge = Annotated[KnowledgeStore, Depends(get_knowledge_store)]
#: What a `data_query` node reads through — `Knowledge`'s twin for a Hệ CSDL (DS-04).
Databases = Annotated[DatabaseReader, Depends(get_database_reader)]

#: The request's own session, for the two endpoints here that read and write rows
#: rather than only streaming. The streaming ones deliberately do not take it:
#: by the time they write anything the response body is open and FastAPI has
#: closed this session (see `store_sandbox_run`).
Db = Annotated[Session, Depends(get_db)]


@router.post("/{agent_id}/versions/{version_id}/workflow-chat")
async def chat_with_my_versions_workflow(
    agent: OwnedAgent,
    version: OwnedVersion,
    payload: WorkflowChatRequest,
    start_run: StartRun,
    checkpointer: Checkpointer,
    knowledge: Knowledge,
    databases: Databases,
) -> StreamingResponse:
    """Run this version's confirmed workflow against one message — user story #17.

    Owner-only and version-addressed, because what is being run has not been
    approved: see the module docstring for why this is not V1's `/chat`.

    Every refusal happens **before the stream opens**, so it arrives as an
    ordinary HTTP status rather than an error buried in an SSE body. All three are
    409 and none is a 422, because none of them is about the request: a version
    that has not confirmed its graph is a screen the creator has not finished, a
    graph that no longer validates is a document to go and fix, and a graph
    holding a node type the runtime cannot execute yet is this platform being
    behind rather than the creator being wrong.

    The turn is remembered on its thread: send the same `conversation_id` back to
    continue the conversation, omit it to start an empty one. The id is announced
    on the stream's first event, so a client that let the server pick one knows it
    before any answer arrives.
    """
    workflow = runnable_workflow(version)
    conversation = conversation_for(agent, checkpointer, ThreadAddress(payload.conversation_id, ThreadNamespace.TRIAL))
    run = start_run(version.id, conversation)
    return StreamingResponse(
        _workflow_event_stream(workflow, payload.message, run, knowledge, databases),
        media_type="text/event-stream",
    )


@router.post("/{agent_id}/deployed-workflow-chat")
async def chat_with_deployed_workflow(
    agent: MeteredAgent,
    user: CurrentUser,
    payload: WorkflowChatRequest,
    start_run: StartRun,
    checkpointer: Checkpointer,
    knowledge: Knowledge,
    databases: Databases,
    db: Db,
) -> StreamingResponse:
    """Talk to a **published** agent whose version runs a workflow — V2's end-user door.

    The counterpart of `POST /agents/{id}/chat`, which reads `AgentVersion.spec`
    and therefore only ever answers for a V1 pipeline: a V2 version has no
    `AgentSpec` at all (ADR-0020 §5), so that door hands a 422 to every workflow
    agent ever deployed. Two doors rather than one for the reason
    `trial_run_my_versions_workflow` gives — a single endpoint would have to
    guess which of two documents the caller meant from which column happened to
    be filled.

    **The Deployment is the gate, and it is the only gate.** Unlike the
    version-addressed doors above, nothing here consults `WORKFLOW_RUNNABLE`:
    what runs is `agent.deployed_version`, and `src.deployments.publication.publish`
    refuses to point a Deployment at anything but an `approved` version. Checking
    the status again would be a second statement of a rule that already has one,
    and V1's `/chat` does not check it either for exactly that reason.

    Everything else is that door's, unchanged. `MeteredAgent` answers "may this
    caller reach this publication, and have they asked too often" (ADR-0029 §2–3,
    ADR-0037), the thread is `production` and addressed **per caller** so several
    end-users of one Deployment never land in each other's history, and a run
    that stops at a `HumanApproval` node ends its stream with `awaiting_approval`
    exactly as a trial does — deciding it is still the owner's endpoint, because
    the pause belongs to the version's creator and not to whoever it stopped in
    front of.

    The turn is also filed in the sidebar index, which is what makes this
    conversation findable again tomorrow. It happens here rather than inside the
    stream because this is the last point at which a failure can still be an
    HTTP status: a row written from the generator would land after the response
    had started, leaving a person whose conversation never appeared in their
    list with no error to show for it.
    """
    workflow = deployed_workflow(agent)
    conversation = conversation_for(
        agent,
        checkpointer,
        ThreadAddress(payload.conversation_id, ThreadNamespace.PRODUCTION, Caller.signed_in(user).key),
    )
    record_turn(db, agent_id=agent.id, user_id=user.id, thread=conversation.thread, message=payload.message)
    db.commit()
    run = start_run(deployed_version(agent).id, conversation)
    return StreamingResponse(
        _workflow_event_stream(workflow, payload.message, run, knowledge, databases),
        media_type="text/event-stream",
    )


def deployed_workflow(agent: Agent) -> WorkflowJSON:
    """The graph `agent` currently serves, refusing by name if it serves a pipeline instead.

    `deployed_version` answers "is anything published"; this answers "and is what
    was published a workflow". They are separate refusals because they send the
    caller to different places: an agent with nothing deployed needs publishing,
    while a V1 agent reached through this door needs the *other* door — and a
    client that got one sentence for both would have no way to offer either.

    `ensure_runnable` re-runs the confirm gate's validator here as well, the
    defence in depth ADR-0003 §7 asks of every stored document: a graph written
    before a rule existed must not reach the runtime just because somebody
    published it first.
    """
    version = deployed_version(agent)
    try:
        workflow = workflow_of(version)
    except WorkflowStageError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    try:
        ensure_runnable(workflow)
    except (WorkflowGraphError, UnsupportedNodeError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return workflow


def _workflow_to_resume(agent: Agent, version: AgentVersion) -> WorkflowJSON:
    """The graph to carry a paused run on with — validated the way *that run* was.

    A run pauses at a `HumanApproval` node on whichever door started it, and the
    two doors have deliberately different rules about which versions may run:
    `runnable_workflow` refuses anything past `pending_approval`, because it is
    the creator's door onto their own **unapproved** version; `deployed_workflow`
    checks no status at all, because for a published agent the Deployment is the
    gate and the only gate (see both docstrings).

    Deciding always went through the first one, so a run that paused on a
    **deployed** agent could never be resumed: the decision was recorded and the
    resume died with *"trạng thái 'approved' — phải xác nhận workflow trước khi
    chạy thử"*, a sentence about trialling an unfinished draft shown to somebody
    approving a live action. The pause is on the same graph either way; only the
    door it came through differs, so that is what this reads.
    """
    if agent.deployed_version_id == version.id:
        return deployed_workflow(agent)
    return runnable_workflow(version)


def runnable_workflow(version: AgentVersion) -> WorkflowJSON:
    """`version`'s workflow, re-validated and checked against what the runtime can run.

    Three refusals in the order a creator would meet them: no workflow at all, a
    stage that has not reached `workflow_confirmed`, and a graph the runtime
    cannot honestly execute.

    The stage check is what keeps ADR-0022 §5's promise operational. A workflow
    at `workflow_pending_confirmation` is still being edited on the canvas, and
    running it would mean chatting with a document that changes between turns —
    the confirm gate exists precisely so that everything downstream of it reads
    something immutable.

    `WORKFLOW_RUNNABLE` and not one exact stage, since #46: a successful trial
    promotes the version to `sandbox_passed`, so an exact check would refuse the
    creator's *second* run on the strength of their first one having worked.
    Which stages count is the lifecycle module's answer, not this route's.

    `ensure_runnable` re-runs the confirm gate's own validator on the way out,
    the defence in depth ADR-0003 §7 asks of every stored document: a row written
    before a rule existed must not reach the runtime either.
    """
    try:
        workflow = workflow_of(version)
    except WorkflowStageError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    if version.status not in WORKFLOW_RUNNABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Workflow của version này đang ở trạng thái {version.status.value!r} — "
                f"phải xác nhận workflow trước khi chạy thử"
            ),
        )
    try:
        ensure_runnable(workflow)
    except (WorkflowGraphError, UnsupportedNodeError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return workflow


async def _workflow_event_stream(
    workflow: WorkflowJSON,
    message: str,
    run: Run,
    knowledge: KnowledgeStore,
    databases: DatabaseReader,
    *,
    resume: Resumption | None = None,
) -> AsyncIterator[str]:
    """SSE lines: a `thread` event, then the run's own events as they happen.

    This layer relabels nothing, so what a browser sees is what
    `src.workflow.runtime` decided to say — `node_started`, `token`, `tool`,
    `tool_result`, `node`, `awaiting_approval`, `usage`, `cost`.

    A node that dies mid-run becomes an `error` event rather than a broken
    stream: by this point the response has started and an HTTP status can no
    longer change. The run is banked *after* that rather than on its `cost`
    event, because the runtime emits `cost` on the failing path too — so a row
    written the moment the figure arrives would say a broken run went fine
    (`RunLedgerEntry`). The `finally` is what keeps that from costing anything: a
    client that closes the tab shuts this generator down at the pending `yield`,
    which on a finished run is the one carrying the `cost` line.

    `resume` is the *same* run carrying on after a person decided (#47), which is
    why it is a parameter here rather than a second function: from the client's
    side nothing has changed, and the two would have differed by one keyword and
    drifted apart at the first fix applied to only one of them.
    """
    entry = RunLedgerEntry(run, message=message)
    failure: Exception | None = None
    yield thread_line(run.conversation, run.identity.trace_id)
    try:
        async for event in stream_workflow_reply(
            workflow,
            message,
            services=run.services,
            conversation=run.conversation,
            knowledge=knowledge,
            databases=databases,
            sandbox=False,
            identity=run.identity,
            resume=resume,
        ):
            entry.note(event)
            yield sse_line(event["event"], event["data"])
    except Exception as exc:  # noqa: BLE001 — surfaced to the client as a structured SSE event
        failure = exc
        yield sse_line("error", {"message": str(exc)})
    finally:
        await entry.bank(failure)


async def _workflow_sandbox_stream(
    workflow: WorkflowJSON,
    message: str,
    run: Run,
    knowledge: KnowledgeStore,
    databases: DatabaseReader,
    *,
    resume: tuple[ParkedLeg, Resumption] | None = None,
    live_sends: bool = False,
) -> AsyncIterator[str]:
    """SSE lines for a trial run: the thread, the run's own events, then its record.

    The same event names the chat stream above writes, so a client renders a
    trial run of a graph with the code it already has — plus the closing
    `sandbox_run` event naming the stored transcript, or an `awaiting_approval`
    where a person has to decide before there is anything to store.

    Nothing is caught here, unlike the chat stream: `stream_workflow_sandbox_run`
    already turns a dead run into an `error` event *and* a `failed` record, and a
    second catch at this layer would swallow the failure before the record could
    be written. That module also banks what the trial cost, which is why
    `RunLedgerEntry` is absent — a second writer would bill the run twice.

    The parked leg travels *with* its `Resumption` because neither is usable
    without the other: the transcript to prepend and the decision to resume with
    are two halves of one paused run.
    """
    yield thread_line(run.conversation, run.identity.trace_id)
    async for event in stream_workflow_sandbox_run(
        workflow,
        message,
        identity=run.identity,
        services=run.services,
        conversation=run.conversation,
        knowledge=knowledge,
        databases=databases,
        resume=resume,
        live_sends=live_sends,
    ):
        yield sse_line(event["event"], event["data"])


@router.get("/{agent_id}/versions/{version_id}/workflow-approvals")
def my_versions_pending_approvals(
    agent: OwnedAgent,
    version: OwnedVersion,
    db: Db,
) -> list[dict[str, object]]:
    """Every run of this version currently waiting on a person — #47 / ADR-0024 §2.

    The list a checkpoint cannot produce. `interrupt()` suspends a run and the
    checkpointer knows exactly where, but nothing can ask Postgres "which runs
    are waiting" without deserialising every in-flight checkpoint — which is why
    `run_approvals` is written alongside them and why this endpoint reads that
    table rather than the graph's state.

    Owner-scoped through `OwnedAgent`/`OwnedVersion`, for the reason the chat door
    is: a workflow at `workflow_confirmed` is three gates short of being anybody
    else's business.
    """
    return [approval_payload(row) for row in pending_approvals(db, version_id=version.id)]


@router.post("/{agent_id}/versions/{version_id}/workflow-approvals/{approval_id}")
async def decide_and_resume_my_versions_run(
    agent: OwnedAgent,
    version: OwnedVersion,
    approval_id: int,
    payload: ApprovalDecisionRequest,
    user: CurrentUser,
    db: Db,
    start_run: StartRun,
    checkpointer: Checkpointer,
    knowledge: Knowledge,
    databases: Databases,
) -> StreamingResponse:
    """Decide one pending approval, and stream the rest of the run it was holding up.

    **The decision is recorded before the graph moves**, never after. A resume
    that died mid-stream must still leave a permanent record that somebody
    decided — and the `run_approvals` row, not the checkpoint, is what an auditor
    reads a year later.

    The run then continues from its checkpoint with `Command(resume=...)` on the
    thread it was already on: `leg.identity` is the identity it started with,
    including its `trace_id`, so both halves of one execution land in one trace.
    Nothing about the run is rebuilt from this request, which is what stops a
    decision being filed against a version the decider happened to name.

    Both refusals are 409 rather than 422: an approval that is not this version's
    and one that somebody already decided are both facts about the world having
    moved on, not about the request being malformed. A missing reason on a
    rejection is 409 too, for the same reason — it is the domain rule ADR-0012 §2
    states, enforced where the decision is made rather than at the boundary.

    **Who may decide.** This is the creator's own door onto their own unapproved
    version, exactly as `workflow-chat` is (see the module docstring), so
    ownership is the check. ADR-0024 §5's `AdminUser` boundary is about the
    reviewer-facing surface over *deployed* runs, which that ADR leaves to a
    later ticket and which is not this route.
    """
    workflow = _workflow_to_resume(agent, version)
    try:
        row = approval_for_decision(db, approval_id, version_id=version.id)
        decision = decide_approval(db, row, decided_by=user.id, approved=payload.approved, reason=payload.reason)
    except ApprovalDecisionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None

    leg = parked_leg(db, row)
    run = start_run.resumed(leg.identity, Conversation(thread=leg.identity.thread, checkpointer=checkpointer))
    resumption = Resumption(
        run_id=leg.run_id,
        decision=decision,
        carried_seconds=leg.carried_seconds,
        carried_usage=leg.carried_usage,
    )
    # `leg.input_message`, never the deciding request: the run was asked
    # something before this request existed, and a record naming the approver's
    # click as the user's question would describe a run that never happened.
    # The asker likewise is the first leg's (DS-05), never the decider.
    reader = databases.asked_by(leg.run_as)
    stream = (
        _workflow_sandbox_stream(workflow, leg.input_message, run, knowledge, reader, resume=(leg, resumption))
        if leg.sandbox
        else _workflow_event_stream(workflow, leg.input_message, run, knowledge, reader, resume=resumption)
    )
    return StreamingResponse(stream, media_type="text/event-stream")


@router.post("/{agent_id}/versions/{version_id}/workflow-sandbox-runs")
async def trial_run_my_versions_workflow(
    agent: OwnedAgent,
    version: OwnedVersion,
    payload: SandboxRunRequest,
    start_run: StartRun,
    checkpointer: Checkpointer,
    knowledge: Knowledge,
    databases: Databases,
) -> StreamingResponse:
    """Trial one of the caller's own confirmed workflows — see #46 / ADR-0026 / ADR-0032.

    The V2 counterpart of `POST /versions/{id}/sandbox-runs`, and a second door
    rather than a widening of that one: the V1 route reads `AgentVersion.spec`
    and a V2 version has none, so a single endpoint would have had to guess which
    of two documents the caller meant from which column happened to be filled.

    Refused before the stream opens by the same three checks the chat door uses,
    so a creator meets one HTTP status and one sentence rather than an error
    buried in an SSE body. A version that has *already* passed may be trialled
    again — a creator retrying after a bad run must not be refused for running
    twice, and `store_sandbox_run` leaves a version past the gate where it is.

    The thread is always `trial`, never the caller's choice (AC 5): that is what
    keeps test chatter out of the agent's real conversation history.

    `run_as` is who this turn's `data_query` reads are scoped to — the creator
    standing in for one of their company's employees (DS-05, ADR-0059 §3),
    legitimate only here, in a sandbox run by the person who registered the
    sample database. Per turn, so the same thread can ask as a rep and then as
    their lead.

    `send_for_real` is DS-09's switch, per turn for the same reason and off
    unless this request says otherwise: the approved actions of *this* run go
    out for real, through the creator's own connection, and the next turn is a
    rehearsal again.
    """
    workflow = runnable_workflow(version)
    conversation = conversation_for(agent, checkpointer, ThreadAddress(payload.conversation_id, ThreadNamespace.TRIAL))
    run = start_run(version.id, conversation)
    return StreamingResponse(
        _workflow_sandbox_stream(
            workflow,
            payload.message,
            run,
            knowledge,
            databases.asked_by(payload.run_as),
            live_sends=payload.send_for_real,
        ),
        media_type="text/event-stream",
    )


@router.get("/{agent_id}/versions/{version_id}/askers", response_model=list[AskerResponse])
def my_versions_askers(
    _agent: OwnedAgent, version: OwnedVersion, databases: Databases, q: str = ""
) -> list[AskerResponse]:
    """Who the creator can run this version's trial as — "Chạy với tư cách…"'s suggestions (DS-05).

    Read off the employee view of every Hệ CSDL this version's `data_query`
    nodes read, narrowed by `q` against both the sign-in value and the
    position. Empty for a version with no `data_query` node — the screen hides
    the box then — and for one with no workflow yet. Suggestions only: a
    creator may still type anyone, and somebody the view does not know is
    simply denied every read.
    """
    try:
        workflow = workflow_of(version)
    except WorkflowStageError:
        return []
    return [
        AskerResponse(value=asker.value, position=asker.position, source=asker.source)
        for asker in databases.askers(data_query_configs(workflow), q)
    ]
