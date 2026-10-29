"""The approval flow, both halves of it — see issue #14 / ADR-0012.

Two routers in one module, and they belong together: one is where a creator asks
to be reviewed, the other is where an admin reviews. Splitting them by audience
would put the queue in one file and the only thing that fills it in another, and
the rule they share — *nothing enters the queue except through
`request_approval`* — would be stated in neither.

Everything an admin does here deliberately crosses ownership, the same way the
cost board (#11) does, and for the same reason: deciding whether another
creator's agent may run for real is a governance question, and it belongs to
whoever answers for the platform. `src.api.agent_routes` gives admins no
read-through precisely so that this crossing is the audited one.

No gate is re-checked at this layer. `src.agents.versions` refuses an approval
request with no sandbox run behind it, refuses one below the eval bar, refuses a
rejection with no reason, and refuses an override with none — this module turns
each refusal into an HTTP status and carries the sentence through untouched. Two
places stating one rule is how a screen ends up showing a green light over a
button that refuses (ADR-0011 §5).
"""

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.agents.versions import VersionLifecycleError, request_approval, submission_blocker
from src.api.agent_routes import (
    OwnedVersion,
    flow_diagram_response,
    requested_tool_responses,
    version_costs_response,
)
from src.api.eval_routes import eval_run_response, suite_change_response
from src.approvals.decisions import (
    approve_version,
    history_for,
    override_eval_bar,
    reject_version,
    score_of,
)
from src.approvals.dossier import Dossier, RequestedAction, build_dossier
from src.approvals.queue import DecidedEntry, QueueEntry, approval_queue, decided_versions
from src.auth.dependencies import AdminUser
from src.db.base import get_db
from src.db.models import AgentVersion, ApprovalDecision, User
from src.evals.results import Breakdown, Score
from src.evals.suite_changes import SuiteChanges
from src.models.schemas import (
    ApprovalDecidedEntryResponse,
    ApprovalDecisionResponse,
    ApprovalDossierResponse,
    ApprovalQueueEntryResponse,
    ApprovalReasonRequest,
    EvalBreakdownResponse,
    EvalCaseResponse,
    EvalScoreResponse,
    EvalTallyResponse,
    RequestedActionResponse,
    SandboxRunResponse,
    SuiteChangesResponse,
    VersionApprovalResponse,
)

router = APIRouter(prefix="/admin/approvals", tags=["approvals"])

#: The creator's half. Under `/agents` because it is addressed by agent and
#: version like everything else a creator owns, and scoped by `OwnedVersion` for
#: the same reason — a creator may only ever submit their own work.
creator_router = APIRouter(prefix="/agents", tags=["approvals"])


