"""Reading one run and saying whether it was right — ADR-0053 §3, layer three.

The deck's review tool (slide 62) in four rules, each one a decision here:

1. *One screen with enough context* — `run_detail` returns the question,
   the transcript, the answer, every score and every label in one payload.
2. *Domain words, no AI jargon* — the transcript is served as it is stored;
   the page renders tool lines as "gọi X với … → kết quả", not as spans.
3. *Evidence before the machine's opinion* (slide 61: a score shown first
   drags the person toward it, and the downside is four times the upside)
   — `judge_verdicts` rides along, but the page keeps it behind the person's
   own decision; `RunDetail.judge_hidden_until_labelled` says so.
4. *One binary decision* — `label_run` takes `passed`, a list of trace codes
   from `TRACE_CODES`, and a note. Append-only.

`next_to_review` is the sampler for the expert's queue. It prefers runs the
judge has already scored (each label there is also a calibration point —
one hour of review reused three times, slide 64), then runs an end-user
thumbed down, then anything recent and unlabelled.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from src.db.models import AgentRun, QualityScore, RunLabel, RunOutcome, RunTrigger, ScoreSource, User
from src.pm.judge import JUDGE_PREFIX
from src.pm.taxonomy import TRACE_CODES


class ReviewError(ValueError):
    """A label on something that is not there, or with words the taxonomy does not know."""


@dataclass(frozen=True, slots=True)
class ScoreLine:
    name: str
    value: float
    source: str
    comment: str | None
    given_by_email: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LabelLine:
    id: int
    passed: bool
    codes: list[str]
    note: str | None
    reviewer_email: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunDetail:
    id: int
    pipeline: str
    agent_key: str
    version_key: str
    agent_name: str
    agent_id: int | None
    version_id: int | None
    trace_id: str
    conversation_id: str
    caller: str | None
    trigger: str
    outcome: str
    error: str | None
    cost_usd: float
    latency_seconds: float
    tokens: int
    simulated: bool
    input_message: str | None
    answer: str | None
    transcript: list[dict[str, Any]]
    node_count: int
    tool_calls: int
    tool_failures: int
    actions: list[str]
    first_failure: str | None
    failure_node_type: str | None
    created_at: datetime
    #: Code scores and human thumbs, shown with the evidence.
    scores: list[ScoreLine] = field(default_factory=list)
    #: Judge verdicts, served separately so the page can hold them back.
    judge_verdicts: list[ScoreLine] = field(default_factory=list)
    labels: list[LabelLine] = field(default_factory=list)
    judge_hidden_until_labelled: bool = True
    #: How many unlabelled candidates remain — the "case 38 / 120" of slide 62.
    queue_remaining: int | None = None


def run_detail(db: Session, run_id: int, *, queue_remaining: int | None = None) -> RunDetail | None:
    run = db.get(AgentRun, run_id)
    if run is None:
        return None
    score_rows = db.execute(
        select(QualityScore, User.email)
        .outerjoin(User, User.id == QualityScore.given_by)
        .where(QualityScore.run_id == run.id)
        .order_by(QualityScore.created_at, QualityScore.id)
    ).all()
    scores: list[ScoreLine] = []
    judge: list[ScoreLine] = []
    for score, email in score_rows:
        line = ScoreLine(
            name=score.name,
            value=score.value,
            source=score.source.value,
            comment=score.comment,
            given_by_email=email,
            created_at=score.created_at,
        )
        (judge if score.source is ScoreSource.JUDGE else scores).append(line)
    label_rows = db.execute(
        select(RunLabel, User.email)
        .outerjoin(User, User.id == RunLabel.reviewer_id)
        .where(RunLabel.run_id == run.id)
        .order_by(RunLabel.created_at, RunLabel.id)
    ).all()
    return RunDetail(
        id=run.id,
        pipeline=run.pipeline,
        agent_key=run.agent_key,
        version_key=run.version_key,
        agent_name=run.agent_name,
        agent_id=run.agent_id,
        version_id=run.version_id,
        trace_id=run.trace_id,
        conversation_id=run.conversation_id,
        caller=run.caller,
        trigger=run.trigger,
        outcome=run.outcome,
        error=run.error,
        cost_usd=run.cost_usd,
        latency_seconds=run.latency_seconds,
        tokens=run.input_tokens + run.output_tokens,
        simulated=run.simulated,
        input_message=run.input_message,
        answer=run.answer,
        transcript=list(run.transcript or []),
        node_count=run.node_count,
        tool_calls=run.tool_calls,
        tool_failures=run.tool_failures,
        actions=list(run.actions or []),
        first_failure=run.first_failure,
        failure_node_type=run.failure_node_type,
        created_at=run.created_at,
        scores=scores,
        judge_verdicts=judge,
        labels=[
            LabelLine(
                id=label.id,
                passed=label.passed,
                codes=list(label.codes or []),
                note=label.note,
                reviewer_email=email,
                created_at=label.created_at,
            )
            for label, email in label_rows
        ],
        judge_hidden_until_labelled=not label_rows,
        queue_remaining=queue_remaining,
    )


def _unlabelled() -> Any:
    return ~exists().where(RunLabel.run_id == AgentRun.id)


def _candidates(db: Session, *, include_simulated: bool) -> Any:
    """Production runs with a transcript and no label — what there is to review."""
    conditions = [
        AgentRun.trigger == RunTrigger.MANUAL.value,
        AgentRun.node_count > 0,
        _unlabelled(),
    ]
    if not include_simulated:
        conditions.append(AgentRun.simulated.is_(False))
    return conditions


def queue_size(db: Session, *, include_simulated: bool = False) -> int:
    from sqlalchemy import func

    return int(
        db.scalar(select(func.count(AgentRun.id)).where(*_candidates(db, include_simulated=include_simulated))) or 0
    )


def next_to_review(db: Session, *, include_simulated: bool = False, agent_key: str | None = None) -> RunDetail | None:
    """The next run worth a person's minute: judged first, then thumbed-down, then newest."""
    base = _candidates(db, include_simulated=include_simulated)
    if agent_key:
        base.append(AgentRun.agent_key == agent_key)
    judged = exists().where(QualityScore.run_id == AgentRun.id, QualityScore.source == ScoreSource.JUDGE)
    thumbed_down = exists().where(
        QualityScore.run_id == AgentRun.id,
        QualityScore.source == ScoreSource.HUMAN,
        QualityScore.name == "thumbs_up",
        QualityScore.value < 1,
    )
    failed = AgentRun.outcome != RunOutcome.SUCCESS.value
    for preference in (judged, thumbed_down, failed, None):
        statement = select(AgentRun.id).where(*base)
        if preference is not None:
            statement = statement.where(preference)
        run_id = db.execute(
            statement.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(1)
        ).scalar_one_or_none()
        if run_id is not None:
            return run_detail(db, int(run_id), queue_remaining=queue_size(db, include_simulated=include_simulated))
    return None


def label_run(
    db: Session, *, run_id: int, reviewer_id: int | None, passed: bool, codes: Sequence[str], note: str | None
) -> RunLabel:
    """One expert verdict on one run. Codes must be in the taxonomy; a failed run with no code gets `other`."""
    run = db.get(AgentRun, run_id)
    if run is None:
        raise ReviewError(f"Không có lần chạy #{run_id}")
    chosen = [c.strip() for c in codes if c and c.strip()]
    unknown = [c for c in chosen if c not in TRACE_CODES]
    if unknown:
        raise ReviewError(f"Trace code không có trong từ vựng: {', '.join(unknown)} — thêm vào src.pm.taxonomy trước")
    if not passed and not chosen:
        chosen = ["other"]
    label = RunLabel(
        run_id=run.id,
        reviewer_id=reviewer_id,
        passed=passed,
        codes=chosen if not passed else [],
        note=(note or "").strip() or None,
    )
    db.add(label)
    db.commit()
    db.refresh(label)
    return label


def judge_verdict_names() -> tuple[str, ...]:
    return (JUDGE_PREFIX,)
