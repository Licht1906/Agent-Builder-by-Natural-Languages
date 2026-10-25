"""UC1-21 (#137) — persisting and reading `uc1_pending_actions`, FR-42/FR-43.

The graph itself (`src.compiler.graph`) never touches a database session — it
only accumulates `PendingActionState` entries on `AgentState.pending_actions`
(`src.compiler.state`). `record_pending_actions` is what a route with a real
`Session` in hand (`src.api.sandbox_routes`, and eventually a production chat
route) calls once a turn is over, the same "graph state in, DB row out" shape
`src.workflow.uc1_sandbox_runs.record_sandbox_run` already uses for the
transcript.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.compiler.state import PendingActionState
from src.db.models import UC1PendingAction, UC1PendingActionStatus
from src.policy.pep import Principal


@dataclass(frozen=True, slots=True)
class PendingActionBatch:
    """One turn's worth of `require_approval` decisions, plus which spec version
    and which caller they belong to — bundled the same way `ToolCallContext`
    (`src.compiler.tools`) bundles a tool call's own call-site arguments, since
    `record_pending_actions` would otherwise need five parameters.
    """

    agent_id: str
    version: int
    requester: Principal
    pending_actions: Sequence[PendingActionState]


def record_pending_actions(db: Session, batch: PendingActionBatch) -> list[UC1PendingAction]:
    """One row per `require_approval` decision this turn hit — `batch.requester`
    is the *caller's own* identity (delegated authority, never
    `metadata.created_by`), captured here because it has to outlive the graph
    turn that produced it to be compared, later, against whoever clicks
    approve (FR-46).
    """
    requester = batch.requester
    rows = [
        UC1PendingAction(
            agent_id=batch.agent_id,
            version=batch.version,
            tool=pending["tool"],
            args=dict(pending["args"]),
            action_hash=pending["action_hash"],
            connection_id=pending["connection_id"],
            approver_roles=list(pending["approver_roles"]),
            requested_by_id=int(requester.sub),
            requested_by_sub=requester.sub,
            requested_by_employee_id=requester.employee_id,
            requested_by_department=requester.department,
            requested_by_roles=list(requester.roles),
        )
        for pending in batch.pending_actions
    ]
    if not rows:
        return []
    db.add_all(rows)
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def get_pending_action(db: Session, action_hash: str) -> UC1PendingAction | None:
    """The most recent row for `action_hash` still `pending` — `None` if it was
    already decided or never existed. `action_hash` is not a primary key (the
    table's own docstring: it is fixed per row, not guaranteed globally unique
    across every agent/version), so "most recent pending" is the honest read
    for `POST /approvals/actions/{action_hash}/decision`, matching how a human
    reviewing the queue would pick the one they are looking at.
    """
    return (
        db.execute(
            select(UC1PendingAction)
            .where(UC1PendingAction.action_hash == action_hash, UC1PendingAction.status == UC1PendingActionStatus.PENDING)
            .order_by(UC1PendingAction.id.desc())
        )
        .scalars()
        .first()
    )


def list_pending_actions(db: Session) -> Sequence[UC1PendingAction]:
    """Every action still waiting on a decision, oldest first — `GET /approvals/actions/queue`."""
    return (
        db.execute(
            select(UC1PendingAction).where(UC1PendingAction.status == UC1PendingActionStatus.PENDING).order_by(UC1PendingAction.id)
        )
        .scalars()
        .all()
    )
