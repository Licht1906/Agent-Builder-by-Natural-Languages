"""UC1-21 (#137) + UC1-23 (#139) — `/approvals/actions/...`, spec.md §19's own
`POST /approvals/actions/{action_hash}/decision`, "duyệt hành động write cụ
thể (email, task) — tách khỏi duyệt version".

`_attempt_approval` is where both tickets' integrity rules live, because they
are the same code path (tickets.md's own reason for shipping them together):

- **FR-46, two *independent* layers, neither deferring to the other**: the
  API layer's own identity check (`approver_sub` is always `str(user.id)` —
  *this call's own* authenticated caller, never a client-supplied field) does
  not short-circuit before the PEP is asked — `execute_tool_call` always
  runs first, so a self-approval is genuinely refused by `authz.rego`'s own
  `approver_sub != principal.sub` rule too (`ApprovalRequiredError`, since a
  self-approval never satisfies `approval_granted`). The API check that
  follows is a backstop against a *misconfigured* PEP granting it anyway, not
  the only reason it fails — "bị từ chối cả ở API lẫn PEP" holds even if
  either check were deleted.
- **FR-45, at the PEP**: approval execution replays through
  `execute_tool_call` (`src.compiler.tools`) exactly the way a graph turn
  would have, with the *stored* `action_hash` bound into the `Approval` it
  submits. `execute_tool_call` recomputes the hash from whatever `args` the
  row holds *right now* — if that has drifted from what was originally
  queued, the freshly computed hash no longer matches the approval's, and the
  PEP denies, never this module's own judgment call.

Approving does not leave a second "approved but not yet executed" state to
forget about: `_attempt_approval` executes the real tool call in the same
request, so a 200 with `status: "approved"` is also the fact that the real
implementation ran (FR-42's real SMTP send, FR-43's real task write).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.agentspec.spec import AgentSpec, Capability, SpecStatus
from src.agentspec.store import SpecVersionNotFoundError, get_spec_version
from src.auth.dependencies import CurrentUser
from src.compiler.state import EMPTY_PROVENANCE, EMPTY_SESSION
from src.compiler.tools import (
    ApprovalRequiredError,
    BoundTool,
    ToolCallContext,
    ToolDeniedError,
    default_tool_implementations,
    execute_tool_call,
)
from src.db.base import get_db
from src.db.models import UC1PendingAction, UC1PendingActionStatus, User
from src.policy.audit import audit_sink
from src.policy.pep import Approval, PEPClient, Principal, get_pep_client
from src.services.vault_secret_store import VaultKvStore, get_vault_store, resolve_credential_via
from src.workflow.uc1_pending_actions import get_pending_action, list_pending_actions

router = APIRouter(prefix="/approvals/actions", tags=["uc1-action-approval"])


class ActionQueueEntryResponse(BaseModel):
    action_hash: str
    agent_id: str
    version: int
    tool: str
    args: dict[str, object]
    approver_roles: list[str]
    requested_by_employee_id: str
    created_at: str

    model_config = {"from_attributes": True}


def _queue_entry(row: UC1PendingAction) -> ActionQueueEntryResponse:
    return ActionQueueEntryResponse(
        action_hash=row.action_hash,
        agent_id=row.agent_id,
        version=row.version,
        tool=row.tool,
        args=row.args,
        approver_roles=row.approver_roles,
        requested_by_employee_id=row.requested_by_employee_id,
        created_at=row.created_at.isoformat(),
    )


@router.get("/queue", response_model=list[ActionQueueEntryResponse])
def action_queue(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> list[ActionQueueEntryResponse]:
    """Every write-tool call still waiting on a decision, oldest first (FR-42/FR-43)."""
    return [_queue_entry(row) for row in list_pending_actions(db)]


class ActionDecisionRequest(BaseModel):
    decision: str  # "approve" | "reject"
    reason: str | None = None


class ActionDecisionResponse(BaseModel):
    action_hash: str
    tool: str
    status: str
    reasons: list[str] = []
    result: dict[str, object] | None = None


def _policy_spec_for(db: Session, agent_id: str, version: int) -> AgentSpec:
    """The same "frozen `deployed`" spec `compile_spec` sends the PEP — a queued
    action's execution has to be judged by the exact policy its graph turn
    would have been, not by whatever `metadata.status` happens to be by the
    time someone gets around to approving it.
    """
    try:
        spec = get_spec_version(db, agent_id, version)
    except SpecVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    return spec.model_copy(update={"metadata": spec.metadata.model_copy(update={"status": SpecStatus.DEPLOYED})})


def _requester_of(row: UC1PendingAction) -> Principal:
    """Delegated authority preserved through the decision: the real tool call
    executes attributed to the *original asker*, never the approver — the
    same identity a graph turn would have submitted this action under.
    """
    return Principal(
        sub=row.requested_by_sub,
        employee_id=row.requested_by_employee_id,
        roles=list(row.requested_by_roles),
        department=row.requested_by_department,
    )


_SELF_APPROVAL_MESSAGE = "Người tạo action không được tự duyệt hành động của chính mình"


@dataclass(frozen=True, slots=True)
class _ApprovalAttempt:
    """Everything one approve attempt needs, bundled — the same "3 parameters"
    fix `ToolCallContext`/`CompileDependencies` already apply elsewhere in
    this codebase (`src.compiler.tools`/`src.compiler.graph`).
    """

    row: UC1PendingAction
    spec: AgentSpec
    approver: User
    reason: str | None
    pep_client: PEPClient
    vault: VaultKvStore
    db: Session

    @property
    def is_self_approval(self) -> bool:
        return str(self.approver.id) == self.row.requested_by_sub


def _bound_tool_for(attempt: _ApprovalAttempt, capability: Capability) -> BoundTool:
    implementation = default_tool_implementations(resolve_credential=resolve_credential_via(attempt.vault))[attempt.row.tool]
    return BoundTool(name=attempt.row.tool, scope=capability.scope, connection_id=attempt.row.connection_id, implementation=implementation)


def _execution_context(attempt: _ApprovalAttempt) -> ToolCallContext:
    approval = Approval(
        tool=attempt.row.tool,
        approver_sub=str(attempt.approver.id),
        approver_roles=[attempt.approver.role.value],
        action_hash=attempt.row.action_hash,
    )
    return ToolCallContext(
        policy_spec=attempt.spec,
        principal=_requester_of(attempt.row),
        provenance=EMPTY_PROVENANCE.copy(),
        session=EMPTY_SESSION.copy(),
        pep_client=attempt.pep_client,
        audit_sink=audit_sink(),
        approvals=[approval],
        # UC1-31 (#143): this replay is its own request, distinct from the
        # turn that originally queued the pending action — that turn's own
        # request id already got its own log lines/audit rows when it first
        # hit `require_approval`.
        request_id=uuid.uuid4().hex,
    )


def _mark_approved(attempt: _ApprovalAttempt, tool_result: object) -> ActionDecisionResponse:
    result = tool_result if isinstance(tool_result, dict) else {"value": tool_result}
    row = attempt.row
    row.status = UC1PendingActionStatus.APPROVED
    row.decided_by_id = attempt.approver.id
    row.decision_reason = attempt.reason
    row.decided_at = datetime.now(UTC)
    row.result = result
    attempt.db.commit()
    return ActionDecisionResponse(action_hash=row.action_hash, tool=row.tool, status="approved", result=result)


def _attempt_approval(attempt: _ApprovalAttempt) -> ActionDecisionResponse:
    """FR-45/FR-46: always asks the PEP first — a self-approval is refused by
    `authz.rego` itself (never granted, so it falls through to `pending`),
    and the explicit identity check below only turns that outcome into a
    clearer 403; it is never the *sole* reason self-approval fails.
    """
    row = attempt.row
    capability = next((c for c in attempt.spec.capabilities if c.tool == row.tool), None)
    if capability is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"tool {row.tool!r} không còn nằm trong capabilities của version này")

    bound, ctx = _bound_tool_for(attempt, capability), _execution_context(attempt)
    try:
        outcome = execute_tool_call(bound, row.args, ctx)
    except (ToolDeniedError, ApprovalRequiredError) as exc:
        if attempt.is_self_approval:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_SELF_APPROVAL_MESSAGE) from None
        reasons = exc.reasons if isinstance(exc, ToolDeniedError) else [f"vẫn cần duyệt bởi {exc.approver_roles}"]
        return ActionDecisionResponse(action_hash=row.action_hash, tool=row.tool, status="pending", reasons=reasons)

    if attempt.is_self_approval:
        # Backstop only: a correctly-configured PEP already refused this above
        # (`approver_sub != principal.sub` never holds), so this line is only
        # ever reached by a misconfigured PEP — the API layer still refuses.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_SELF_APPROVAL_MESSAGE) from None

    return _mark_approved(attempt, outcome.result)


@router.post("/{action_hash}/decision", response_model=ActionDecisionResponse)
def decide_action(
    action_hash: str,
    payload: ActionDecisionRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    pep_client: Annotated[PEPClient, Depends(get_pep_client)],
    vault: Annotated[VaultKvStore, Depends(get_vault_store)],
) -> ActionDecisionResponse:
    row = get_pending_action(db, action_hash)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"không có action nào đang chờ duyệt với hash {action_hash!r}")

    if payload.decision == "reject":
        row.status = UC1PendingActionStatus.REJECTED
        row.decided_by_id = user.id
        row.decision_reason = payload.reason
        row.decided_at = datetime.now(UTC)
        db.commit()
        return ActionDecisionResponse(action_hash=action_hash, tool=row.tool, status="rejected")

    if payload.decision != "approve":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="decision phải là 'approve' hoặc 'reject'")

    spec = _policy_spec_for(db, row.agent_id, row.version)
    attempt = _ApprovalAttempt(row=row, spec=spec, approver=user, reason=payload.reason, pep_client=pep_client, vault=vault, db=db)
    return _attempt_approval(attempt)
