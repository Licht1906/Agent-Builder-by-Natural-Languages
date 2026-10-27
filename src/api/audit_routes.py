"""The audit chain, as an admin reads and checks it — DS-10.

Two doors, both admin-only: the newest entries (what a run read and did),
and a walk of the whole chain naming the first place it does not hold. The
same check runs from the command line as `scripts/verify_audit_chain.py`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.audit.chain import AuditKind, verify_chain
from src.auth.dependencies import AdminUser
from src.db.base import get_db
from src.db.models import AuditEvent
from src.models.schemas import AuditChainVerdictResponse, AuditEventResponse

router = APIRouter(prefix="/admin/audit", tags=["audit"])

Db = Annotated[Session, Depends(get_db)]


@router.get("/events", response_model=list[AuditEventResponse])
def list_audit_events(
    _admin: AdminUser,
    db: Db,
    kind: AuditKind | None = None,
    trace_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditEventResponse]:
    """The newest entries first, narrowed to one kind or one run's trace if asked."""
    statement = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(limit)
    if kind is not None:
        statement = statement.where(AuditEvent.kind == kind.value)
    if trace_id:
        statement = statement.where(AuditEvent.trace_id == trace_id)
    return [AuditEventResponse.model_validate(row, from_attributes=True) for row in db.scalars(statement)]


@router.get("/verify", response_model=AuditChainVerdictResponse)
def verify_audit_chain(_admin: AdminUser, db: Db) -> AuditChainVerdictResponse:
    """Walk the whole chain: intact, or the first `seq` where it breaks and why."""
    verdict = verify_chain(db)
    return AuditChainVerdictResponse(
        ok=verdict.ok, checked=verdict.checked, broken_at=verdict.broken_at, reason=verdict.reason
    )
