"""The PM board's widgets, as queries over the shared ledger — ADR-0053 §4.

Every function takes a `Session`, a window and `include_simulated`, and
returns plain dataclasses; the route serialises, the page draws. Nothing
here is filtered in Python that the database could filter (the rule
`src.deployments.operations` keeps): a widget that summed rows it had loaded
would be a second population the moment the list is capped. The exceptions
are the JSON columns (`usage_by_model`, `actions`, `codes`), folded in Python
over a window of rows because teaching every dialect to unnest JSON is
dearer than the fold.

**What is counted.** A run is an `agent_runs` row — one per leg, every
pipeline, trial and production alike. The widgets the deck says must keep
rehearsals apart from traffic group on `trigger`; the ones about quality
read `quality_scores` through `run_id` and `run_labels`, so a judge's
verdict, a person's thumbs and an expert's label on the *same* run sit in
the same row of the "offline vs online" table — which is the table the
deck's four production questions (slide 19) are answered from.

Simulated rows are out unless asked for. They used to be counted and
announced; now that real runs exist, a board that averaged 1,200 invented
runs into 200 real ones would be describing the seed script.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from src.db.models import (
    Agent,
    AgentRun,
    AgentVersion,
    Deployment,
    EvalRun,
    MetaAgentGateRun,
    QualityScore,
    RunApproval,
    RunApprovalStatus,
    RunLabel,
    RunOutcome,
    RunTrigger,
    ScoreSource,
    UC1EvalRun,
    User,
)
from src.pm.judge import JUDGE_PASS
from src.pm.ledger import TOOL_GROUNDEDNESS
from src.pm.thresholds import PM_THRESHOLDS, Thresholds

#: Ratio of `spec_error` to all classified errors the playbook's widget 2 reads
#: as "is the self-fix loop working". Below this, most failures need the
#: platform team, and the creator's own loop is not carrying its share.
SELF_FIX_HEALTHY_RATIO = 0.6

PENDING_LIST_LIMIT = 30
RECENT_RUNS_LIMIT = 60

_SUCCESS = RunOutcome.SUCCESS.value
_MANUAL = RunTrigger.MANUAL.value
THUMBS = "thumbs_up"


def _since(window_days: int) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=window_days)


def _scope(window_days: int, include_simulated: bool) -> list[Any]:
    conditions: list[Any] = [AgentRun.created_at >= _since(window_days)]
    if not include_simulated:
        conditions.append(AgentRun.simulated.is_(False))
    return conditions


def _success_case() -> Any:
    return case((AgentRun.outcome == _SUCCESS, 1), else_=0)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


# -- 1. Run success rate by trigger ----------------------------------------- #


@dataclass(frozen=True, slots=True)
class TriggerRate:
    trigger: str
    runs: int
    successes: int
    simulated: int

    @property
    def rate(self) -> float | None:
        return _ratio(self.successes, self.runs)


def success_by_trigger(db: Session, *, window_days: int, include_simulated: bool = False) -> list[TriggerRate]:
    """Widget 1. One line per `trigger`: a creator rehearsing and a real caller are two populations."""
    rows = db.execute(
        select(
            AgentRun.trigger,
            func.count(AgentRun.id),
            func.sum(_success_case()),
            func.sum(case((AgentRun.simulated.is_(True), 1), else_=0)),
        )
        .where(*_scope(window_days, include_simulated))
        .group_by(AgentRun.trigger)
        .order_by(AgentRun.trigger)
    ).all()
    return [
        TriggerRate(trigger=trigger, runs=int(runs), successes=int(ok or 0), simulated=int(sim or 0))
        for trigger, runs, ok, sim in rows
    ]


# -- 2. Outcome breakdown --------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OutcomeSlice:
    outcome: str
    runs: int


@dataclass(frozen=True, slots=True)
class OutcomeBreakdown:
    slices: list[OutcomeSlice]
    self_fix_ratio: float | None
    unclassified_errors: int
    #: Slide 33's "lỗi chìm dưới mặt nước": runs that ended `success` while a
    #: tool they called came back failed. The answer read fine; the trace did not.
    success_with_tool_failures: int


def outcome_breakdown(db: Session, *, window_days: int, include_simulated: bool = False) -> OutcomeBreakdown:
    """Widget 2. Every `outcome` word and its count, the self-fix ratio, and the hidden failures."""
    scope = _scope(window_days, include_simulated)
    rows = db.execute(
        select(AgentRun.outcome, func.count(AgentRun.id))
        .where(*scope)
        .group_by(AgentRun.outcome)
        .order_by(func.count(AgentRun.id).desc())
    ).all()
    counts = {outcome: int(n) for outcome, n in rows}
    spec = counts.get(RunOutcome.SPEC_ERROR.value, 0)
    system = counts.get(RunOutcome.SYSTEM_ERROR.value, 0)
    hidden = int(
        db.scalar(
            select(func.count(AgentRun.id)).where(*scope, AgentRun.outcome == _SUCCESS, AgentRun.tool_failures > 0)
        )
        or 0
    )
    return OutcomeBreakdown(
        slices=[OutcomeSlice(outcome=o, runs=n) for o, n in counts.items()],
        self_fix_ratio=_ratio(spec, spec + system),
        unclassified_errors=counts.get(RunOutcome.ERROR.value, 0),
        success_with_tool_failures=hidden,
    )


# -- 3. Cost, latency per agent --------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AgentCost:
    agent_key: str
    agent_name: str
    pipeline: str
    runs: int
    successes: int
    cost_usd: float
    latency_p50: float | None
    latency_p90: float | None
    deployment_status: str | None
    agent_id: int | None

    @property
    def cost_per_run(self) -> float | None:
        return self.cost_usd / self.runs if self.runs else None

    @property
    def cost_per_success(self) -> float | None:
        return self.cost_usd / self.successes if self.successes else None


@dataclass(frozen=True, slots=True)
class ModelCost:
    model: str
    runs: int
    cost_usd: float
    tokens: int


def cost_by_agent(db: Session, *, window_days: int, include_simulated: bool = False) -> list[AgentCost]:
    """Widget 3a. Dearest agent first, with the two latency quantiles the deck's threshold checks read (slide 45)."""
    p50 = func.percentile_cont(0.5).within_group(AgentRun.latency_seconds)
    p90 = func.percentile_cont(0.9).within_group(AgentRun.latency_seconds)
    rows = db.execute(
        select(
            AgentRun.agent_key,
            func.max(AgentRun.agent_name),
            func.max(AgentRun.pipeline),
            func.max(AgentRun.agent_id),
            func.count(AgentRun.id),
            func.sum(_success_case()),
            func.sum(AgentRun.cost_usd),
            p50,
            p90,
        )
        .where(*_scope(window_days, include_simulated))
        .group_by(AgentRun.agent_key)
        .order_by(func.sum(AgentRun.cost_usd).desc())
    ).all()
    agent_ids = [int(agent_id) for _, _, _, agent_id, *_ in rows if agent_id is not None]
    statuses: dict[int, str] = {}
    if agent_ids:
        for agent_id, status_ in db.execute(
            select(Deployment.agent_id, Deployment.status).where(Deployment.agent_id.in_(agent_ids))
        ):
            statuses[int(agent_id)] = status_.value
    return [
        AgentCost(
            agent_key=key,
            agent_name=name,
            pipeline=pipeline,
            runs=int(runs),
            successes=int(ok or 0),
            cost_usd=float(usd or 0.0),
            latency_p50=float(lat50) if lat50 is not None else None,
            latency_p90=float(lat90) if lat90 is not None else None,
            deployment_status=statuses.get(int(agent_id)) if agent_id is not None else None,
            agent_id=int(agent_id) if agent_id is not None else None,
        )
        for key, name, pipeline, agent_id, runs, ok, usd, lat50, lat90 in rows
    ]


