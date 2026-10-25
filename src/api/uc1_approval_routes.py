"""UC1-39 (#127) + UC1-17 (#133) + UC1-18 (#134) + UC1-16 (#132) — the
approval/deploy surface under `/agents/{id}/...` plus `/approvals/queue`,
spec.md §19.

`submit_version_for_approval` wires FR-38's gate ("không được xin duyệt nếu
chưa có ít nhất một lượt chat thử") and FR-33's gate (golden-set
`min_pass_rate`) into the one HTTP door that fires `draft -> pending_approval`
(`src.agentspec.lifecycle.submit_for_approval`) — both read *existing* records
(`sandbox_run_count`, `latest_eval_run`) rather than running anything
themselves, the same "the gate checks, it does not trigger" shape both
conditions share. `run_golden_set` is what produces the record the second one
reads; `approve_version` is HITL #2's quorum, accumulated one admin at a time
via `uc1_version_approvals` until `authority.approval_quorum` is met
(`src.agentspec.lifecycle.approve`); `deploy_version`/`suspend_agent` are
FR-34/FR-35's Postgres role lifecycle (UC1-18 / #134).

`approval_queue`/`diff_versions`/`version_dossier` (UC1-16 / #132) are HITL
#2's own reading surface — everything an `agent_admin` needs to decide,
gathered rather than assembled by a screen making several requests, the same
"one hồ sơ, not five screens" argument #14/ADR-0012 already made for V2's own
queue (`src.approvals`, a different pipeline entirely — see that package's own
docstrings for why it does not apply here).

`form_confirmed` stays a stand-in — UC1-04's `POST .../confirm` (E2) is a
different ticket's territory and still not built; see
`_FORM_CONFIRMED_STAND_IN`.
"""

from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agentspec.diff import diff_spec
from src.agentspec.lifecycle import (
    InvalidTransitionError,
    SuspensionReason,
    approve,
    deploy,
    rollback,
    submit_for_approval,
    suspend,
)
from src.agentspec.network_policy import revoke_network_policy, write_network_policy
from src.agentspec.roles import AgentRoleGrantError, missing_connection_reason, provision_agent_role, suspend_agent_role
from src.agentspec.spec import AgentSpec, Scope, SpecStatus
from src.agentspec.store import (
    SpecVersionNotFoundError,
    get_deployed_spec_version,
    get_spec_version,
    update_spec_version_status,
)
from src.auth.dependencies import AdminUser
from src.compiler.tools import default_tool_implementations
from src.config import get_settings
from src.db.base import get_db
from src.db.models import UC1AgentSpecVersion, UC1VersionApproval, User
from src.evals.uc1_eval_runs import latest_eval_run, record_eval_run
from src.evals.uc1_golden_set_runner import GoldenSetRunDeps, run_uc1_golden_set
from src.models.schemas import UtcTimestamp
from src.policy.audit import audit_sink
from src.policy.pep import PEPClient, get_pep_client
from src.services.llm import ChatModelFactory, get_chat_model_factory
from src.services.vault_secret_store import VaultKvStore, get_vault_store, resolve_credential_via
from src.workflow.uc1_connections import verified_objects_for_spec
from src.workflow.uc1_sandbox_runs import list_sandbox_runs, sandbox_run_count

router = APIRouter(prefix="/agents", tags=["uc1-approval"])


def _network_policy_dir() -> Path:
    """UC1-35 (#147): where every deployed agent's compose network override
    lands — `settings.network_policy_dir`, read fresh per call the same way
    `psycopg.connect(get_settings().database_url)` already is a few lines
    below each of these handlers, not cached at import time.
    """
    return Path(get_settings().network_policy_dir)


def _network_policy_host_aliases() -> dict[str, str]:
    """UC1-35 (#147): which real compose service backs each `allowed_hosts`
    entry in *this* deployment — `settings.network_policy_host_aliases`,
    read fresh per call for the same reason `_network_policy_dir` is.
    """
    return get_settings().network_policy_host_aliases


