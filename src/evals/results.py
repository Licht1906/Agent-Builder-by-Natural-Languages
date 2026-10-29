"""What a run scored, where it is written down, and how to read it back.

Split from `runner` because the two change for different reasons: a new way of
executing the set edits the runner, a new question about a score edits this.

The arithmetic is small and it is all here rather than spread across a route, a
gate and a template. `pass_rate`, "is that enough", "how far short" and "how many
more cases" are four phrasings of one comparison, and four call sites computing
them separately is how a screen ends up saying *đạt* beside a blocked button.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.db.base import SessionLocal
from src.db.models import EvalRun, EvalRunKind, EvalRunStatus
from src.models.schemas import EvalCaseResult, SkippedCase


@dataclass(frozen=True, slots=True)
class Score:
    """A run's result measured against a bar — always *some* bar, named on the spot.

    The threshold is a field rather than something this type looks up, because
    the same run is legitimately compared against two different numbers: the one
    in force now, which decides whether approval opens, and the one in force when
    it ran, which is what the dossier records. A type that fetched its own
    threshold could only ever answer one of those.
    """

    passed_count: int
    total_count: int
    threshold: float

    @property
    def pass_rate(self) -> float:
        """The fraction of cases that passed.

        Zero for a run with no cases at all. `0/0` is not 1.0 here: a set that
        asked nothing has demonstrated nothing, and treating it as a perfect
        score would clear every threshold on no evidence. `src.evals.runner`
        refuses to start such a run; this is the same answer given twice.
        """
        return self.passed_count / self.total_count if self.total_count else 0.0

    @property
    def meets_threshold(self) -> bool:
        """Whether this score opens the way to an approval request.

        `>=`, so a threshold of exactly the achieved rate passes — a bar is a
        minimum, and `0.6` means "six in ten is enough" rather than "six in ten
        is not quite enough".
        """
        return self.pass_rate >= self.threshold

    @property
    def gap(self) -> float:
        """How far below the bar this run is, as a fraction. Zero once it is met."""
        return max(0.0, self.threshold - self.pass_rate)

    @property
    def cases_short(self) -> int:
        """How many more cases would have had to pass. Zero once the bar is met.

        The number a creator can actually act on: "cách ngưỡng 0.15" is hard to
        picture, "còn một case nữa" is not.

        Rounded before the ceiling because `0.6 * 5` is `2.9999999999999996` in
        binary floating point, and a bare `ceil` would answer "three more" to
        someone who needs three in total and already has three.
        """
        if self.meets_threshold:
            return 0
        needed = math.ceil(round(self.threshold * self.total_count, 9))
        return max(0, needed - self.passed_count)


@dataclass(frozen=True, slots=True)
class Tally:
    """How one grader's share of a run went — passed out of asked.

    No `pass_rate` and no threshold, unlike `Score`, and the absence is the
    point: the bar is a statement about the whole set (ADR-0011 §5), and a half
    of it measured against the same number would read as a second verdict on a
    version that only ever gets one.
    """

    passed_count: int
    total_count: int


@dataclass(frozen=True, slots=True)
class Breakdown:
    """One run's cases split by *who* decided them — see #49 / ADR-0028 §2.

    The score alone cannot answer the question an admin actually has. *"4/5 đạt"*
    is one sentence over two completely different kinds of verdict: an opinion an
    LLM formed about an answer, and a fact read straight off the transcript. The
    first is what `eval_override` exists for, because a judge wobbles; the second
    is what it may never touch.

    `safety_failures` is deliberately not "the rule-based cases that failed". A
    workflow that never reached its `End` fails a rule and is merely broken; one
    that called a forbidden action fails a rule and is the thing the gate exists
    to stop, and only that second kind is unwaivable (ADR-0028 §6). Counted here
    rather than recomputed by whoever displays it, so the number beside the
    override button is the same one `src.evals.gate` refuses on.
    """

    rule_based: Tally
    judged: Tally
    safety_failures: int


def breakdown_of(run: EvalRun) -> Breakdown:
    """`run`'s cases split by grader, read off what each result recorded.

    The split is `expected_check`, not `category`: a case carries a check exactly
    when a rule graded it, while `category` is `None` on the prohibition cases
    ADR-0028 §4 generates — which are the most rule-based cases in the set.
    Splitting on the category would file them with the judge's, and the one
    verdict no admin can waive would be shown as the one kind they can.
    """
    results = results_of(run)
    rule_based = [result for result in results if result.expected_check is not None]
    judged = [result for result in results if result.expected_check is None]
    return Breakdown(
        rule_based=_tally(rule_based),
        judged=_tally(judged),
        safety_failures=sum(1 for result in results if result.safety_violation),
    )


def _tally(results: list[EvalCaseResult]) -> Tally:
    """`results` counted: how many passed, out of how many were asked."""
    return Tally(passed_count=sum(1 for result in results if result.passed), total_count=len(results))


def store_run(
    db: Session,
    version_id: int,
    *,
    threshold: float,
    results: list[EvalCaseResult],
    skipped: list[SkippedCase] | None = None,
    judge_model: str = "",
) -> EvalRun:
    """Write one finished run down, results and all.

    `threshold` is stored as the bar that stood at the time. It is not what the
    approval gate later compares against (ADR-0011 §5) — it is what lets someone
    reading this run months later see what it was measured against.

    `skipped` never counts toward `total_count` — ADR-0054 §1's whole point is
    that a case whose rule cannot be graded against this version's graph is not
    evidence about the version, so it must not sit in the same denominator as
    the cases that actually ran.

    `judge_model` is likewise a fact about *this* run, read once by the caller
    (`Settings.judge_model`, ADR-0054 §4) rather than re-read per case — an
    admin's mid-run edit must not leave one run scored by two different judges
    while claiming to be one measurement.
    """
    run = EvalRun(
        version_id=version_id,
        threshold=threshold,
        passed_count=sum(1 for result in results if result.passed),
        total_count=len(results),
        results=[result.model_dump(mode="json") for result in results],
        skipped=[case.model_dump(mode="json") for case in (skipped or [])],
        judge_model=judge_model,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def open_run(
    db: Session,
    version_id: int,
    *,
    threshold: float,
    total: int,
    skipped: list[SkippedCase],
    judge_model: str,
    kind: EvalRunKind = EvalRunKind.GATE,
) -> EvalRun:
    """Write down a run that has been asked for and not yet done — `running`, 0 of `total` (EV-01).

    The threshold and the judge are fixed here, once for the whole run, for
    `store_run`'s reasons: the background job that fills the row in later must
    not pick up a bar or a judge an admin changed while it ran.
    """
    run = EvalRun(
        version_id=version_id,
        threshold=threshold,
        passed_count=0,
        total_count=total,
        results=[],
        skipped=[case.model_dump(mode="json") for case in skipped],
        judge_model=judge_model,
        status=EvalRunStatus.RUNNING,
        done_count=0,
        kind=kind,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def record_progress(db: Session, run_id: int, done: int) -> None:
    """`done` cases of a running run have a verdict — what the screen shows as "37/150"."""
    db.execute(update(EvalRun).where(EvalRun.id == run_id).values(done_count=done))
    db.commit()


def finish_run(db: Session, run_id: int, results: list[EvalCaseResult]) -> None:
    """Fill a running run in with its results, in case order, and mark it `completed` — evidence from now on."""
    db.execute(
        update(EvalRun)
        .where(EvalRun.id == run_id)
        .values(
            status=EvalRunStatus.COMPLETED,
            passed_count=sum(1 for result in results if result.passed),
            total_count=len(results),
            done_count=len(results),
            results=[result.model_dump(mode="json") for result in results],
        )
    )
    db.commit()


def fail_run(db: Session, run_id: int, reason: str) -> None:
    """A run that stopped as a whole — not a case that failed, which `finish_run` records like any verdict."""
    db.execute(update(EvalRun).where(EvalRun.id == run_id).values(status=EvalRunStatus.FAILED, error=reason))
    db.commit()


#: What `recover_interrupted_eval_runs` writes into `error`.
INTERRUPTED_RUN = "Bị gián đoạn giữa chừng (khởi động lại server) — hãy chạy lại"


def recover_interrupted_eval_runs(session_factory: Callable[[], Session] = SessionLocal) -> int:
    """At boot, mark every run a previous process left `running` as `failed`.

    The background job lives entirely inside the process that accepted the
    request (`src.services.ingestion`'s reason, and the same remedy): nothing is
    resumed, the row just stops claiming that somebody is working on it.
    """
    db = session_factory()
    try:
        stalled = list(db.scalars(select(EvalRun).where(EvalRun.status == EvalRunStatus.RUNNING)))
        for run in stalled:
            run.status = EvalRunStatus.FAILED
            run.error = INTERRUPTED_RUN
        db.commit()
        return len(stalled)
    finally:
        db.close()


def latest_run(db: Session, version_id: int) -> EvalRun | None:
    """The most recent **completed gate** run of `version_id`, or `None` if it has never finished one.

    The latest rather than the best. A creator who edits their cases and runs
    again is asking to be judged on the set they now stand behind, and "best run
    ever" would let a version be approved on a test set its own creator has since
    replaced.

    Completed only (EV-01): a run still going has no verdict yet, and one that
    stopped as a whole has none at all — neither may stand in for the last run
    that did finish, in the gate or anywhere else. Gate only (EV-04): a smoke
    run is a subset, partly from cache, and is never evidence for approval.
    """
    return db.execute(
        select(EvalRun)
        .where(
            EvalRun.version_id == version_id,
            EvalRun.status == EvalRunStatus.COMPLETED,
            EvalRun.kind == EvalRunKind.GATE,
        )
        .order_by(EvalRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def newest_run(db: Session, version_id: int) -> EvalRun | None:
    """The most recent run of `version_id` whatever its status — what the Evaluation tab reopens on."""
    return db.execute(
        select(EvalRun).where(EvalRun.version_id == version_id).order_by(EvalRun.id.desc()).limit(1)
    ).scalar_one_or_none()


def results_of(run: EvalRun) -> list[EvalCaseResult]:
    """A stored run's results, back in the shape they were written in."""
    return [EvalCaseResult.model_validate(payload) for payload in run.results]


def skipped_of(run: EvalRun) -> list[SkippedCase]:
    """A stored run's `ungrounded` cases, back in the shape they were written in."""
    return [SkippedCase.model_validate(payload) for payload in run.skipped]