def cost_by_model(db: Session, *, window_days: int, include_simulated: bool = False) -> list[ModelCost]:
    """Widget 3b. `usage_by_model` is a JSON map per run, folded in Python."""
    rows = db.execute(select(AgentRun.usage_by_model).where(*_scope(window_days, include_simulated))).scalars()
    totals: dict[str, list[float]] = {}
    for usage in rows:
        for model, figures in (usage or {}).items():
            slot = totals.setdefault(model, [0, 0.0, 0])
            slot[0] += 1
            slot[1] += float(figures.get("usd", 0.0) or 0.0)
            slot[2] += int(figures.get("input_tokens", 0) or 0) + int(figures.get("output_tokens", 0) or 0)
    return sorted(
        (ModelCost(model=m, runs=int(r), cost_usd=usd, tokens=int(t)) for m, (r, usd, t) in totals.items()),
        key=lambda entry: entry.cost_usd,
        reverse=True,
    )


# -- 4. Offline vs online, per version -------------------------------------- #


@dataclass(frozen=True, slots=True)
class Rate:
    """A ratio with the count it was taken over — never the one without the other (slide 18)."""

    n: int = 0
    passed: int = 0

    @property
    def rate(self) -> float | None:
        return _ratio(self.passed, self.n)


@dataclass(frozen=True, slots=True)
class VersionQuality:
    agent_key: str
    version_key: str
    agent_name: str
    pipeline: str
    agent_id: int | None
    version_id: int | None
    deployed: bool
    #: Offline — the reference set, before release (eval_runs / uc1_eval_runs, latest run).
    offline: Rate
    offline_threshold: float | None
    offline_previous: Rate | None
    #: Online — production traffic in the window.
    online: Rate
    judge: Rate
    thumbs: Rate
    expert: Rate
    tool_grounded: Rate
    regressed: bool
    diverged: bool