#: A separate prefix — `GET /approvals/queue` names the collection, not one
#: agent's own sub-resource, and V2's `src.api.approval_routes` already sits
#: at `/admin/approvals` rather than this literal path, so there is nothing
#: this collides with.
queue_router = APIRouter(prefix="/approvals", tags=["uc1-approval"])

#: `form_confirmed` (UC1-04's `POST .../confirm`, E2 — not this batch's ticket)
#: stays the honest, most permissive stand-in — see module docstring. Named
#: rather than inlined so the day that endpoint lands, grepping this name
#: finds the one call-site argument it replaces.
_FORM_CONFIRMED_STAND_IN = True


class SubmitForApprovalProblemResponse(BaseModel):
    """One `Problem` from `InvalidTransitionError`, addressed to whoever reads the 409."""

    code: str
    path: str
    message: str


class SubmitForApprovalResponse(BaseModel):
    status: str


@router.post("/{agent_id}/versions/{version}/submit-for-approval", response_model=SubmitForApprovalResponse)
def submit_version_for_approval(
    agent_id: str, version: int, user: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> SubmitForApprovalResponse:
    """FR-38/AC-11: refuse `draft -> pending_approval` with a structured error unless
    this version has at least one sandbox run — §13's own "thiếu bất kỳ điều kiện nào
    thì ở lại draft, không có nhánh lỗi riêng": the response lists every failing
    condition in one 409, not just the first.
    """
    try:
        spec = get_spec_version(db, agent_id, version)
    except SpecVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    run_count = sandbox_run_count(db, agent_id, version)
    eval_run = latest_eval_run(db, agent_id, version)
    # `0.0` when nothing has ever graded this version — the same "absent means
    # fail" `submit_for_approval` already applies to `sandbox_run_count`, read
    # here rather than special-cased: a version nobody ran `golden-set/run` on
    # is not exempt from FR-33, it simply has not cleared it yet.
    pass_rate = eval_run.pass_rate if eval_run is not None else 0.0
    try:
        transitioned = submit_for_approval(
            spec,
            form_confirmed=_FORM_CONFIRMED_STAND_IN,
            sandbox_run_count=run_count,
            deepeval_pass_rate=pass_rate,
        )
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=[
                SubmitForApprovalProblemResponse(
                    code=problem.code, path=problem.path, message=problem.message
                ).model_dump()
                for problem in exc.problems
            ],
        ) from None

    update_spec_version_status(db, transitioned)
    return SubmitForApprovalResponse(status=transitioned.metadata.status.value)


class DeployResponse(BaseModel):
    status: str


@router.post("/{agent_id}/versions/{version}/deploy", response_model=DeployResponse)
def deploy_version(
    agent_id: str, version: int, user: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> DeployResponse:
    """UC1-18 (#134), FR-34: `approved -> deployed`, then provision this agent's
    own Postgres role from its `connections[]` — the two happen together
    because a deployed version with no role yet is a version the runtime
    cannot serve traffic for at all.

    Retires whichever version was serving before, the same way
    `rollback_version` below already does. "Mỗi agent chỉ có tối đa một version
    chạy thật" (§13, and what the Lifecycle tab promises the admin) is an
    invariant nothing enforced on this path: deploying v2 over a `deployed` v1
    left *both* rows deployed, and `get_deployed_spec_version`'s own
    `scalar_one_or_none` then raised `MultipleResultsFound` rather than
    returning either — taking down the production chat door and the kill switch
    for that agent, both of which resolve their target through it.
    """
    try:
        spec = get_spec_version(db, agent_id, version)
    except SpecVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    try:
        transitioned = deploy(spec)
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=[
                SubmitForApprovalProblemResponse(
                    code=problem.code, path=problem.path, message=problem.message
                ).model_dump()
                for problem in exc.problems
            ],
        ) from None

    # Ahead of every state change below, because it reads nothing but the
    # document: a spec that could not serve a single question once deployed is
    # refused while the outgoing version is still untouched and serving.
    gap = missing_connection_reason(transitioned)
    if gap is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=gap)

    # Before the new role is provisioned, so a failure here leaves the old
    # version serving rather than neither of them.
    outgoing = get_deployed_spec_version(db, agent_id)
    if outgoing is not None and outgoing.metadata.version != transitioned.metadata.version:
        update_spec_version_status(db, suspend(outgoing, reason=SuspensionReason.SUPERSEDED))

    with psycopg.connect(get_settings().database_url) as conn:
        try:
            provision_agent_role(conn, transitioned)
        except AgentRoleGrantError as exc:
            # `update_spec_version_status` below never runs, so the version does
            # not reach `deployed` — the grants are real but unusable, which is
            # an infrastructure fault, not a bad request.
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from None
    write_network_policy(transitioned, _network_policy_dir(), host_aliases=_network_policy_host_aliases())
    update_spec_version_status(db, transitioned)
    return DeployResponse(status=transitioned.metadata.status.value)


