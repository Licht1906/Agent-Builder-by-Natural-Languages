"""V2's second and third screens: getting a workflow, then editing it — #43 and #44.

Four endpoints on the version, because a workflow belongs to a version the same
way its spec does and inherits the same ownership checks (`OwnedVersion`), plus
one that belongs to nobody: the node-type table the canvas draws itself from.

`POST .../workflow` is written as a creation, not as an idempotent generate:
running the Generator again on a version that already holds a draft would
discard whatever the creator has since done to it on the canvas. So the second
call is a 409, and the way to get a different workflow is a new version — the
same rule V1 drew around anything already being worked on.

`PUT .../workflow` and `POST .../workflow/confirm` are the canvas's two writes,
and the difference between them is the whole of ADR-0022 §6. **The PUT never
refuses a graph for being unfinished** — it answers with `problems` so the canvas
can draw them, which is a report, not a rejection. The confirm is where that same
list becomes a refusal.

Nothing in this module decides *what* is wrong with a workflow. Every problem on
every response comes from `src.workflow.validation`, so the rings the canvas
draws and the sentence the confirm refuses with are the same judgement asked
twice rather than two implementations that can disagree.
"""

import json
from collections.abc import Iterable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.admin.config import read_config
from src.agents.versions import WORKFLOW_EDITABLE, version_number, wrong_stage
from src.api.agent_routes import OwnedAgent, OwnedVersion
from src.auth.dependencies import CurrentUser
from src.db.base import get_db
from src.db.models import (
    AgentVersion,
    PlatformCallPurpose,
    SandboxRun,
    TemplateSubmission,
    ToolRegistration,
    WorkbenchTurn,
    WorkbenchTurnRole,
)
from src.metaagent.catalog import creator_catalog
from src.metaagent.loop import (
    HISTORY_TURNS,
    CatalogTool,
    HistoryTurn,
    TurnRequest,
    run_turn,
)
from src.models.model_catalog import default_node_model
from src.models.node_registry import NODE_REGISTRY, NodeType
from src.models.schemas import (
    NodeTypeResponse,
    TemplateSubmissionRequest,
    TemplateSubmissionResponse,
    ToolActionResponse,
    ToolActionReviewResponse,
    ToolAllowedActionsRequest,
    ToolCatalogEntryResponse,
    ToolCredentialResponse,
    ToolMockResponsesRequest,
    ToolRegistrationRequest,
    ToolRegistrationResponse,
    WorkbenchStepResponse,
    WorkbenchTurnRequest,
    WorkbenchTurnResponse,
    WorkflowDraftRequest,
    WorkflowDraftResponse,
    WorkflowProblemResponse,
    WorkflowRefinementResponse,
    WorkflowRefineRequest,
    WorkflowTemplateResponse,
)
from src.models.solution_spec import DataSourceKind
from src.models.workflow import Edge, NodeInstance, WorkflowJSON
from src.services.llm import ChatModelFactory, get_chat_model_factory, meta_agent_model
from src.services.platform_spend import metered
from src.services.quality_scores import record_spec_lint
from src.services.secret_store import EncryptedDbSecretStore
from src.solution.specs import SpecStageError
from src.workflow.drafts import confirm_workflow, save_workflow
from src.workflow.generator import WorkflowStageError, draft_workflow, workflow_of
from src.workflow.mcp import McpTransport, get_mcp_transport
from src.workflow.refinement import BlankGraphInstructionError, GraphRefinementError, refine_workflow
from src.workflow.registrations import (
    ReviewedAction,
    ToolRegistrationError,
    approved_registrations,
    register_tool,
    registration_id_of,
    registrations_of,
    reviewed_actions,
    set_allowed_actions,
    set_mock_responses,
)
from src.workflow.submissions import TemplateSubmissionError, submit_template
from src.workflow.templates import approved_templates
from src.workflow.tools import TOOL_DEFINITIONS, ToolAction, ToolCredential, ToolDefinition, ToolStatus
from src.workflow.validation import GraphProblem, WorkflowGraphError, graph_problems, graph_warnings

router = APIRouter(prefix="/agents", tags=["workflow"])

