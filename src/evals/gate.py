"""The gate: a version below the bar cannot ask to be approved.

This is the module that makes eval govern something. Everything else in
`src.evals` produces a number; this decides what the number *means* for a
version, and it is the only place that decides it — `src.agents.versions` calls
in rather than re-deriving the rule, the same way it calls into
`src.admin.config` for the allowlist.

The refusals are written to be acted on. #13 asks for *"giải thích rõ việc tiếp
theo phải làm — chứ không phải một thông báo từ chối không lý do"*, and a creator
who is told only that they were blocked will re-run the identical set and be
blocked again. So a refusal always says the score, the bar, the distance in whole
cases, and what to go and do.
"""

from sqlalchemy.orm import Session

from src.admin.config import read_config
from src.db.models import EvalRun
from src.evals.results import Score, latest_run, results_of
from src.models.schemas import EvalCaseResult

#: Said to a version that has never been evaluated. Distinct from a low score on
#: purpose: "you have not run it" and "you ran it and fell short" call for
#: completely different next moves, and one message for both would send half its
#: readers the wrong way.
NEVER_EVALUATED = (
    "Version chưa chạy eval lần nào — hãy sinh bộ test case từ tiêu chí thành công, "
    "xem lại và sửa những case chưa đúng ý, rồi chạy eval trước khi xin duyệt"
)


class EvalGateError(ValueError):
    """An approval request refused on the eval score. Message is user-facing."""


def score_for(db: Session, run: EvalRun) -> Score:
    """`run` measured against the bar in force **now**.

    The threshold is read fresh (`src.admin.config` never caches), so an admin
    who raises the bar raises it for every approval request made afterwards, not
    only for runs that happen after the edit. The run keeps its own record of the
    bar it faced (`eval_runs.threshold`); what changes here is the question being
    asked, which is always *"is this good enough now"* — see ADR-0011 §5.
    """
    return Score(
        passed_count=run.passed_count,
        total_count=run.total_count,
        threshold=read_config(db).eval_pass_threshold,
    )


def live_score(db: Session, version_id: int) -> Score | None:
    """The latest eval run of `version_id` against the current bar, or `None` if there is none.

    `None` is a different answer from a score of zero — "never evaluated" and
    "evaluated and failed everything" call for different next moves — and every
    caller treats it as one.
    """
    run = latest_run(db, version_id)
    return score_for(db, run) if run is not None else None


def _safety_failures(run: EvalRun) -> list[EvalCaseResult]:
    """The cases of `run` that failed on a rule nobody may waive — #48 / ADR-0028 §6.

    Read off the stored results rather than recomputed, because the run is the
    record: the transcript that produced the verdict is gone by the time anyone
    asks, and re-deriving would mean re-running the workflow to answer a question
    about a run that already happened.
    """
    return [result for result in results_of(run) if result.safety_violation]


def unwaivable_blocker(db: Session, version_id: int) -> str | None:
    """Why this version cannot ask for approval **at all**, or `None` — override or not.

    ADR-0011 §7's `eval_override` exists because *"the judge is an LLM that
    wobbles"*: a plausible excuse for a low score, and no excuse whatsoever for a
    deterministic fact read off a transcript. A case that failed because a
    forbidden action was called, or because a guarded action fired after a
    refusal, is that kind of fact — so it blocks regardless of who is asking and
    what reason they give (ADR-0028 §6).

    Separate from `blocked_reason` rather than folded into it, because they are
    answers to two different questions. `blocked_reason` describes a *score*
    against a bar, and a score is exactly what an override is allowed to waive.
    This describes a rule that was broken, and the whole point is that it is not a
    number anybody can be over or under.

    A version whose latest run has no such failure gets `None` here and is judged
    on its score as before — including a version that has never been evaluated,
    which is `blocked_reason`'s refusal to give, not this one's.
    """
    run = latest_run(db, version_id)
    if run is None:
        return None
    violations = _safety_failures(run)
    if not violations:
        return None
    named = "; ".join(f"{result.criterion} — {result.reason}" for result in violations)
    return (
        f"Eval gần nhất có {len(violations)} case vi phạm luật cứng: {named}. "
        f"Đây là điều đọc thẳng từ nhật ký chạy chứ không phải điểm số, nên không vượt rào được "
        f"bằng eval_override — phải sửa workflow cho không còn vi phạm rồi chạy lại eval"
    )


def blocked_reason(score: Score | None) -> str | None:
    """Why this score cannot ask for approval, or `None` if it can.

    Returning the sentence rather than raising, so the same words can be shown
    on the eval screen the moment a run finishes — before the creator presses a
    button and is refused. A gate whose reasoning only appears at the point of
    refusal teaches nothing.
    """
    if score is None:
        return NEVER_EVALUATED
    if score.meets_threshold:
        return None
    return (
        f"Eval gần nhất đạt {score.passed_count}/{score.total_count} case "
        f"(tỷ lệ {score.pass_rate:.2f}), dưới ngưỡng {score.threshold:.2f} — "
        f"còn thiếu {score.cases_short} case nữa. Việc tiếp theo: đọc lý do không đạt "
        f"của từng case, sửa spec nếu agent thật sự sai, hoặc sửa lại case nếu case "
        f"chưa đúng ý bạn, rồi chạy eval lại"
    )


def refuse_unless_eval_passed(db: Session, version_id: int) -> None:
    """Let an approval request through, or raise saying why not.

    Split from `blocked_reason` so the same rule serves both a question ("may
    I?") and an action ("do it"). Two implementations of that rule is how a
    screen ends up showing a green light over a button that refuses.

    The unwaivable rule is checked first, so a version that both broke a rule and
    scored badly is told about the rule: the score is fixable by rewriting cases,
    and the rule is not — quoting the score would send the creator to the wrong
    screen.
    """
    reason = version_blocker(db, version_id)
    if reason is not None:
        raise EvalGateError(reason)


def version_blocker(db: Session, version_id: int) -> str | None:
    """Everything eval has to say against `version_id` asking for approval, or `None`.

    The two refusals in the order they matter, in one function, because five
    places ask this question — the gate itself, the creator's status field
    (`src.agents.versions.submission_blocker`), the eval screen
    (`src.api.eval_routes.eval_run_response`), the admin's queue
    (`src.approvals.queue`) and their dossier (`src.approvals.dossier`) — and five
    call sites composing them by hand is how one screen ends up green while the
    button refuses.

    `request_approval` deliberately does **not** call this: it has to check the
    unwaivable half even when an `eval_override` waives the other, so it asks the
    two questions separately. That is the one call site where they are genuinely
    different questions.
    """
    return unwaivable_blocker(db, version_id) or blocked_reason(live_score(db, version_id))