class SuspendResponse(BaseModel):
    status: str
    version: int


@router.post("/{agent_id}/suspend", response_model=SuspendResponse)
def suspend_agent(agent_id: str, user: AdminUser, db: Annotated[Session, Depends(get_db)]) -> SuspendResponse:
    """UC1-18 (#134), FR-35: the kill switch. Targets whichever version of
    `agent_id` is currently `deployed` — there is at most one — and revokes
    its Postgres role's privileges before this call returns, which is what
    makes AC-18's "< 1 giây đo bằng đồng hồ thực" a fact about this endpoint
    rather than about a background job it kicked off.
    """
    spec = get_deployed_spec_version(db, agent_id)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"agent {agent_id!r} không có version nào đang deployed"
        )

    try:
        transitioned = suspend(spec, reason=SuspensionReason.ADMIN_KILL_SWITCH)
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=[
                SubmitForApprovalProblemResponse(
                    code=problem.code, path=problem.path, message=problem.message
                ).model_dump()
                for problem in exc.problems
            ],
        ) from None

    with psycopg.connect(get_settings().database_url) as conn:
        suspend_agent_role(conn, agent_id)
    revoke_network_policy(agent_id, _network_policy_dir())
    update_spec_version_status(db, transitioned)
    return SuspendResponse(status=transitioned.metadata.status.value, version=transitioned.metadata.version)


class RollbackRequest(BaseModel):
    version: int


class RollbackResponse(BaseModel):
    status: str
    version: int


@router.post("/{agent_id}/rollback", response_model=RollbackResponse)
def rollback_agent(
    agent_id: str, payload: RollbackRequest, user: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> RollbackResponse:
    """UC1-25 (#141), FR-51: point `deployed_version` back at `payload.version` —
    only from `approved` or `suspended` (`src.agentspec.lifecycle.rollback`),
    never a new version, never a fresh HITL #2 signature. Whatever version is
    *currently* deployed (if any — there is at most one, and it can never be
    the target itself, since a `deployed` target would already have failed
    `rollback`'s own status check) is taken down first via the same `suspend`
    move the kill switch uses, tagged `ROLLED_BACK` rather than
    `ADMIN_KILL_SWITCH` so the audit trail reads "replaced by a rollback", not
    "killed". `provision_agent_role` resets the one Postgres role `agent_id`
    shares across every version (`src.agentspec.roles`'s own docstring) to
    match the *target*'s `connections[]` — the role does not need a separate
    revoke step first, the same reason `deploy_version` above does not need one.
    """
    try:
        target = get_spec_version(db, agent_id, payload.version)
    except SpecVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    try:
        rolled_back = rollback(target)
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=[
                SubmitForApprovalProblemResponse(
                    code=problem.code, path=problem.path, message=problem.message
                ).model_dump()
                for problem in exc.problems
            ],
        ) from None

    # Same order and same reasoning as `deploy_my_agents_version`: refuse a
    # target that cannot serve before the currently-deployed one is suspended.
    # A version can reach this point having been deployable under an older
    # rule set, so rolling *back* to one is not a safe assumption.
    gap = missing_connection_reason(rolled_back)
    if gap is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=gap)

    currently_deployed = get_deployed_spec_version(db, agent_id)
    if currently_deployed is not None:
        update_spec_version_status(db, suspend(currently_deployed, reason=SuspensionReason.ROLLED_BACK))

    with psycopg.connect(get_settings().database_url) as conn:
        try:
            provision_agent_role(conn, rolled_back)
        except AgentRoleGrantError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from None
    write_network_policy(rolled_back, _network_policy_dir(), host_aliases=_network_policy_host_aliases())
    update_spec_version_status(db, rolled_back)
    return RollbackResponse(status=rolled_back.metadata.status.value, version=rolled_back.metadata.version)