#: The node-type table, which hangs off no agent and no version. A separate
#: router because its path is not `/agents/...` — the palette is the same eight
#: types for every creator on the platform, and scoping it under a version would
#: imply a workflow could have a different set.
registry_router = APIRouter(prefix="/workflow", tags=["workflow"])

ModelFactory = Annotated[ChatModelFactory, Depends(get_chat_model_factory)]


@router.post(
    "/{agent_id}/versions/{version_id}/workflow",
    response_model=WorkflowDraftResponse,
    status_code=status.HTTP_201_CREATED,
)
async def draft_my_versions_workflow(
    _agent: OwnedAgent,
    version: OwnedVersion,
    db: Annotated[Session, Depends(get_db)],
    model_factory: ModelFactory,
    payload: WorkflowDraftRequest | None = None,
) -> WorkflowDraftResponse:
    """Turn a confirmed spec into a draft workflow — user stories #7–#10.

    **The body is optional and so is the template.** No body, or a body with no
    `template_id`, means the platform composes a graph for this creator's own
    problem (ADR-0041) — which is what a creator who has just described a
    business is asking for, and what the previous behaviour of matching them to
    the nearest of eight catalog entries could not give them. A `template_id`
    means they picked a shape from the gallery on purpose.

    A 409 at any other stage, and the sentence says which one it is: an
    unconfirmed spec is a review screen the creator has not finished, while an
    existing draft is a canvas they are already working on. A `template_id`
    naming no template on offer is a 409 too, and for the same kind of reason —
    it is not a malformed request, it is a request about a template that is no
    longer there.

    What comes back is expected to be imperfect. A prompt the model left blank
    and an `action` nobody has bound yet both arrive as an ordinary draft with
    `problems` on it — the alternative, refusing to generate at all, leaves the
    creator drawing a graph from nothing.
    """
    try:
        drafted = await draft_workflow(
            db,
            version,
            model_factory=model_factory,
            template_id=payload.template_id if payload is not None else None,
        )
    except (SpecStageError, WorkflowStageError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return workflow_response(drafted)


@router.get("/{agent_id}/versions/{version_id}/workflow", response_model=WorkflowDraftResponse)
def read_my_versions_workflow(_agent: OwnedAgent, version: OwnedVersion) -> WorkflowDraftResponse:
    """The workflow this version holds — what the canvas loads, and what it re-loads.

    Owner-only, like every other version-scoped read: a workflow is the creator's
    own account of how their business runs, and a deployed agent is not a
    published source listing.
    """
    return workflow_response(version)


@router.put("/{agent_id}/versions/{version_id}/workflow", response_model=WorkflowDraftResponse)
def save_my_versions_workflow(
    _agent: OwnedAgent,
    version: OwnedVersion,
    workflow: WorkflowJSON,
    db: Annotated[Session, Depends(get_db)],
) -> WorkflowDraftResponse:
    """Store whatever is on the canvas right now — user story #15, AC 4.

    A save is not a checkpoint the creator has to earn. A node with no prompt, a
    port wired to nothing, a branch that never reaches `End`: all of them store,
    and all of them come back in `problems` for the canvas to ring in red. That
    is the entire distinction this endpoint exists to hold — refusing here is
    what would trap somebody halfway through wiring a branch.

    422 is still possible, and it is a different thing entirely: a body that is
    not a `WorkflowJSON` at all — a node type nobody defined, a budget over the
    system ceiling — is not a mid-edit state, it is a client that does not know
    the schema.

    **The response may name a different version than the URL did.** Editing a
    workflow that has already been confirmed forks (ADR-0022 §5), and
    `version_id` on the way back is where the creator's edits actually landed —
    the canvas follows it rather than assuming it is still on the row it opened.
    """
    try:
        saved = save_workflow(db, version, workflow)
    except WorkflowStageError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    response = workflow_response(saved)
    # PM playbook step 2: the verdict the canvas is about to draw, kept as data.
    record_spec_lint(
        agent_id=saved.agent_id,
        version_id=saved.id,
        problems=[problem.message for problem in response.problems],
        warnings=[warning.message for warning in response.warnings],
        moment="save",
    )
    return response


@router.post("/{agent_id}/versions/{version_id}/workflow/confirm", response_model=WorkflowDraftResponse)
def confirm_my_versions_workflow(
    _agent: OwnedAgent,
    version: OwnedVersion,
    db: Annotated[Session, Depends(get_db)],
) -> WorkflowDraftResponse:
    """`workflow_pending_confirmation -> workflow_confirmed` — user story #16, AC 5.

    The one place a graph is refused for being incomplete, and the only door to
    the status that makes it immutable.

    Both refusals are 409 and neither is a 422, because neither is about the
    request: a graph with an unwired port is a document the creator has to go and
    finish, and a version somebody already confirmed is a stage this call arrived
    too late for. The body carries `ensure_confirmable`'s own sentence, which
    names every problem at once — a creator fixing a graph one rejection at a
    time is a creator making four round trips to learn four things.
    """
    try:
        confirmed = confirm_workflow(db, version)
    except (WorkflowStageError, WorkflowGraphError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    # A confirm that got this far passed the gate — the score that matters is
    # that it happened, dated, per version (activation funnel's first gate).
    record_spec_lint(agent_id=confirmed.agent_id, version_id=confirmed.id, problems=[], warnings=[], moment="confirm")
    return workflow_response(confirmed)


@router.post("/{agent_id}/versions/{version_id}/workflow/refine", response_model=WorkflowRefinementResponse)
async def refine_my_versions_workflow(
    _agent: OwnedAgent,
    version: OwnedVersion,
    payload: WorkflowRefineRequest,
    model_factory: ModelFactory,
) -> WorkflowRefinementResponse:
    """Change this workflow with one sentence, returned for the canvas to apply.

    **Writes nothing.** The graph comes back proposed, and `PUT .../workflow` —
    the canvas's own save, unchanged — is what stores it. That split is not
    squeamishness about a model's output; it is what keeps one writer of
    `AgentVersion.workflow`. The canvas tracks "unsaved" by comparing the graph
    it holds against the last one the server acknowledged, so an endpoint that
    saved behind its back would leave it believing a stale document was the
    synced one, and the creator's next drag would either be dropped or arrive as
    a second save of a graph it never saw. It also makes undo free: putting the
    old graph back is an ordinary edit, because the new one was too.

    No `db` parameter, and that absence is the docstring above stated as a
    signature. Nothing here reads or writes a row that the version dependency has
    not already fetched.

    Gated to `WORKFLOW_EDITABLE` — every stage the canvas itself accepts edits
    at, which is the same set `PUT .../workflow` writes at. The rule is that a
    sentence may do exactly what the same screen's mouse may do and no more:
    chat that reached further would fork a version on a screen showing no way to
    have caused it, and chat that reached less far would be a text box refusing
    what the drag beside it just did. Beyond `workflow_pending_confirmation` the
    graph is immutable and the save that follows forks (ADR-0022 §5) — which is
    fine here precisely because this endpoint writes nothing: what comes back is
    a proposal, and the fork happens when the creator saves it.

    Three refusals, three statuses, chosen by what the reader does next. A
    request that did not say what to change is a 422 and no call was made. A
    version at the wrong stage is a 409 — the way out is a different version, not
    a retry. A meta-agent that could not write a valid graph in its allotted
    attempts is a 502 with a sentence saying the canvas is untouched, which it
    is, because nothing here ever writes.
    """
    refusal = wrong_stage(version, WORKFLOW_EDITABLE, action="sửa workflow bằng chat")
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refusal)
    try:
        refined = await refine_workflow(
            current=workflow_of(version),
            instruction=payload.instruction,
            model_factory=model_factory,
        )
    except BlankGraphInstructionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    except WorkflowStageError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except GraphRefinementError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    return WorkflowRefinementResponse(workflow=refined)


@router.post("/{agent_id}/versions/{version_id}/workbench/turn", response_model=WorkbenchTurnResponse)
async def workbench_turn(
    agent: OwnedAgent,
    version: OwnedVersion,
    payload: WorkbenchTurnRequest,
    model_factory: ModelFactory,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> WorkbenchTurnResponse:
    """One turn with the meta agent — ADR-0050's loop behind one door.

    The successor to `.../workflow/refine`, which stays alive as the fallback
    while this one earns its eval gate. Same contract where it matters: **writes
    no workflow**. What comes back is a proposal; the canvas applies it with
    undo and the ordinary save stores it. What this endpoint *does* write is the
    conversation itself — two `workbench_turns` rows per call — because memory
    across turns is the point (ADR-0049's first gap).

    Same stage gate as refine, same reasoning: a sentence may do exactly what
    the same screen's mouse may do.
    """
    refusal = wrong_stage(version, WORKFLOW_EDITABLE, action="làm việc với meta agent")
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refusal)
    message = payload.loi_nhan.strip()
    if not message:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail='Chưa rõ bạn muốn gì. Nói cụ thể giúp — ví dụ "thêm bước duyệt tay trước khi gửi mail".',
        )
    try:
        current = workflow_of(version)
    except WorkflowStageError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None

    request = TurnRequest(
        message=message,
        workflow=current,
        catalog=_workbench_catalog(db, user.id),
        history=_workbench_history(db, agent.id),
        viewing_run_id=payload.viewing_run_id,
        run_fetcher=lambda ma_run: _run_transcript(db, agent.id, ma_run),
    )
    try:
        # Metered here rather than inside `run_turn`, which is pure orchestration
        # on purpose — it opens no session and knows no settings, which is what
        # lets the whole loop be tested against a fake model with no
        # infrastructure. Wrapping the model on the way in is the seam that
        # already exists for exactly this: the loop binds tools and then takes up
        # to a dozen round trips on the platform's dearest model, and
        # `_MeteredChatModel.bind_tools` is what keeps the meter attached
        # through the binding.
        reply = await run_turn(
            metered(model_factory(meta_agent_model()), PlatformCallPurpose.WORKBENCH_TURN),
            request,
        )
    except Exception as exc:  # noqa: BLE001 — a provider outage must not read as a crash
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Meta agent không trả lời được lượt này ({exc}). Canvas không bị đụng tới — bạn thử lại.",
        ) from exc

    db.add(WorkbenchTurn(agent_id=agent.id, version_id=version.id, role=WorkbenchTurnRole.CREATOR, message=message))
    db.add(
        WorkbenchTurn(
            agent_id=agent.id,
            version_id=version.id,
            role=WorkbenchTurnRole.META_AGENT,
            message=reply.loi_nhan,
            question=reply.cau_hoi,
            workflow_after=reply.workflow.model_dump(mode="json") if reply.workflow is not None else None,
            steps_json=[{"tool": step.tool, "ke": step.ke, "ok": step.ok} for step in reply.cac_buoc],
        )
    )
    db.commit()

    return WorkbenchTurnResponse(
        loi_nhan=reply.loi_nhan,
        cau_hoi=reply.cau_hoi,
        workflow=reply.workflow,
        van_de=list(reply.van_de),
        cac_buoc=[WorkbenchStepResponse(tool=step.tool, ke=step.ke, ok=step.ok) for step in reply.cac_buoc],
    )