def quality_by_version(
    db: Session, *, window_days: int, include_simulated: bool = False, thresholds: Thresholds = PM_THRESHOLDS
) -> list[VersionQuality]:
    """Widget 4. One row per version with any traffic in the window or any deployment, offline beside online.

    Q2 of slide 19 — *offline và online score có đang diverge?* — is a
    subtraction on this row, and it is only possible because `eval_runs`
    (what the version scored before release) and `agent_runs` (what it did
    after) are keyed by the same version. `regressed` compares the offline
    rate with the previous version of the same agent; `diverged` compares
    offline with online on this one.
    """
    scope = _scope(window_days, include_simulated)
    key = (AgentRun.agent_key, AgentRun.version_key)

    traffic = db.execute(
        select(
            *key,
            func.max(AgentRun.agent_name),
            func.max(AgentRun.pipeline),
            func.max(AgentRun.agent_id),
            func.max(AgentRun.version_id),
            func.count(AgentRun.id),
            func.sum(_success_case()),
        )
        .where(*scope, AgentRun.trigger == _MANUAL)
        .group_by(*key)
    ).all()
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for agent_key, version_key, name, pipeline, agent_id, version_id, runs, ok in traffic:
        rows[(agent_key, version_key)] = {
            "agent_name": name,
            "pipeline": pipeline,
            "agent_id": agent_id,
            "version_id": version_id,
            "online": Rate(int(runs), int(ok or 0)),
        }

    # Deployed V2 versions with no traffic in the window still get a row:
    # "what would this version score" is a question about the thing serving.
    for agent_id, version_id, name in db.execute(
        select(Deployment.agent_id, Deployment.agent_version_id, Agent.name).join(Agent, Agent.id == Deployment.agent_id)
    ):
        k = (str(agent_id), str(version_id))
        rows.setdefault(
            k, {"agent_name": name, "pipeline": "v2", "agent_id": agent_id, "version_id": version_id, "online": Rate()}
        )
        rows[k]["deployed"] = True

    # Scores and labels on the runs of each version, in the window.
    score_rows = db.execute(
        select(
            *key, QualityScore.name, func.count(QualityScore.id), func.sum(case((QualityScore.value >= 1, 1), else_=0))
        )
        .join(AgentRun, AgentRun.id == QualityScore.run_id)
        .where(*scope, QualityScore.name.in_((JUDGE_PASS, THUMBS, TOOL_GROUNDEDNESS)))
        .group_by(*key, QualityScore.name)
    ).all()
    scores: dict[tuple[str, str, str], Rate] = {
        (a, v, name): Rate(int(n), int(ok or 0)) for a, v, name, n, ok in score_rows
    }
    label_rows = db.execute(
        select(*key, func.count(RunLabel.id), func.sum(case((RunLabel.passed.is_(True), 1), else_=0)))
        .join(AgentRun, AgentRun.id == RunLabel.run_id)
        .where(*scope)
        .group_by(*key)
    ).all()
    labels: dict[tuple[str, str], Rate] = {(a, v): Rate(int(n), int(ok or 0)) for a, v, n, ok in label_rows}

    offline = _offline_rates(db, rows)

    out: list[VersionQuality] = []
    for (agent_key, version_key), row in rows.items():
        off = offline.get((agent_key, version_key))
        prev = _previous_offline(offline, agent_key, version_key, row.get("pipeline") or "v2")
        online: Rate = row["online"]
        off_rate = off[0].rate if off else None
        prev_rate = prev.rate if prev else None
        regressed = off_rate is not None and prev_rate is not None and off_rate < prev_rate - thresholds.regression_drop
        diverged = (
            off_rate is not None
            and online.rate is not None
            and online.n >= 10
            and abs(off_rate - online.rate) > thresholds.offline_online_gap_max
        )
        out.append(
            VersionQuality(
                agent_key=agent_key,
                version_key=version_key,
                agent_name=row["agent_name"],
                pipeline=row.get("pipeline") or "v2",
                agent_id=row.get("agent_id"),
                version_id=row.get("version_id"),
                deployed=bool(row.get("deployed")),
                offline=off[0] if off else Rate(),
                offline_threshold=off[1] if off else None,
                offline_previous=prev,
                online=online,
                judge=scores.get((agent_key, version_key, JUDGE_PASS), Rate()),
                thumbs=scores.get((agent_key, version_key, THUMBS), Rate()),
                expert=labels.get((agent_key, version_key), Rate()),
                tool_grounded=scores.get((agent_key, version_key, TOOL_GROUNDEDNESS), Rate()),
                regressed=regressed,
                diverged=diverged,
            )
        )
    return sorted(out, key=lambda v: (v.agent_name, -_int_or(v.version_key)))