def get_version_under_review(
    version_id: int, _admin: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> AgentVersion:
    """Any version at all, by id — this is the endpoint where an admin crosses ownership.

    404 rather than 403 for a version that does not exist, matching
    `get_owned_agent`. There is no ownership check to fail here: reaching across
    creators is what these endpoints are for, and `AdminUser` is what makes it a
    decision rather than a leak.
    """
    version = db.get(AgentVersion, version_id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy version")
    return version


#: Declare this type and both halves come with it: the caller is an admin, and
#: the version in the path exists.
VersionUnderReview = Annotated[AgentVersion, Depends(get_version_under_review)]


def _refused(exc: VersionLifecycleError) -> HTTPException:
    """A lifecycle refusal on the wire: 409, carrying the reason verbatim.

    409 and not 422, because nothing is wrong with the request — the version is
    in a state where what was asked for cannot happen. The sentence is the
    valuable part, so it is passed through rather than replaced with a
    status-shaped one: "chưa chạy sandbox" and "còn thiếu 2 case nữa" are
    different next moves, and a client that had to guess would offer neither.
    """
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("", response_model=list[ApprovalQueueEntryResponse])
def read_approval_queue(_admin: AdminUser, db: Annotated[Session, Depends(get_db)]) -> list[ApprovalQueueEntryResponse]:
    """Every version waiting on a person, oldest first — see ADR-0012 §4.

    Includes versions the eval bar is holding shut, marked `blocked_by_eval`.
    They are not asking to be approved and cannot be, but the override that #14
    gives an admin exists for exactly them — and an admin who cannot see one has
    no way to reach it.
    """
    return [_queue_entry(entry) for entry in approval_queue(db)]


# Declared **above** `/{version_id}`, and it has to be: Starlette matches routes
# in declaration order, `decided` matches the `{version_id}` pattern, and FastAPI
# would then refuse it as a non-integer id. A 422 on a path that exists is the
# kind of bug that gets diagnosed as a permissions problem.
@router.get("/decided", response_model=list[ApprovalDecidedEntryResponse])
def read_decided_reviews(_admin: AdminUser, db: Annotated[Session, Depends(get_db)]) -> list[ApprovalDecidedEntryResponse]:
    """Reviews that are over, most recently settled first — see ADR-0064.

    The queue above answers "what is waiting on me"; nothing answered "what did
    we decide, and why". A dossier used to be reachable only *from* the queue, so
    approving a version made its evidence unreachable — the platform's whole
    pitch is that a person read a dossier and decided, and the moment they did,
    nobody could ever see what they read. This list is the way back in.

    Same `AdminUser` as every other endpoint in this module, and deliberately no
    wider: whoever could see the queue can see what left it, and nobody else.
    """
    return [_decided_entry(entry) for entry in decided_versions(db)]


@router.get("/{version_id}", response_model=ApprovalDossierResponse)
def read_approval_dossier(
    version: VersionUnderReview, db: Annotated[Session, Depends(get_db)]
) -> ApprovalDossierResponse:
    """Everything about one version, gathered so that deciding needs nothing else.

    A stored spec that no longer validates is a 422 rather than a partial
    dossier. Rendering the rest of it would put an admin in front of an approve
    button for an agent nobody can describe — worse here than at run time, where
    the same check only costs a failed request (ADR-0003 §7).

    **Any status, including a decided one**, and that was already true before
    ADR-0064 — nothing here has ever filtered by status. What was missing was a
    way to *reach* a decided version's id, which `GET /decided` above now is.
    Read-only-ness is not enforced by withholding this payload: it is the
    decision endpoints that refuse, and `status` on the way out is what tells a
    screen not to offer buttons that would be refused (ADR-0011 §5).
    """
    try:
        dossier = build_dossier(db, version)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Version không hợp lệ: {exc}",
        ) from None
    return _dossier_response(db, dossier)


@router.post("/{version_id}/approve", response_model=VersionApprovalResponse)
def approve_version_under_review(
    version: VersionUnderReview, admin: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> VersionApprovalResponse:
    """Let this version run for real. Only from `pending_approval`.

    Approving does not deploy it. `Agent.deployed_version_id` is #15's to move,
    and keeping the two apart is what lets an approved version wait until someone
    chooses the moment (ADR-0005 §3).
    """
    try:
        approve_version(db, version, admin=admin)
    except VersionLifecycleError as exc:
        raise _refused(exc) from None
    return _approval_response(db, version)


@router.post("/{version_id}/reject", response_model=VersionApprovalResponse)
def reject_version_under_review(
    version: VersionUnderReview,
    admin: AdminUser,
    payload: ApprovalReasonRequest,
    db: Annotated[Session, Depends(get_db)],
) -> VersionApprovalResponse:
    """Send this version back, with a reason its creator will read.

    The reason is mandatory and it is the whole endpoint: a creator told only
    "rejected" has to guess what to change, and will resubmit something that gets
    rejected again.
    """
    try:
        reject_version(db, version, admin=admin, reason=payload.reason)
    except VersionLifecycleError as exc:
        raise _refused(exc) from None
    return _approval_response(db, version)


@router.post("/{version_id}/eval-override", response_model=VersionApprovalResponse)
def override_eval_bar_for_version(
    version: VersionUnderReview,
    admin: AdminUser,
    payload: ApprovalReasonRequest,
    db: Annotated[Session, Depends(get_db)],
) -> VersionApprovalResponse:
    """Waive the eval threshold so this version can enter the queue. Reason mandatory.

    The safety valve #14 asks for: the eval judge is an LLM and it wobbles, and
    without a way past, one unlucky grading dead-ends the whole flow.

    It does not approve anything. The version lands in the queue and still has to
    be read and decided on — waiving the machine's verdict is not the same act as
    accepting the agent, and a valve that did both would be a way to skip the
    review rather than a way to reach it.
    """
    try:
        override_eval_bar(db, version, admin=admin, reason=payload.reason)
    except VersionLifecycleError as exc:
        raise _refused(exc) from None
    return _approval_response(db, version)


@creator_router.post("/{agent_id}/versions/{version_id}/approval-request", response_model=VersionApprovalResponse)
def submit_my_version_for_approval(
    version: OwnedVersion, db: Annotated[Session, Depends(get_db)]
) -> VersionApprovalResponse:
    """Ask an admin to review one of the caller's own versions.

    The only door into the queue, which is why every gate lives behind it rather
    than here: no sandbox run is a 409 saying so, and an eval score below the bar
    is a 409 saying how far short and what to do next (#13). Both refusals are
    the gate's own words — this endpoint adds nothing to them and takes nothing
    away.
    """
    try:
        request_approval(db, version)
    except VersionLifecycleError as exc:
        raise _refused(exc) from None
    return _approval_response(db, version)


@creator_router.get("/{agent_id}/versions/{version_id}/approval", response_model=VersionApprovalResponse)
def read_my_versions_approval(
    version: OwnedVersion, db: Annotated[Session, Depends(get_db)]
) -> VersionApprovalResponse:
    """Where one of the caller's own versions stands, and every decision made about it.

    The creator's half of the audit trail. They see the reasons, and the admin
    who gave them, because a rejection nobody can read is a rejection nobody can
    act on — but they reach it through `OwnedVersion`, so it is only ever their
    own version's history.
    """
    return _approval_response(db, version)


def _approval_response(db: Session, version: AgentVersion) -> VersionApprovalResponse:
    """One version's standing, for whoever is entitled to ask.

    The same shape for the creator and for the admin who just decided, so the
    screen that submitted and the screen that approved both re-render from one
    payload rather than from two subtly different ones.
    """
    return VersionApprovalResponse(
        version_id=version.id,
        status=version.status,
        # `submission_blocker` and not `blocked_reason`: this field answers "what
        # is stopping *me* submitting", and for a draft that is the missing trial
        # run, not the missing eval. Quoting the eval sentence there would send a
        # creator off to write test cases for a version nobody has watched run.
        blocked_reason=submission_blocker(db, version),
        rejection_reason=version.rejection_reason,
        decisions=[_decision(db, decision) for decision in history_for(db, version.id)],
    )


def _decision(db: Session, decision: ApprovalDecision) -> ApprovalDecisionResponse:
    """One audit entry, with the person behind it named.

    The admin is looked up rather than joined in the query: this list is one
    version's history, which is a handful of rows, and the alternative is a join
    on every read of a list that is almost always short.
    """
    admin = db.get(User, decision.admin_id)
    return ApprovalDecisionResponse(
        id=decision.id,
        kind=decision.kind,
        reason=decision.reason,
        admin_email=admin.email if admin is not None else "",
        eval_score=_score(score_of(decision)),
        created_at=decision.created_at,
    )


def _score(score: Score | None) -> EvalScoreResponse | None:
    """A score on the wire, never without the bar it is measured against."""
    if score is None:
        return None
    return EvalScoreResponse(
        passed_count=score.passed_count,
        total_count=score.total_count,
        pass_rate=score.pass_rate,
        threshold=score.threshold,
        meets_threshold=score.meets_threshold,
    )


def _queue_entry(entry: QueueEntry) -> ApprovalQueueEntryResponse:
    return ApprovalQueueEntryResponse(
        version_id=entry.version_id,
        agent_id=entry.agent_id,
        agent_name=entry.agent_name,
        owner_email=entry.owner_email,
        state=entry.state,
        score=_score(entry.score),
        blocked_reason=entry.blocked_reason,
        high_risk_tools=list(entry.high_risk_tools),
        created_at=entry.created_at,
    )


def _decided_entry(entry: DecidedEntry) -> ApprovalDecidedEntryResponse:
    """One settled review on the wire, nulls and all.

    Nothing is filled in on the way out. `decided_by` and `decided_at` are `None`
    for a version that reached its status without an `ApprovalDecision` behind
    it, and they stay `None` here rather than falling back to the version's own
    `created_at` — the screen has to be able to say "no record of who decided
    this", and it cannot if the payload has quietly answered for it.
    """
    return ApprovalDecidedEntryResponse(
        version_id=entry.version_id,
        agent_id=entry.agent_id,
        agent_name=entry.agent_name,
        owner_email=entry.owner_email,
        status=entry.status,
        decided_by=entry.decided_by,
        decided_at=entry.decided_at,
        note=entry.note,
        eval_override_reason=entry.eval_override_reason,
        score=_score(entry.score),
        created_at=entry.created_at,
    )


def _dossier_response(db: Session, dossier: Dossier) -> ApprovalDossierResponse:
    """The dossier on the wire, whichever generation of documents it gathered.

    The eval run and the cost figures are rendered by the very functions the
    creator's own screens use (`eval_run_response`, `version_costs_response`), so
    a number an admin reads here is the number its creator read there. Two
    renderings of one figure is how the two sides of a review start disagreeing
    about what was measured.

    The V1 half is built only when the version has a pipeline, and the V2 half
    only when it has a graph (#49). Neither is defaulted into the other's shape:
    an empty `flow` beside a V2 version would be a diagram of an agent that has
    none, on the one screen whose whole job is to show what is really there.
    """
    return ApprovalDossierResponse(
        version_id=dossier.version.id,
        agent_id=dossier.agent.id,
        agent_name=dossier.agent.name,
        owner_email=dossier.owner.email,
        status=dossier.version.status,
        created_at=dossier.version.created_at,
        spec=dossier.spec,
        flow=flow_diagram_response(dossier.flow) if dossier.flow is not None else None,
        tools=requested_tool_responses(dossier.tools),
        allowed_domains=list(dossier.allowed_domains),
        solution_spec=dossier.solution_spec,
        workflow=dossier.workflow,
        actions=requested_action_responses(dossier.actions),
        budget=dossier.budget,
        sandbox_run=(
            SandboxRunResponse.model_validate(dossier.sandbox_run) if dossier.sandbox_run is not None else None
        ),
        eval_run=eval_run_response(db, dossier.eval_run) if dossier.eval_run is not None else None,
        eval_breakdown=_breakdown(dossier.eval_breakdown),
        blocked_reason=dossier.blocked_reason,
        # Always built, even for a V2 version whose two projections are absent:
        # its trial run spent real money, and the measurement is the one figure
        # here that is a fact rather than a forecast (ADR-0009 §4).
        costs=version_costs_response(
            dossier.version.id,
            estimate=dossier.estimate,
            worst_case=dossier.worst_case,
            measured=dossier.measured,
        ),
        rejection_reason=dossier.version.rejection_reason,
        decisions=[_decision(db, decision) for decision in dossier.decisions],
        suite_changes=_suite_changes(db, dossier.suite_changes),
    )


def _suite_changes(db: Session, changes: SuiteChanges | None) -> SuiteChangesResponse | None:
    """ADR-0069 §6's block, rendered with the same history entries the creator's own screen shows."""
    if changes is None:
        return None
    return SuiteChangesResponse(
        baseline_version_id=changes.baseline_version_id,
        retired=[EvalCaseResponse.model_validate(case) for case in changes.retired],
        retired_failing_count=changes.retired_failing,
        creator_changes=[suite_change_response(db, change) for change in changes.creator_changes],
        new_cases=[EvalCaseResponse.model_validate(case) for case in changes.new_cases],
    )


def requested_action_responses(actions: Sequence[RequestedAction]) -> list[RequestedActionResponse]:
    """The V2 permission list on the wire, in the order `requested_actions` put it in.

    The order is not re-established here, for `requested_tool_responses`' reason:
    where attention should go is a judgement `src.approvals.dossier` makes once,
    and a second sort at the edge would be a second opinion about it.
    """
    return [
        RequestedActionResponse(
            name=action.name,
            tool_id=action.tool_id,
            action=action.action,
            tool_name=action.tool_name,
            description=action.description,
            risk=action.risk,
            requires_human_approval=action.requires_human_approval,
            scopes=list(action.scopes),
            requested_by_node_ids=list(action.requested_by_node_ids),
        )
        for action in actions
    ]


def _breakdown(breakdown: Breakdown | None) -> EvalBreakdownResponse | None:
    """Who graded what, or `None` when nothing has been graded at all.

    `None` and not two zeroed tallies, for the reason every other absent figure
    on this payload is `None`: "no case was graded by a rule" and "no eval has
    ever run" are different facts, and a reviewer reading the second as the first
    would take a version nobody has tested for one that broke no rules.
    """
    if breakdown is None:
        return None
    return EvalBreakdownResponse(
        rule_based=EvalTallyResponse(
            passed_count=breakdown.rule_based.passed_count,
            total_count=breakdown.rule_based.total_count,
        ),
        judged=EvalTallyResponse(
            passed_count=breakdown.judged.passed_count,
            total_count=breakdown.judged.total_count,
        ),
        safety_failures=breakdown.safety_failures,
    )
