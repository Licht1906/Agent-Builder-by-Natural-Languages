"""The levers on the PM control room — what a PM *does* after reading the board.

Four, and each one reuses the platform's existing write path rather than
growing a second one, so a decision made from this screen is indistinguishable
in the database from one made from the creator's own:

- **Kill switch** — `pause_or_resume` is `src.deployments.publication.set_status`
  for any agent, admin-scoped. The owner's door does the same write for their
  own agent; this one exists because the person who sees an agent failing on
  the board is not its owner.
- **Thumbs** — `leave_feedback` writes one `quality_scores` row, source
  `human`, name `thumbs_up`, comment optional. Playbook step 4: the cheapest
  label there is, and the calibration set for step 5's judge.
- **Decide a waiting run** — `decide_pending_run` records the decision through
  `src.workflow.approvals.decide_approval`, the same function the owner's
  route calls, so the audit row is the same row. Resuming the paused graph is
  the route's job (it needs the request's run factory and checkpointer); this
  module only says whether there is anything *to* resume, which for a simulated
  row there is not.
- **Run → case** — `promote_run_to_case` turns one bad run into an `eval_cases`
  row (origin `promoted` — ADR-0054 §1) in the agent's suite — playbook §2's
  first rule, "start from real failures", made one click. It also returns the
  line a reviewer would paste into `golden/`, because the golden set is a
  reviewed artifact and an API must not write to it behind the diff.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.db.models import (
    Agent,
    AgentRun,
    Deployment,
    DeploymentStatus,
    EvalCase,
    EvalCaseOrigin,
    QualityScore,
    RunApproval,
    RunApprovalStatus,
    ScoreSource,
)
from src.deployments.publication import set_status
from src.evals.cases import get_or_create_suite
from src.pm.simulation import is_simulated_conversation
from src.workflow.approvals import ApprovalDecisionError, decide_approval

THUMBS = "thumbs_up"


class ControlError(ValueError):
    """A lever pulled on something that is not there, or not in a state to be pulled."""


# -- Kill switch ------------------------------------------------------------ #


def pause_or_resume(db: Session, *, agent_id: int, serving: bool) -> Deployment:
    """Pause (`serving=False`) or resume an agent's Deployment. 404-shaped error if it was never published."""
    agent = db.get(Agent, agent_id)
    if agent is None or agent.deployment is None:
        raise ControlError(f"Agent {agent_id} chưa được publish — không có deployment để tạm dừng hay chạy lại")
    return set_status(db, agent.deployment, DeploymentStatus.ACTIVE if serving else DeploymentStatus.DISABLED)


# -- Thumbs ----------------------------------------------------------------- #


def leave_feedback(db: Session, *, run_id: int, thumbs_up: bool, comment: str | None, given_by: int) -> QualityScore:
    """One human verdict on one run of the shared ledger. Append-only: a second vote is a second row,
    and `metrics.recent_runs` shows the newest — changing one's mind is data."""
    run = db.get(AgentRun, run_id)
    if run is None:
        raise ControlError(f"Không có lần chạy #{run_id}")
    score = QualityScore(
        agent_id=run.agent_id,
        version_id=run.version_id,
        run_cost_id=run.run_cost_id,
        run_id=run.id,
        name=THUMBS,
        value=1.0 if thumbs_up else 0.0,
        comment=(comment or "").strip() or None,
        source=ScoreSource.HUMAN,
        given_by=given_by,
    )
    db.add(score)
    db.commit()
    db.refresh(score)
    return score


# -- Decide a waiting run --------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Decision:
    approval: RunApproval
    #: Whether a checkpointed graph is waiting behind this row. `False` for a
    #: simulated row, and the route then knows not to go looking for one.
    resumable: bool


def decide_pending_run(
    db: Session, *, approval_id: int, agent_id: int, decided_by: int, approved: bool, reason: str | None
) -> Decision:
    """Record the PM's decision on one pending `run_approvals` row.

    Same rules as the owner's door: a rejection needs a reason, a decided row
    cannot be decided again, and the row is written before anything resumes.
    A row that is not `agent_id`'s is refused by the same error as a missing
    one — it is not this request's to decide.
    """
    row = db.get(RunApproval, approval_id)
    if row is None or row.agent_id != agent_id:
        raise ControlError(f"Không tìm thấy yêu cầu duyệt {approval_id} của agent {agent_id}")
    if row.status is not RunApprovalStatus.PENDING:
        raise ControlError(f"Yêu cầu duyệt {approval_id} đã được quyết định rồi ({row.status.value})")
    try:
        decide_approval(db, row, decided_by=decided_by, approved=approved, reason=reason)
    except ApprovalDecisionError as exc:
        raise ControlError(str(exc)) from None
    db.refresh(row)
    return Decision(approval=row, resumable=not is_simulated_conversation(row.conversation_id))


# -- Run → case ------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PromotedCase:
    #: `None` for a UC-1 run — there is no `agent_versions` row to attach a case to.
    case: EvalCase | None
    golden_line: str


def promote_run_to_case(
    db: Session,
    *,
    run_id: int,
    input_message: str | None = None,
    expected: str,
    criterion: str | None = None,
) -> PromotedCase:
    """A failed (or complained-about) run, kept as a test the version must pass from now on.

    `input_message` defaults to what the run's own record says the person
    asked — the ledger keeps it since ADR-0053, so the PM no longer retypes
    it — and `expected` is exactly the judgement a person adds when they
    promote a failure. A UC-1 run has no `agent_versions` row to hang an
    `eval_cases` row on; it gets the golden line only, and the caller is told.
    """
    run = db.get(AgentRun, run_id)
    if run is None:
        raise ControlError(f"Không có lần chạy #{run_id}")
    text = (input_message or run.input_message or "").strip()
    want = (expected or "").strip()
    if not text or not want:
        raise ControlError(
            "Cần cả câu hỏi đầu vào lẫn đáp án mong đợi — một case thiếu một trong hai thì không chấm được"
        )
    line = json.dumps(
        {
            "id": f"run-{run.id}",
            "source": "real_failure" if not run.simulated else "simulated_failure",
            "pipeline": run.pipeline,
            "agent": run.agent_key,
            "version": run.version_key,
            "outcome": run.outcome,
            "input": text,
            "answer_was": (run.answer or "")[:2000],
            "expected": want,
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
    )
    if run.version_id is None or run.agent_id is None:
        return PromotedCase(case=None, golden_line=line)
    suite = get_or_create_suite(db, run.agent_id)
    case = EvalCase(
        suite_id=suite.id,
        introduced_in_version_id=run.version_id,
        criterion=(criterion or "").strip()
        or f"Thu từ lần chạy #{run.id} ({run.outcome}) — không được lặp lại lỗi này",
        input_message=text,
        expected=want,
        origin=EvalCaseOrigin.PROMOTED,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return PromotedCase(case=case, golden_line=line)