class GoldenSetCaseResponse(BaseModel):
    case_id: str
    group: str
    judge: str
    passed: bool
    detail: str


class GoldenSetRunResponse(BaseModel):
    passed_count: int
    total_count: int
    pass_rate: float
    cases: list[GoldenSetCaseResponse]


@router.post("/{agent_id}/versions/{version}/golden-set/run", response_model=GoldenSetRunResponse)
async def run_golden_set(
    agent_id: str,
    version: int,
    user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
    model_factory: Annotated[ChatModelFactory, Depends(get_chat_model_factory)],
    pep_client: Annotated[PEPClient, Depends(get_pep_client)],
    vault: Annotated[VaultKvStore, Depends(get_vault_store)],
) -> GoldenSetRunResponse:
    """UC1-17 (#133), FR-33: run the 23-case golden-set smoke test now and log
    every case's verdict — the record `submit_version_for_approval` reads its
    `deepeval_pass_rate` from, and the approval dossier (UC1-16) renders.

    Never runs implicitly. §8.1's own words for FR-33 are "chạy smoke test...
    trong lúc sandbox" — a creator's action during rehearsal, the same moment
    `POST .../sandbox/chat` belongs to — not something `submit-for-approval`
    triggers on a caller's behalf, the same "the gate checks, it does not
    trigger" shape FR-38's `sandbox_run_count` already has.
    """
    try:
        spec = get_spec_version(db, agent_id, version)
    except SpecVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    result = await run_uc1_golden_set(
        spec,
        GoldenSetRunDeps(
            model_factory=model_factory,
            judge_model_factory=model_factory,
            pep_client=pep_client,
            tool_implementations=default_tool_implementations(resolve_credential=resolve_credential_via(vault)),
            audit_sink=audit_sink(),
            verified_connection_objects=verified_objects_for_spec(db, spec),
        ),
        db,
    )
    record_eval_run(db, agent_id=agent_id, version=version, result=result)
    return GoldenSetRunResponse(
        passed_count=result.passed_count,
        total_count=result.total_count,
        pass_rate=result.pass_rate,
        cases=[
            GoldenSetCaseResponse(
                case_id=case.case_id,
                group=case.group.value,
                judge=case.judge.value,
                passed=case.passed,
                detail=case.detail,
            )
            for case in result.cases
        ],
    )


class ApproveResponse(BaseModel):
    #: `pending_approval` while quorum has not yet been reached — a decision
    #: was still recorded, this call just is not the one that tipped it over.
    status: str
    approvals_recorded: int
    approval_quorum: int


def _record_approval(db: Session, *, agent_id: str, version: int, admin_id: int) -> None:
    """One row per distinct admin — `uq_uc1_version_approvals_admin` makes a
    second `approve` call from the same admin a no-op insert rather than a
    second signature, which is what makes "cùng một người duyệt hai lần không
    đủ quorum" true regardless of how many times that admin clicks.
    """
    already_recorded = db.execute(
        select(UC1VersionApproval).where(
            UC1VersionApproval.agent_id == agent_id,
            UC1VersionApproval.version == version,
            UC1VersionApproval.admin_id == admin_id,
        )
    ).scalar_one_or_none()
    if already_recorded is None:
        db.add(UC1VersionApproval(agent_id=agent_id, version=version, admin_id=admin_id))
        db.commit()


def _distinct_approver_ids(db: Session, *, agent_id: str, version: int) -> list[str]:
    admin_ids = (
        db.execute(
            select(UC1VersionApproval.admin_id)
            .where(UC1VersionApproval.agent_id == agent_id, UC1VersionApproval.version == version)
            .distinct()
        )
        .scalars()
        .all()
    )
    return [str(admin_id) for admin_id in admin_ids]


