"""UC1-38 (#126) — persisting sandbox transcripts (`uc1_sandbox_runs`), FR-37/FR-38.

`sandbox_run_count` exists for FR-38's own gate ("không được xin duyệt nếu
chưa có ít nhất một lượt chat thử") — wiring it into
`POST .../submit-for-approval` is UC1-39 (#127), a separate, not-yet-built
ticket; this function is what that ticket will call.
"""

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.compiler.sandbox import SandboxTurnResult
from src.db.models import UC1SandboxRun, UC1SandboxRunStatus


def record_sandbox_run(
    db: Session,
    *,
    agent_id: str,
    version: int,
    principal_employee_id: str,
    message: str,
    result: SandboxTurnResult,
) -> UC1SandboxRun:
    """One row per sandbox turn — the transcript FR-38 requires a version to have at least one of."""
    row = UC1SandboxRun(
        agent_id=agent_id,
        version=version,
        principal_employee_id=principal_employee_id,
        thread_id=result.thread_id,
        message=message,
        status=UC1SandboxRunStatus(result.status.value),
        transcript=list(result.transcript),
        total_cost_usd=result.total_cost_usd,
        final_answer=result.final_answer,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_sandbox_runs(db: Session, agent_id: str, version: int) -> Sequence[UC1SandboxRun]:
    """Every sandbox run for `(agent_id, version)`, newest first — `GET .../sandbox/runs`."""
    return (
        db.execute(
            select(UC1SandboxRun)
            .where(UC1SandboxRun.agent_id == agent_id, UC1SandboxRun.version == version)
            .order_by(UC1SandboxRun.id.desc())
        )
        .scalars()
        .all()
    )


def sandbox_run_count(db: Session, agent_id: str, version: int) -> int:
    return db.execute(
        select(func.count())
        .select_from(UC1SandboxRun)
        .where(UC1SandboxRun.agent_id == agent_id, UC1SandboxRun.version == version)
    ).scalar_one()