def _int_or(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _offline_rates(
    db: Session, rows: dict[tuple[str, str], dict[str, Any]]
) -> dict[tuple[str, str], tuple[Rate, float | None]]:
    """Latest offline eval per version — V2 from `eval_runs`, UC-1 from `uc1_eval_runs` — plus every other version of the same agents, for the previous-version comparison."""
    out: dict[tuple[str, str], tuple[Rate, float | None]] = {}
    v2_agents = {int(r["agent_id"]) for r in rows.values() if r.get("agent_id") is not None}
    if v2_agents:
        latest = (
            select(EvalRun.version_id, func.max(EvalRun.id).label("id"))
            .join(AgentVersion, AgentVersion.id == EvalRun.version_id)
            .where(AgentVersion.agent_id.in_(v2_agents))
            .group_by(EvalRun.version_id)
            .subquery()
        )
        for agent_id, version_id, passed, total, threshold in db.execute(
            select(
                AgentVersion.agent_id, EvalRun.version_id, EvalRun.passed_count, EvalRun.total_count, EvalRun.threshold
            )
            .join(latest, latest.c.id == EvalRun.id)
            .join(AgentVersion, AgentVersion.id == EvalRun.version_id)
        ):
            out[(str(agent_id), str(version_id))] = (Rate(int(total), int(passed)), float(threshold))
    uc1_agents = {r_key[0] for r_key, r in rows.items() if (r.get("pipeline") or "") == "uc1"}
    if uc1_agents:
        latest_uc1 = (
            select(UC1EvalRun.agent_id, UC1EvalRun.version, func.max(UC1EvalRun.id).label("id"))
            .where(UC1EvalRun.agent_id.in_(uc1_agents))
            .group_by(UC1EvalRun.agent_id, UC1EvalRun.version)
            .subquery()
        )
        for agent_id, version, passed, total in db.execute(
            select(UC1EvalRun.agent_id, UC1EvalRun.version, UC1EvalRun.passed_count, UC1EvalRun.total_count).join(
                latest_uc1, latest_uc1.c.id == UC1EvalRun.id
            )
        ):
            out[(str(agent_id), str(version))] = (Rate(int(total), int(passed)), None)
    return out


def _previous_offline(
    offline: dict[tuple[str, str], tuple[Rate, float | None]], agent_key: str, version_key: str, pipeline: str
) -> Rate | None:
    """The offline rate of the newest version of this agent older than `version_key` — the baseline a candidate is read against (slide 18)."""
    current = _int_or(version_key)
    older = [(_int_or(v), rate) for (a, v), (rate, _) in offline.items() if a == agent_key and _int_or(v) < current]
    if not older:
        return None
    return max(older, key=lambda pair: pair[0])[1]


# -- 5. Approval funnel (V2) ------------------------------------------------ #


@dataclass(frozen=True, slots=True)
class PendingApproval:
    id: int
    agent_id: int
    agent_name: str
    version_id: int
    node_id: str
    approver_role: str
    message: str
    input_message: str
    sandbox: bool
    simulated: bool
    waiting_minutes: float
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalFunnel:
    pending: int
    decided: int
    approved: int
    rejected: int
    avg_wait_minutes: float | None
    p90_wait_minutes: float | None
    oldest_pending_minutes: float | None
    pending_list: list[PendingApproval] = field(default_factory=list)

    @property
    def approval_rate(self) -> float | None:
        return _ratio(self.approved, self.decided)


def approval_funnel(db: Session, *, window_days: int, include_simulated: bool = False) -> ApprovalFunnel:
    """Widget 5. How many runs wait on a person, how long, how often the answer is yes. V2's `run_approvals`; UC-1's action queue is its own table and not yet here (ADR-0053 §6)."""
    since = _since(window_days)
    now = datetime.now(UTC).replace(tzinfo=None)
    sim = RunApproval.conversation_id.like("sim-%")
    not_sim = [] if include_simulated else [~sim]

    pending = int(
        db.scalar(select(func.count(RunApproval.id)).where(RunApproval.status == RunApprovalStatus.PENDING, *not_sim))
        or 0
    )
    decided_rows = db.execute(
        select(RunApproval.status, RunApproval.created_at, RunApproval.decided_at).where(
            RunApproval.status != RunApprovalStatus.PENDING, RunApproval.created_at >= since, *not_sim
        )
    ).all()
    waits = sorted(
        (decided_at - created_at).total_seconds() / 60
        for _status, created_at, decided_at in decided_rows
        if decided_at is not None and created_at is not None
    )
    approved = sum(1 for status_, _, _ in decided_rows if status_ is RunApprovalStatus.APPROVED)
    rejected = sum(1 for status_, _, _ in decided_rows if status_ is RunApprovalStatus.REJECTED)
    oldest = db.scalar(
        select(func.min(RunApproval.created_at)).where(RunApproval.status == RunApprovalStatus.PENDING, *not_sim)
    )
    pending_rows = db.execute(
        select(RunApproval, Agent.name, sim)
        .join(Agent, Agent.id == RunApproval.agent_id)
        .where(RunApproval.status == RunApprovalStatus.PENDING, *not_sim)
        .order_by(RunApproval.created_at, RunApproval.id)
        .limit(PENDING_LIST_LIMIT)
    ).all()
    return ApprovalFunnel(
        pending=pending,
        decided=len(decided_rows),
        approved=approved,
        rejected=rejected,
        avg_wait_minutes=(sum(waits) / len(waits)) if waits else None,
        p90_wait_minutes=waits[min(len(waits) - 1, int(0.9 * len(waits)))] if waits else None,
        oldest_pending_minutes=((now - oldest).total_seconds() / 60) if oldest else None,
        pending_list=[
            PendingApproval(
                id=row.id,
                agent_id=row.agent_id,
                agent_name=name,
                version_id=row.version_id,
                node_id=row.node_id,
                approver_role=row.approver_role,
                message=row.message,
                input_message=row.input_message,
                sandbox=row.sandbox,
                simulated=bool(simulated),
                waiting_minutes=(now - row.created_at).total_seconds() / 60 if row.created_at else 0.0,
                created_at=row.created_at,
            )
            for row, name, simulated in pending_rows
        ],
    )


# -- 6. Feedback ------------------------------------------------------------ #


@dataclass(frozen=True, slots=True)
class FeedbackWeek:
    week_start: datetime
    thumbs_up: int
    thumbs_down: int

    @property
    def ratio(self) -> float | None:
        return _ratio(self.thumbs_up, self.thumbs_up + self.thumbs_down)


@dataclass(frozen=True, slots=True)
class FeedbackNote:
    score_id: int
    run_id: int | None
    agent_name: str
    version_key: str
    thumbs_up: bool
    comment: str
    given_by_email: str | None
    created_at: datetime


def feedback_by_week(db: Session, *, window_days: int, include_simulated: bool = False) -> list[FeedbackWeek]:
    """Widget 6a. Thumbs per ISO week — from the chat window and from the board alike."""
    week = func.date_trunc("week", QualityScore.created_at)
    rows = db.execute(
        select(
            week,
            func.sum(case((QualityScore.value >= 1, 1), else_=0)),
            func.sum(case((QualityScore.value < 1, 1), else_=0)),
        )
        .join(AgentRun, AgentRun.id == QualityScore.run_id)
        .where(
            QualityScore.name == THUMBS,
            QualityScore.source == ScoreSource.HUMAN,
            QualityScore.created_at >= _since(window_days),
            *([] if include_simulated else [AgentRun.simulated.is_(False)]),
        )
        .group_by(week)
        .order_by(week)
    ).all()
    return [
        FeedbackWeek(week_start=start, thumbs_up=int(up or 0), thumbs_down=int(down or 0)) for start, up, down in rows
    ]


def recent_feedback_notes(db: Session, *, limit: int = 12, include_simulated: bool = False) -> list[FeedbackNote]:
    """Widget 6b. The newest thumbs that came with words."""
    rows = db.execute(
        select(QualityScore, AgentRun.agent_name, AgentRun.version_key, User.email)
        .join(AgentRun, AgentRun.id == QualityScore.run_id)
        .outerjoin(User, User.id == QualityScore.given_by)
        .where(
            QualityScore.name == THUMBS,
            QualityScore.source == ScoreSource.HUMAN,
            QualityScore.comment.isnot(None),
            QualityScore.comment != "",
            *([] if include_simulated else [AgentRun.simulated.is_(False)]),
        )
        .order_by(QualityScore.created_at.desc(), QualityScore.id.desc())
        .limit(limit)
    ).all()
    return [
        FeedbackNote(
            score_id=score.id,
            run_id=score.run_id,
            agent_name=name,
            version_key=version_key,
            thumbs_up=score.value >= 1,
            comment=score.comment or "",
            given_by_email=email,
            created_at=score.created_at,
        )
        for score, name, version_key, email in rows
    ]


# -- 7. Where failures start ------------------------------------------------ #


@dataclass(frozen=True, slots=True)
class SliceCount:
    key: str
    runs: int


@dataclass(frozen=True, slots=True)
class FailureSlices:
    """Slide 47: *failures tập trung ở đâu?* — three cuts over the runs that went wrong."""

    by_node_type: list[SliceCount]
    by_action: list[SliceCount]
    by_trace_code: list[SliceCount]
    failed_runs: int


def failure_slices(db: Session, *, window_days: int, include_simulated: bool = False) -> FailureSlices:
    """A run "went wrong" when it did not end `success`, or ended `success` with a failed tool, or an expert said no."""
    scope = _scope(window_days, include_simulated)
    wrong = (AgentRun.outcome != _SUCCESS) | (AgentRun.tool_failures > 0)
    failed = int(db.scalar(select(func.count(AgentRun.id)).where(*scope, wrong)) or 0)
    by_type = db.execute(
        select(func.coalesce(AgentRun.failure_node_type, "(không rõ)"), func.count(AgentRun.id))
        .where(*scope, wrong)
        .group_by(AgentRun.failure_node_type)
        .order_by(func.count(AgentRun.id).desc())
    ).all()
    actions: dict[str, int] = {}
    for acts in db.execute(select(AgentRun.actions).where(*scope, wrong)).scalars():
        for action in acts or []:
            actions[action] = actions.get(action, 0) + 1
    codes: dict[str, int] = {}
    for run_codes in db.execute(
        select(RunLabel.codes).join(AgentRun, AgentRun.id == RunLabel.run_id).where(*scope, RunLabel.passed.is_(False))
    ).scalars():
        for code in run_codes or []:
            codes[code] = codes.get(code, 0) + 1
    return FailureSlices(
        by_node_type=[SliceCount(key=str(k), runs=int(n)) for k, n in by_type],
        by_action=[SliceCount(key=k, runs=n) for k, n in sorted(actions.items(), key=lambda kv: -kv[1])],
        by_trace_code=[SliceCount(key=k, runs=n) for k, n in sorted(codes.items(), key=lambda kv: -kv[1])],
        failed_runs=failed,
    )


# -- Recent runs (the list the levers act on) ------------------------------- #


@dataclass(frozen=True, slots=True)
class RecentRun:
    id: int
    pipeline: str
    agent_key: str
    agent_name: str
    version_key: str
    agent_id: int | None
    version_id: int | None
    conversation_id: str
    trigger: str
    outcome: str
    error: str | None
    cost_usd: float
    latency_seconds: float
    tokens: int
    simulated: bool
    has_trace: bool
    tool_calls: int
    tool_failures: int
    failure_node_type: str | None
    input_message: str | None
    thumbs: int | None
    judge_pass: int | None
    tool_grounded: int | None
    expert_passed: bool | None
    expert_codes: list[str]
    created_at: datetime


def recent_runs(
    db: Session,
    *,
    window_days: int,
    include_simulated: bool = False,
    outcome: str | None = None,
    trigger: str | None = None,
    agent_key: str | None = None,
    failure_node_type: str | None = None,
    needs_review: bool = False,
    limit: int = RECENT_RUNS_LIMIT,
) -> list[RecentRun]:
    """The runs behind the numbers, newest first, with every verdict joined on so the page shows them without a request per row."""
    conditions = _scope(window_days, include_simulated)
    if outcome:
        conditions.append(AgentRun.outcome == outcome)
    if trigger:
        conditions.append(AgentRun.trigger == trigger)
    if agent_key:
        conditions.append(AgentRun.agent_key == agent_key)
    if failure_node_type:
        conditions.append(AgentRun.failure_node_type == failure_node_type)
    if needs_review:
        labelled = select(RunLabel.run_id).where(RunLabel.run_id == AgentRun.id).exists()
        conditions.append(~labelled)
    rows = list(
        db.execute(
            select(AgentRun)
            .where(and_(*conditions))
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(limit)
        ).scalars()
    )
    return decorate_runs(db, rows)


def decorate_runs(db: Session, rows: Sequence[AgentRun]) -> list[RecentRun]:
    """`rows` with their newest thumbs, judge verdict, code check and expert label attached."""
    ids = [row.id for row in rows]
    verdicts: dict[tuple[int, str], float] = {}
    labels: dict[int, RunLabel] = {}
    if ids:
        for run_id, name, value in db.execute(
            select(QualityScore.run_id, QualityScore.name, QualityScore.value)
            .where(QualityScore.run_id.in_(ids), QualityScore.name.in_((THUMBS, JUDGE_PASS, TOOL_GROUNDEDNESS)))
            .order_by(QualityScore.created_at, QualityScore.id)
        ):
            verdicts[(int(run_id), name)] = float(value)  # later rows overwrite: newest verdict wins
        for label in db.execute(
            select(RunLabel).where(RunLabel.run_id.in_(ids)).order_by(RunLabel.created_at, RunLabel.id)
        ).scalars():
            labels[label.run_id] = label

    def verdict(run_id: int, name: str) -> int | None:
        value = verdicts.get((run_id, name))
        return None if value is None else int(value >= 1)

    return [
        RecentRun(
            id=row.id,
            pipeline=row.pipeline,
            agent_key=row.agent_key,
            agent_name=row.agent_name,
            version_key=row.version_key,
            agent_id=row.agent_id,
            version_id=row.version_id,
            conversation_id=row.conversation_id,
            trigger=row.trigger,
            outcome=row.outcome,
            error=row.error,
            cost_usd=row.cost_usd,
            latency_seconds=row.latency_seconds,
            tokens=row.input_tokens + row.output_tokens,
            simulated=row.simulated,
            has_trace=bool(row.transcript),
            tool_calls=row.tool_calls,
            tool_failures=row.tool_failures,
            failure_node_type=row.failure_node_type,
            input_message=row.input_message,
            thumbs=verdict(row.id, THUMBS),
            judge_pass=verdict(row.id, JUDGE_PASS),
            tool_grounded=verdict(row.id, TOOL_GROUNDEDNESS),
            expert_passed=labels[row.id].passed if row.id in labels else None,
            expert_codes=list(labels[row.id].codes or []) if row.id in labels else [],
            created_at=row.created_at,
        )
        for row in rows
    ]


# -- Activation funnel + gate history --------------------------------------- #


@dataclass(frozen=True, slots=True)
class ActivationFunnel:
    agents: int
    with_confirmed_workflow: int
    with_trial_run: int
    published: int
    with_production_run: int


def activation_funnel(db: Session, *, include_simulated: bool = False) -> ActivationFunnel:
    """Playbook §5's event-measured funnel, V2 agents. Ends at "somebody other than the owner used it"."""
    confirmed_statuses = ("workflow_confirmed", "draft", "sandbox_passed", "pending_approval", "approved")
    sim = [] if include_simulated else [AgentRun.simulated.is_(False)]
    agents = int(
        db.scalar(select(func.count(Agent.id)).where(*([] if include_simulated else [~Agent.name.like("[SIM] %")])))
        or 0
    )
    confirmed = int(
        db.scalar(
            select(func.count(func.distinct(AgentVersion.agent_id))).where(AgentVersion.status.in_(confirmed_statuses))
        )
        or 0
    )
    trial = int(
        db.scalar(
            select(func.count(func.distinct(AgentRun.agent_key))).where(
                AgentRun.trigger == RunTrigger.CHAT_TEST.value, AgentRun.pipeline == "v2", *sim
            )
        )
        or 0
    )
    published = int(db.scalar(select(func.count(Deployment.id))) or 0)
    production = int(
        db.scalar(
            select(func.count(func.distinct(AgentRun.agent_key))).where(
                AgentRun.trigger == _MANUAL, AgentRun.pipeline == "v2", *sim
            )
        )
        or 0
    )
    return ActivationFunnel(
        agents=agents,
        with_confirmed_workflow=confirmed,
        with_trial_run=trial,
        published=published,
        with_production_run=production,
    )


@dataclass(frozen=True, slots=True)
class GateRunSummary:
    id: int
    mode: str
    passed: bool
    per_family: dict[str, Any]
    thresholds: dict[str, Any]
    failure_classes: dict[str, int]
    created_at: datetime


def gate_history(db: Session, *, limit: int = 10) -> list[GateRunSummary]:
    """The last few gate runs, newest first. Failure classes are counted here rather than stored."""
    rows = db.execute(select(MetaAgentGateRun).order_by(MetaAgentGateRun.id.desc()).limit(limit)).scalars()
    out = []
    for row in rows:
        classes: dict[str, int] = {}
        for verdict in row.verdicts:
            if not verdict.get("passed"):
                label = str(verdict.get("classification") or "unclassified")
                classes[label] = classes.get(label, 0) + 1
        out.append(
            GateRunSummary(
                id=row.id,
                mode=row.mode,
                passed=row.passed,
                per_family=row.per_family,
                thresholds=row.thresholds,
                failure_classes=classes,
                created_at=row.created_at,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class SimulationFootprint:
    """How many invented rows exist, and whether the board is showing them."""

    simulated_runs: int
    total_runs: int
    simulated_pending_approvals: int
    shown: bool


def simulation_footprint(db: Session, *, include_simulated: bool = False) -> SimulationFootprint:
    total, simulated = db.execute(
        select(func.count(AgentRun.id), func.sum(case((AgentRun.simulated.is_(True), 1), else_=0)))
    ).one()
    pending_sim = db.scalar(
        select(func.count(RunApproval.id)).where(
            RunApproval.status == RunApprovalStatus.PENDING, RunApproval.conversation_id.like("sim-%")
        )
    )
    return SimulationFootprint(
        simulated_runs=int(simulated or 0),
        total_runs=int(total or 0),
        simulated_pending_approvals=int(pending_sim or 0),
        shown=include_simulated,
    )


# -- Traces: how much of the window has evidence ---------------------------- #


@dataclass(frozen=True, slots=True)
class TraceCoverage:
    """Runs in the window that left a transcript — the board says plainly which runs it can show the evidence for."""

    runs: int
    with_trace: int


def trace_coverage(db: Session, *, window_days: int, include_simulated: bool = False) -> TraceCoverage:
    runs, traced = db.execute(
        select(
            func.count(AgentRun.id),
            func.sum(case((AgentRun.node_count > 0, 1), else_=0)),
        ).where(*_scope(window_days, include_simulated))
    ).one()
    return TraceCoverage(runs=int(runs or 0), with_trace=int(traced or 0))