@router.post("/{agent_id}/versions/{version}/approve", response_model=ApproveResponse)
def approve_version(
    agent_id: str, version: int, user: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> ApproveResponse:
    """UC1-17 (#133), FR-30/FR-32: HITL #2, one admin at a time. Records this
    admin's decision unconditionally, then asks `lifecycle.approve` whether
    the accumulated distinct signers now meet `authority.approval_quorum` —
    the transition only fires on whichever call happens to be the one that
    reaches it.
    """
    try:
        spec = get_spec_version(db, agent_id, version)
    except SpecVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    _record_approval(db, agent_id=agent_id, version=version, admin_id=user.id)
    approver_ids = _distinct_approver_ids(db, agent_id=agent_id, version=version)

    try:
        transitioned = approve(spec, approver_ids=approver_ids)
    except InvalidTransitionError as exc:
        codes = {problem.code for problem in exc.problems}
        if codes == {"approval_quorum_not_met"}:
            # Recorded, just not enough signers yet — not a caller error.
            return ApproveResponse(
                status=spec.metadata.status.value,
                approvals_recorded=len(approver_ids),
                approval_quorum=spec.authority.approval_quorum,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=[
                SubmitForApprovalProblemResponse(
                    code=problem.code, path=problem.path, message=problem.message
                ).model_dump()
                for problem in exc.problems
            ],
        ) from None

    update_spec_version_status(db, transitioned)
    return ApproveResponse(
        status=transitioned.metadata.status.value,
        approvals_recorded=len(approver_ids),
        approval_quorum=transitioned.authority.approval_quorum,
    )


class FieldDiffResponse(BaseModel):
    path: str
    before: object
    after: object


@router.get("/{agent_id}/versions/{version}/diff", response_model=list[FieldDiffResponse])
def diff_versions(
    agent_id: str,
    version: int,
    user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
    against: Annotated[int | None, Query(description="Mặc định là version - 1")] = None,
) -> list[FieldDiffResponse]:
    """UC1-16 (#132), FR-31: `GET .../diff?against={v-1}` — field-level diff for
    the approval screen. `against` defaults to `version - 1`, the query
    string's own name in spec.md §19's contract row.
    """
    against_version = against if against is not None else version - 1
    try:
        current = get_spec_version(db, agent_id, version)
        previous = get_spec_version(db, agent_id, against_version)
    except SpecVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    return [FieldDiffResponse(path=d.path, before=d.before, after=d.after) for d in diff_spec(previous, current)]


class SandboxTranscriptStepResponse(BaseModel):
    node: str
    tool: str | None
    args: dict[str, object] | None
    result: str | None
    tokens: int | None
    latency_ms: float
    cost_usd: float


class SandboxRunSummaryResponse(BaseModel):
    id: int
    thread_id: str
    message: str
    status: str
    transcript: list[SandboxTranscriptStepResponse]
    total_cost_usd: float
    final_answer: str | None

    model_config = {"from_attributes": True}


class DossierResponse(BaseModel):
    """UC1-16 (#132): everything HITL #2 reads about one version, in one call —
    the diff against its parent, its most recent sandbox transcript (FR-38's
    own attached evidence), its most recent golden-set run, and how far along
    HITL #2's own quorum is.
    """

    agent_id: str
    version: int
    status: str
    diff_against_previous: list[FieldDiffResponse] | None
    sandbox_run: SandboxRunSummaryResponse | None
    eval_run: GoldenSetRunResponse | None
    approvals_recorded: int
    approval_quorum: int
    high_risk_tools: list[str]


def _high_risk_tools(spec: AgentSpec) -> list[str]:
    """`capabilities[].tool` this version asks to *write* with — the permission
    an admin reads first (`src.approvals.dossier`'s own V2 precedent for the
    same ordering argument, re-derived here since that module reads a
    different pipeline's documents).
    """
    return [capability.tool for capability in spec.capabilities if capability.scope is Scope.WRITE]


@router.get("/{agent_id}/versions/{version}/dossier", response_model=DossierResponse)
def version_dossier(
    agent_id: str, version: int, user: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> DossierResponse:
    """UC1-16 (#132): everything HITL #2 reads about one version, gathered into
    one response — the diff against its parent, its most recent sandbox
    transcript, its most recent golden-set run, and how far along quorum is.
    """
    try:
        spec = get_spec_version(db, agent_id, version)
    except SpecVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    diff = None
    if spec.metadata.parent_version is not None:
        try:
            previous = get_spec_version(db, agent_id, spec.metadata.parent_version)
        except SpecVersionNotFoundError:
            diff = None
        else:
            diff = [FieldDiffResponse(path=d.path, before=d.before, after=d.after) for d in diff_spec(previous, spec)]

    sandbox_runs = list_sandbox_runs(db, agent_id, version)  # newest first
    eval_run = latest_eval_run(db, agent_id, version)
    approver_ids = _distinct_approver_ids(db, agent_id=agent_id, version=version)

    return DossierResponse(
        agent_id=agent_id,
        version=version,
        status=spec.metadata.status.value,
        diff_against_previous=diff,
        sandbox_run=SandboxRunSummaryResponse.model_validate(sandbox_runs[0]) if sandbox_runs else None,
        eval_run=_eval_run_response(eval_run) if eval_run is not None else None,
        approvals_recorded=len(approver_ids),
        approval_quorum=spec.authority.approval_quorum,
        high_risk_tools=_high_risk_tools(spec),
    )


class QueueEntryResponse(BaseModel):
    """One row of `GET /approvals/queue` — enough to triage, the dossier is
    where the rest lives (UC1-16 / #132, mirroring `src.approvals.queue`'s own
    "row picks, dossier explains" split for the pipeline it reads).
    """

    agent_id: str
    version: int
    name: str
    owner_email: str
    sandbox_run_count: int
    eval_pass_rate: float | None
    high_risk_tools: list[str]
    approvals_recorded: int
    approval_quorum: int
    created_at: UtcTimestamp


def _eval_run_response(run: object) -> "GoldenSetRunResponse":
    return GoldenSetRunResponse(
        passed_count=run.passed_count,  # type: ignore[attr-defined]
        total_count=run.total_count,  # type: ignore[attr-defined]
        pass_rate=run.pass_rate,  # type: ignore[attr-defined]
        cases=[GoldenSetCaseResponse(**case) for case in run.case_results],  # type: ignore[attr-defined]
    )


@queue_router.get("/queue", response_model=list[QueueEntryResponse])
def approval_queue(user: AdminUser, db: Annotated[Session, Depends(get_db)]) -> list[QueueEntryResponse]:
    """UC1-16 (#132), FR-30: everything waiting on `agent_admin`, oldest first —
    the same fairness argument `src.approvals.queue.approval_queue`'s own
    docstring gives for the pipeline it reads: a queue sorted by recency
    starves whoever has waited longest.
    """
    rows = db.execute(
        select(UC1AgentSpecVersion, User)
        .join(User, User.id == UC1AgentSpecVersion.created_by_id)
        .where(UC1AgentSpecVersion.spec.op("->")("metadata").op("->>")("status") == SpecStatus.PENDING_APPROVAL.value)
        .order_by(UC1AgentSpecVersion.created_at, UC1AgentSpecVersion.id)
    ).all()
    return [_queue_entry(db, row, owner) for row, owner in rows]


def _queue_entry(db: Session, row: UC1AgentSpecVersion, owner: User) -> QueueEntryResponse:
    spec = AgentSpec.model_validate(row.spec)
    eval_run = latest_eval_run(db, row.agent_id, row.version)
    return QueueEntryResponse(
        agent_id=row.agent_id,
        version=row.version,
        name=spec.metadata.name,
        owner_email=owner.email,
        sandbox_run_count=sandbox_run_count(db, row.agent_id, row.version),
        eval_pass_rate=eval_run.pass_rate if eval_run is not None else None,
        high_risk_tools=_high_risk_tools(spec),
        approvals_recorded=len(_distinct_approver_ids(db, agent_id=row.agent_id, version=row.version)),
        approval_quorum=spec.authority.approval_quorum,
        created_at=row.created_at,
    )
