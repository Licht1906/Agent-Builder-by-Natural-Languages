"""UC1-17 (#133) — persisting a `GoldenSetRunResult` (`src.evals.uc1_golden_set_runner`)
under `(agent_id, version)`: what `submit-for-approval` reads for its
`deepeval_pass_rate` condition, and what the approval dossier (UC1-16) renders
as the per-case log FR-33's own "log kết quả từng case vào hồ sơ duyệt" asks for.

One row per `POST .../golden-set/run` call, never updated — the same "a new
attempt is a new row" choice `src.agents.spend`/`src.evals.results` make for
every other run record in this platform, so a version resubmitted after a fix
keeps its whole grading history rather than overwriting the run an earlier
rejection was based on.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import UC1EvalRun
from src.evals.uc1_golden_set_runner import GoldenSetRunResult


def record_eval_run(db: Session, *, agent_id: str, version: int, result: GoldenSetRunResult) -> UC1EvalRun:
    row = UC1EvalRun(
        agent_id=agent_id,
        version=version,
        passed_count=result.passed_count,
        total_count=result.total_count,
        pass_rate=result.pass_rate,
        case_results=[
            {
                "case_id": case.case_id,
                "group": case.group.value,
                "judge": case.judge.value,
                "passed": case.passed,
                "detail": case.detail,
            }
            for case in result.cases
        ],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def latest_eval_run(db: Session, agent_id: str, version: int) -> UC1EvalRun | None:
    """The most recent golden-set run for `(agent_id, version)`, or `None` if
    none has ever been recorded — `submit-for-approval` reads this the same
    way it reads `sandbox_run_count`: a fact about what already happened, not
    something the gate triggers itself.
    """
    return db.execute(
        select(UC1EvalRun)
        .where(UC1EvalRun.agent_id == agent_id, UC1EvalRun.version == version)
        .order_by(UC1EvalRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