def _workbench_catalog(db: Session, creator_id: int) -> tuple[CatalogTool, ...]:
    """The creator's connections in the loop's own shape — `src.metaagent.catalog`'s, shared with the composer."""
    return creator_catalog(db, creator_id)


def _workbench_history(db: Session, agent_id: int) -> tuple[HistoryTurn, ...]:
    """The last stretch of this agent's conversation, oldest first."""
    rows = (
        db.query(WorkbenchTurn)
        .filter(WorkbenchTurn.agent_id == agent_id)
        .order_by(WorkbenchTurn.id.desc())
        .limit(HISTORY_TURNS * 2)
        .all()
    )
    return tuple(
        HistoryTurn(
            role=row.role.value,
            message=row.message if row.question is None else f"{row.message}\n[Đã hỏi lại: {row.question}]",
        )
        for row in reversed(rows)
    )


#: The most transcript characters `xem_run` hands the model. A run that read
#: three long documents would otherwise spend the whole turn budget on step one.
_TRANSCRIPT_CAP = 20_000


def _run_transcript(db: Session, agent_id: int, ma_run: str) -> str | None:
    """Run `ma_run`'s transcript as text, or `None` when it is not this agent's to read.

    `None` for a malformed id, a missing row and somebody else's run alike —
    the loop turns all three into the same sentence, and distinguishing them
    would tell a prompt-injected model which run ids exist.
    """
    try:
        run_id = int(ma_run)
    except ValueError:
        return None
    row = (
        db.query(SandboxRun)
        .join(AgentVersion, SandboxRun.version_id == AgentVersion.id)
        .filter(SandboxRun.id == run_id, AgentVersion.agent_id == agent_id)
        .first()
    )
    if row is None:
        return None
    header = f"Run #{row.id} — input: {row.input_message!r} — status: {row.status.value}"
    if row.error:
        header += f" — error: {row.error}"
    entries = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in row.transcript)
    text = f"{header}\n{entries}"
    return text if len(text) <= _TRANSCRIPT_CAP else text[:_TRANSCRIPT_CAP] + "\n[… transcript cắt bớt]"


@router.post(
    "/{agent_id}/versions/{version_id}/save-as-template",
    response_model=TemplateSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
def save_my_versions_workflow_as_template(
    _agent: OwnedAgent,
    version: OwnedVersion,
    payload: TemplateSubmissionRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> TemplateSubmissionResponse:
    """Submit this confirmed workflow as a candidate template — #53, ADR-0025 §7.

    `workflow_confirmed` only. A 409 at any other stage, for `draft_my_versions_workflow`'s
    reason: this is not about the request body, it is about a graph that has not
    passed the graph validator yet — a canvas the creator is still wiring is not
    a shape to hand to anyone else.

    Lands at `status: pending`, invisible to the Workflow Generator's selection
    pool until an admin approves it (criterion 3) — `submit_template` is the only
    writer of this table, so there is no second door that skips the queue.
    """
    try:
        row = submit_template(
            db,
            version,
            submitted_by=user.id,
            name=payload.name,
            description=payload.description,
            required_data_kinds=payload.required_data_kinds,
        )
    except TemplateSubmissionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return template_submission_response(row)


def template_submission_response(row: TemplateSubmission) -> TemplateSubmissionResponse:
    """One `TemplateSubmission` row, as both the submitting creator and an admin read it."""
    return TemplateSubmissionResponse(
        id=row.id,
        submitted_by=row.submitted_by,
        source_version_id=row.source_version_id,
        name=row.name,
        description=row.description,
        nodes=[NodeInstance.model_validate(node) for node in row.nodes],
        edges=[Edge.model_validate(edge) for edge in row.edges],
        start_node_id=row.start_node_id,
        required_data_kinds=[DataSourceKind(kind) for kind in row.required_data_kinds],
        status=row.status,
        rejection_reason=row.rejection_reason,
        created_at=row.created_at,
    )


@registry_router.get("/node-types", response_model=list[NodeTypeResponse])
def list_node_types(_user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> list[NodeTypeResponse]:
    """The eight node types the canvas builds its palette and its forms from — ADR-0021.

    Read straight off `NODE_REGISTRY`, in the order it declares, so the sidebar
    lists what the validator knows about and nothing else. A ninth node type
    appears here the moment the registry gains one, and — more to the point — a
    type the registry has *not* got can never appear at all.

    Signed in, but not scoped to an agent: the table is the same for everyone,
    and a creator who is not editing any particular workflow still needs it to
    open a canvas. Behind auth all the same, because the set of node types this
    platform supports is not something to hand to anonymous callers.

    One field is decorated on the way out: `agent_llm.model` gains the admin's
    allowlist as `enum` and `default_node_model` as `default`. The registry
    cannot know either — they are platform configuration, re-read per request
    (`read_config`'s own rule). Without this, a node dragged from the palette
    carried `model: ""` and a free-text box beside a rule nobody could see, and
    the creator learned the allowlist from a confirm refusal that said only
    "chưa điền: model". With it, the canvas draws a dropdown and a dropped node
    is valid before anybody touches it.
    """
    allowlist = list(read_config(db).model_allowlist)
    return [
        NodeTypeResponse(
            type=node_type,
            outputs=list(spec.outputs),
            config_schema=_with_model_choices(node_type, spec.config_schema, allowlist),
        )
        for node_type, spec in NODE_REGISTRY.items()
    ]


def _with_model_choices(node_type: NodeType, schema: dict[str, Any], allowlist: list[str]) -> dict[str, Any]:
    """`schema` with the `model` property of an `agent_llm` node narrowed to `allowlist`."""
    if node_type is not NodeType.AGENT_LLM or not allowlist:
        return schema
    properties = dict(schema.get("properties", {}))
    if "model" not in properties:
        return schema
    properties["model"] = {**properties["model"], "enum": allowlist, "default": default_node_model(allowlist)}
    return {**schema, "properties": properties}


@registry_router.get("/templates", response_model=list[WorkflowTemplateResponse])
def list_workflow_templates(
    _user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> list[WorkflowTemplateResponse]:
    """The template gallery a creator picks a starting shape from — ADR-0041.

    The same list the platform's own fallback selects out of (`approved_templates`),
    which is the point: before ADR-0041 this list existed only inside the
    Generator, and a creator who wanted a particular shape had no way to ask for
    it — they could only describe their business and hope the selector agreed.
    Publishing it is what turns the catalog from a routing table into a library.

    Both halves of the registry, built-in and approved creator submissions, and
    nothing at `pending`, `rejected` or `disabled` — one reader, so what the
    gallery offers and what `POST .../workflow` will accept as a `template_id`
    cannot come apart.

    Signed in but not scoped to a version, like the node-type table beside it: a
    creator choosing a shape is usually deciding *before* they open a canvas.
    """
    return [
        WorkflowTemplateResponse(
            id=template.id,
            name=template.name,
            description=template.description,
            required_data_kinds=list(template.required_data_kinds),
            node_count=len(template.nodes),
            source=template.source.value,
        )
        for template in approved_templates(db)
    ]


@registry_router.get("/tools", response_model=list[ToolCatalogEntryResponse])
def list_selectable_tools(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> list[ToolCatalogEntryResponse]:
    """Every connection an `MCP` node may be pointed at right now — #52, criterion 5.

    Both halves of the registry, read the same way and returned in one list: the
    code-defined built-ins first, then the creator-registered connections an
    admin has approved. A `pending` or `rejected` registration is simply not
    here (criterion 2), and one an admin has since disabled leaves on the very
    next request — the list is read fresh rather than cached, so "disabled"
    means disabled now rather than eventually.

    Scoped to the caller, unlike `list_node_types` beside it: the node palette is
    the same eight types for everyone, but `usage_scope` (ADR-0023 §1) makes a
    connection something a particular creator may or may not wire in.
    """
    built_in = [tool for tool in TOOL_DEFINITIONS.all() if tool.status is ToolStatus.APPROVED]
    registered = approved_registrations(db, for_creator=user.id)
    return [_catalog_entry(tool) for tool in [*built_in, *registered]]


#: Where a connection's pasted credential lands in the secret store. Namespaced
#: per owner so two creators registering the same server never collide, and
#: prefixed so these keys are distinguishable from anything an operator seeds.
_CONNECTION_SECRET_REF = "connection/{owner_id}/{slug}"


def _stored_credential(payload: ToolRegistrationRequest, *, db: Session, owner_id: int) -> ToolCredential:
    """The credential as the registry may hold it — the secret itself put away first.

    Agent Builder's dialog takes three auth shapes and so does this: no auth, a
    pasted access token, or custom headers. The last two arrive as *values*, and
    values are exactly what `ToolCredential` may not carry (ADR-0045) — so they
    go into `EncryptedDbSecretStore` here and what comes back is a reference.

    A caller that already has a `secret_ref` (an operator seeded the store) is
    left alone: `secret_ref` wins, because it names a key somebody chose
    deliberately and re-storing under a generated name would orphan it.
    """
    given = payload.credential
    if given.secret_ref:
        return ToolCredential(
            kind=given.kind, provider=given.provider, scopes=tuple(given.scopes), secret_ref=given.secret_ref
        )
    value = json.dumps(given.headers, ensure_ascii=False) if given.headers else given.secret
    if not value:
        return ToolCredential(kind=given.kind, provider=given.provider, scopes=tuple(given.scopes))
    slug = "".join(character if character.isalnum() else "-" for character in payload.name.lower())[:60]
    ref = _CONNECTION_SECRET_REF.format(owner_id=owner_id, slug=slug or "connection")
    EncryptedDbSecretStore(db=db).put(ref, value)
    return ToolCredential(kind=given.kind, provider=given.provider, scopes=tuple(given.scopes), secret_ref=ref)


@registry_router.post("/tools", response_model=ToolRegistrationResponse, status_code=status.HTTP_201_CREATED)
def register_my_mcp_connection(
    payload: ToolRegistrationRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    rpc: Annotated[McpTransport, Depends(get_mcp_transport)],
) -> ToolRegistrationResponse:
    """Register an MCP connection — the system introspects it, an admin decides on it.

    Criterion 1: what comes back already lists `actions[]` with a computed
    `risk_level` per action, and none of it was in the request. The creator
    submitted a URL and a credential reference.

    502 when the server cannot be read, rather than 400 or 422: the request was
    well-formed and the fault is upstream — the creator's own MCP server did not
    answer the way an MCP server answers. The message is the server's, because
    the person who can fix it is the person reading it.

    Lands at `status: pending` (criterion 2), and `register_tool` is the only
    writer of this table — there is no second door that skips the queue.
    """
    try:
        row = register_tool(
            db,
            submitted_by=user.id,
            name=payload.name,
            server_url=payload.server_url,
            description=payload.description,
            credential=_stored_credential(payload, db=db, owner_id=user.id),
            usage_scope=payload.usage_scope,
            mock_response_examples=payload.mock_response_examples,
            allowed_actions=payload.allowed_actions,
            rpc=rpc,
        )
    except ToolRegistrationError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    return tool_registration_response(row)


@registry_router.get("/tools/registrations", response_model=list[ToolRegistrationResponse])
def list_my_tool_registrations(
    user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> list[ToolRegistrationResponse]:
    """Where the caller's own registrations stand, and which actions still want a mock."""
    return [tool_registration_response(row) for row in registrations_of(db, user.id)]


def get_own_registration(
    registration_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> ToolRegistration:
    """The registration at `registration_id` if the caller submitted it, otherwise 404.

    404 rather than 403 for `get_owned_agent`'s reason: "this exists but is not
    yours" leaks that somebody else registered a connection with that id.
    """
    row = db.get(ToolRegistration, registration_id)
    if row is None or row.submitted_by != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy kết nối")
    return row


OwnRegistration = Annotated[ToolRegistration, Depends(get_own_registration)]


@registry_router.put("/tools/registrations/{registration_id}/mocks", response_model=ToolRegistrationResponse)
def set_my_registrations_mock_responses(
    registration: OwnRegistration, payload: ToolMockResponsesRequest, db: Annotated[Session, Depends(get_db)]
) -> ToolRegistrationResponse:
    """Supply what each action *would* have answered in a sandbox — ADR-0023 §4.

    Its own endpoint rather than a field of the registration body, because the
    creator does not know which actions need one until introspection has told
    them: the natural order is register, read `actions[]`, then fill the gaps
    the response marked `needs_mock`.

    409 once the connection has been decided on — from `approved` onward the
    mocks are part of what the admin approved, and a creator who could rewrite
    them afterwards could change what every rehearsal of every workflow using
    this connection reports, with nobody reviewing the new answer.
    """
    try:
        updated = set_mock_responses(db, registration, examples=payload.examples)
    except ToolRegistrationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return tool_registration_response(updated)


@registry_router.put("/tools/registrations/{registration_id}/allowed-actions", response_model=ToolRegistrationResponse)
def set_my_registrations_allowed_actions(
    registration: OwnRegistration, payload: ToolAllowedActionsRequest, db: Annotated[Session, Depends(get_db)]
) -> ToolRegistrationResponse:
    """Narrow this connection to a subset of what its server reported.

    The sibling of the mocks endpoint in every way that matters, including why
    it is separate: a creator does not know which actions exist until
    introspection has answered, so narrowing is a second step by construction.

    409 once the connection has been decided on. From `approved` onward the
    allowlist is part of what the admin approved, and a creator who could widen
    it afterwards could put an unreviewed action into live workflows — the one
    thing the review queue exists to prevent.
    """
    try:
        updated = set_allowed_actions(db, registration, names=payload.actions)
    except ToolRegistrationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return tool_registration_response(updated)


def tool_registration_response(row: ToolRegistration) -> ToolRegistrationResponse:
    """One `ToolRegistration` row, as both its creator and a reviewing admin read it."""
    return ToolRegistrationResponse(
        id=row.id,
        tool_id=registration_id_of(row),
        submitted_by=row.submitted_by,
        name=row.name,
        server_url=row.server_url,
        description=row.description,
        credential=ToolCredentialResponse(
            kind=row.credential_kind,
            provider=row.credential_provider,
            scopes=list(row.credential_scopes),
            secret_ref=row.credential_secret_ref,
        ),
        usage_scope=row.usage_scope,
        allowed_actions=list(row.allowed_actions),
        actions=[
            ToolActionReviewResponse(
                **_action_fields(action),
                overridden=action.overridden,
                needs_mock=action.needs_mock,
                mock_response_example=action.mock_response,
            )
            for action in reviewed_actions(row)
        ],
        status=row.status,
        rejection_reason=row.rejection_reason,
        created_at=row.created_at,
    )


def _catalog_entry(tool: ToolDefinition) -> ToolCatalogEntryResponse:
    """One registry entry as the canvas's tool picker reads it, whichever half it came from."""
    return ToolCatalogEntryResponse(
        id=tool.id,
        name=tool.name,
        family=tool.family,
        source=tool.source,
        usage_scope=tool.usage_scope,
        actions=[ToolActionResponse(**_action_fields(action)) for action in tool.actions],
    )


def _action_fields(action: ToolAction | ReviewedAction) -> dict[str, Any]:
    """The eight fields every action carries on the wire, from either half of the registry.

    One reader for both, because `ToolActionReviewResponse` extends
    `ToolActionResponse` — two hand-copied field lists would be free to disagree
    about, say, what `sandbox_behavior` an action has, and the catalogue and the
    review queue would then be describing the same action differently.

    `ToolAction` and `ReviewedAction` are different classes on purpose (an action
    still missing its mock cannot be a `ToolAction` at all — ADR-0023 §4), but
    they agree on exactly these eight names, which is what makes one reader
    honest rather than a coincidence.
    """
    return {
        "name": action.name,
        "description": action.description,
        "input_schema": action.input_schema,
        "output_schema": action.output_schema,
        "read_only": action.read_only,
        "risk_level": action.risk_level,
        "sandbox_behavior": action.sandbox_behavior,
        "requires_human_approval": action.requires_human_approval,
    }


def workflow_response(version: AgentVersion) -> WorkflowDraftResponse:
    """One version's workflow as the wire sees it, with its problems counted here.

    Re-validated on the way out by `workflow_of`, which is also what answers a
    caller that asked a version for a document it never had — with a sentence
    rather than a 500.
    """
    try:
        workflow = workflow_of(version)
    except WorkflowStageError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    return WorkflowDraftResponse(
        agent_id=version.agent_id,
        version_id=version.id,
        version_number=version_number(version),
        status=version.status,
        workflow=workflow,
        problems=_reported(graph_problems(workflow)),
        warnings=_reported(graph_warnings(workflow)),
        created_at=version.created_at,
    )


def _reported(problems: Iterable[GraphProblem]) -> list[WorkflowProblemResponse]:
    """The validator's findings as the wire carries them, whichever list they are in."""
    return [
        WorkflowProblemResponse(message=problem.message, node_id=problem.node_id, edge_id=problem.edge_id)
        for problem in problems
    ]
