"""The PM control room's API — `docs/PM-PLAYBOOK.md` §5 and ADR-0053 §4 behind `/pm/*`.

Gated by `require_pm` (`PM_CONTROL_ROOM_ROLES`: the `pm` role and the
admin-equivalents), for the reason `admin_routes` gives its cost board: every
number here crosses ownership on purpose. A creator sees their own agent's
operations on its Deploy tab; this surface is for the person who answers for
*all* of them and has to notice an agent misbehaving before its owner does —
and that person is the PM, not the platform admin (ADR-0051).

One `GET /pm/overview` carries every widget at once rather than a dozen
requests, because the page renders them together and a board whose tiles
arrive at different moments describes a dozen different instants. The levers
are separate `POST`s, each the smallest write that does the thing
(`src.pm.controls`, `src.pm.review`).

Every read takes `include_simulated` (default off): the seed script's rows
stay in the database for rehearsal, and stay out of the numbers unless asked.

**Resuming a decided run.** `POST /pm/agents/{id}/approvals/{id}` records the
decision synchronously and — when a real checkpoint is waiting behind the row
— resumes the graph **within the same request**, draining the same SSE
generator the owner's route streams to a browser. Not a background task: on
Cloud Run the CPU is throttled the moment a response is sent, and a resume
parked there would stall while the row already said "resumed".
"""

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.agents.runtime import Conversation
from src.api.agent_routes import Checkpointer, StartRun
from src.api.workflow_run_routes import Databases, Knowledge, _workflow_event_stream, _workflow_sandbox_stream
from src.auth.dependencies import PmUser
from src.db.base import get_db
from src.db.models import AgentVersion, MetaAgentGateRun
from src.models.schemas import UtcTimestamp
from src.pm import controls, gate, metrics, reports, review
from src.pm import judge as judge_module
from src.pm.taxonomy import TRACE_CODES
from src.pm.thresholds import PM_THRESHOLDS
from src.services.llm import ChatModelFactory, get_chat_model_factory
from src.services.platform_spend import spend_by_purpose
from src.services.tracing import trace_url_template
from src.workflow.approvals import ApprovalDecision, parked_leg
from src.workflow.generator import workflow_of
from src.workflow.runtime import Resumption, ensure_runnable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pm", tags=["pm"])

Db = Annotated[Session, Depends(get_db)]
ModelFactory = Annotated[ChatModelFactory, Depends(get_chat_model_factory)]

DEFAULT_WINDOW_DAYS = 28
MAX_WINDOW_DAYS = 365

WindowDays = Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)]
IncludeSimulated = Annotated[bool, Query()]


# -- Payloads ---------------------------------------------------------------- #


class RateOut(BaseModel):
    n: int
    passed: int
    rate: float | None


def _rate(rate: metrics.Rate | None) -> RateOut | None:
    return None if rate is None else RateOut(n=rate.n, passed=rate.passed, rate=rate.rate)


class TriggerRateOut(BaseModel):
    trigger: str
    runs: int
    successes: int
    simulated: int
    rate: float | None


class OutcomeSliceOut(BaseModel):
    outcome: str
    runs: int


class OutcomeBreakdownOut(BaseModel):
    slices: list[OutcomeSliceOut]
    self_fix_ratio: float | None
    self_fix_healthy_above: float
    unclassified_errors: int
    success_with_tool_failures: int


class AgentCostOut(BaseModel):
    agent_key: str
    agent_name: str
    pipeline: str
    agent_id: int | None
    runs: int
    successes: int
    cost_usd: float
    cost_per_run: float | None
    cost_per_success: float | None
    latency_p50: float | None
    latency_p90: float | None
    deployment_status: str | None


class ModelCostOut(BaseModel):
    model: str
    runs: int
    cost_usd: float
    tokens: int
    cost_per_run: float | None


class PlatformSpendOut(BaseModel):
    """One job the platform spends money on *for itself* — `platform_model_calls`.

    Beside `cost_by_model` rather than inside it, because the two answer
    different questions and mixing them would make both unreadable. That one is
    "what do the agents cost to **run**"; this is "what do they cost to
    **build**" — the interview, the composer, the eval-case writer, the judge.

    It exists because the board was confidently wrong. It reported $1.57 total
    while the OpenAI project behind it hit its enforced spend limit, and the
    whole of the difference was here: thirteen call sites on the platform's
    dearest model, retried up to three times a document, metered by nothing.
    """

    purpose: str
    calls: int
    usd: float
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    attempts_avg: float


class VersionQualityOut(BaseModel):
    agent_key: str
    version_key: str
    agent_name: str
    pipeline: str
    agent_id: int | None
    version_id: int | None
    deployed: bool
    offline: RateOut
    offline_threshold: float | None
    offline_previous: RateOut | None
    online: RateOut
    judge: RateOut
    thumbs: RateOut
    expert: RateOut
    tool_grounded: RateOut
    regressed: bool
    diverged: bool


class PendingApprovalOut(BaseModel):
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
    created_at: UtcTimestamp


class ApprovalFunnelOut(BaseModel):
    pending: int
    decided: int
    approved: int
    rejected: int
    approval_rate: float | None
    avg_wait_minutes: float | None
    p90_wait_minutes: float | None
    oldest_pending_minutes: float | None
    pending_list: list[PendingApprovalOut]


class FeedbackWeekOut(BaseModel):
    week_start: UtcTimestamp
    thumbs_up: int
    thumbs_down: int
    ratio: float | None


class FeedbackNoteOut(BaseModel):
    score_id: int
    run_id: int | None
    agent_name: str
    version_key: str
    thumbs_up: bool
    comment: str
    given_by_email: str | None
    created_at: UtcTimestamp


class SliceCountOut(BaseModel):
    key: str
    runs: int


class FailureSlicesOut(BaseModel):
    by_node_type: list[SliceCountOut]
    by_action: list[SliceCountOut]
    by_trace_code: list[SliceCountOut]
    failed_runs: int


class DisagreementOut(BaseModel):
    run_id: int
    agent_name: str
    judge_passed: bool
    expert_passed: bool
    judge_reason: str
    expert_note: str | None
    expert_codes: list[str]


class CalibrationOut(BaseModel):
    labelled: int
    tp: int
    fn: int
    fp: int
    tn: int
    tpr: float | None
    tnr: float | None
    agreement: float | None
    kappa: float | None
    trusted: bool
    min_labels: int
    tpr_min: float
    tnr_min: float
    disagreements: list[DisagreementOut]


class AlertOut(BaseModel):
    code: str
    severity: str
    message: str
    value: float | None
    threshold: float | None
    agent_key: str | None


class ActivationFunnelOut(BaseModel):
    agents: int
    with_confirmed_workflow: int
    with_trial_run: int
    published: int
    with_production_run: int


class GateRunOut(BaseModel):
    id: int
    mode: str
    passed: bool
    per_family: dict[str, Any]
    thresholds: dict[str, Any]
    failure_classes: dict[str, int]
    created_at: UtcTimestamp


class GateRunDetailOut(GateRunOut):
    verdicts: list[dict[str, Any]]
    report: str


class SimulationFootprintOut(BaseModel):
    simulated_runs: int
    total_runs: int
    simulated_pending_approvals: int
    shown: bool


class TraceCoverageOut(BaseModel):
    runs: int
    with_trace: int


class ThresholdsOut(BaseModel):
    production_success_min: float
    offline_online_gap_max: float
    thumbs_up_min: float
    latency_p90_max_seconds: float
    cost_per_run_max_usd: float
    judge_tpr_min: float
    judge_tnr_min: float
    calibration_min_labels: int
    noise_delta: float
    regression_drop: float
    judge_sample_rate: float
    small_n: int


class OverviewOut(BaseModel):
    window_days: int
    include_simulated: bool
    generated_at: UtcTimestamp
    thresholds: ThresholdsOut
    simulation: SimulationFootprintOut
    traces: TraceCoverageOut
    alerts: list[AlertOut]
    success_by_trigger: list[TriggerRateOut]
    outcomes: OutcomeBreakdownOut
    cost_by_agent: list[AgentCostOut]
    cost_by_model: list[ModelCostOut]
    platform_spend: list[PlatformSpendOut]
    quality_by_version: list[VersionQualityOut]
    approvals: ApprovalFunnelOut
    feedback_by_week: list[FeedbackWeekOut]
    feedback_notes: list[FeedbackNoteOut]
    failure_slices: FailureSlicesOut
    calibration: CalibrationOut
    review_queue: int
    activation: ActivationFunnelOut
    gate_history: list[GateRunOut]
    trace_codes: dict[str, str]


class RecentRunOut(BaseModel):
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
    created_at: UtcTimestamp


class ScoreLineOut(BaseModel):
    name: str
    value: float
    source: str
    comment: str | None
    given_by_email: str | None
    created_at: UtcTimestamp


class LabelLineOut(BaseModel):
    id: int
    passed: bool
    codes: list[str]
    note: str | None
    reviewer_email: str | None
    created_at: UtcTimestamp


class RunDetailOut(BaseModel):
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
    created_at: UtcTimestamp
    scores: list[ScoreLineOut]
    judge_verdicts: list[ScoreLineOut]
    labels: list[LabelLineOut]
    judge_hidden_until_labelled: bool
    queue_remaining: int | None
    #: Where the same run's Langfuse trace opens, when a backend is configured
    #: (`LANGFUSE_*` + `LANGFUSE_PROJECT_ID`). The ledger is the board's record;
    #: this is the magnifying glass for one run — same `trace_id`, two readers.
    trace_url: str | None


class CategoryLineOut(BaseModel):
    category: str
    baseline: RateOut | None
    candidate: RateOut
    delta: float | None
    noise: bool


class RegressionOut(BaseModel):
    input_message: str
    criterion: str
    reason: str


class VersionReportOut(BaseModel):
    agent_id: int
    agent_name: str
    version_id: int
    baseline_version_id: int | None
    threshold: float | None
    candidate: RateOut
    baseline: RateOut | None
    lines: list[CategoryLineOut]
    regressions: list[RegressionOut]
    online: RateOut
    judge: RateOut
    thumbs: RateOut
    expert: RateOut
    verdict: str
    notes: list[str]
    candidate_judge_model: str = ""
    baseline_judge_model: str = ""
    judge_changed: bool = False


class DeploymentStatusRequest(BaseModel):
    serving: bool


class DeploymentStatusOut(BaseModel):
    agent_id: int
    deployment_id: int
    status: str
    updated_at: UtcTimestamp


class FeedbackRequest(BaseModel):
    thumbs_up: bool
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackOut(BaseModel):
    score_id: int
    run_id: int
    thumbs_up: bool
    comment: str | None
    created_at: UtcTimestamp


class LabelRequest(BaseModel):
    passed: bool
    codes: list[str] = Field(default_factory=list, max_length=6)
    note: str | None = Field(default=None, max_length=2000)


class LabelOut(BaseModel):
    label_id: int
    run_id: int
    passed: bool
    codes: list[str]
    queue_remaining: int


class DecisionRequest(BaseModel):
    approved: bool
    reason: str | None = Field(default=None, max_length=2000)


class DecisionOut(BaseModel):
    approval_id: int
    status: str
    resumed: bool
    note: str


class PromoteRequest(BaseModel):
    input_message: str | None = Field(default=None, max_length=4000)
    expected: str = Field(min_length=1, max_length=4000)
    criterion: str | None = Field(default=None, max_length=500)


class PromoteOut(BaseModel):
    case_id: int | None
    version_id: int | None
    golden_line: str
    note: str


class JudgeSampleRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)
    agent_key: str | None = Field(default=None, max_length=120)


class CriterionVerdictOut(BaseModel):
    category: str
    criterion: str
    passed: bool
    reason: str


class JudgedRunOut(BaseModel):
    run_id: int
    agent_name: str
    passed: bool
    verdicts: list[CriterionVerdictOut]


class JudgeSampleOut(BaseModel):
    judged: list[JudgedRunOut]
    passed: int
    skipped: int


class GateRunRequest(BaseModel):
    mode: str = Field(default=gate.SIMULATED, pattern="^(simulated|real)$")
    fidelity: float = Field(default=gate.DEFAULT_FIDELITY, ge=0.0, le=1.0)
    seed: int | None = None
    repeats: int = Field(default=1, ge=1, le=5)


class GateRunBatchOut(BaseModel):
    runs: list[GateRunDetailOut]
    always_passed: list[str]
    unsatisfiable: list[str]


# -- Serialisers ------------------------------------------------------------- #


def _gate_out(row: metrics.GateRunSummary) -> GateRunOut:
    return GateRunOut(
        id=row.id,
        mode=row.mode,
        passed=row.passed,
        per_family=row.per_family,
        thresholds=row.thresholds,
        failure_classes=row.failure_classes,
        created_at=row.created_at,
    )


def _gate_detail(row: MetaAgentGateRun) -> GateRunDetailOut:
    classes: dict[str, int] = {}
    for verdict in row.verdicts:
        if not verdict.get("passed"):
            label = str(verdict.get("classification") or "unclassified")
            classes[label] = classes.get(label, 0) + 1
    return GateRunDetailOut(
        id=row.id,
        mode=row.mode,
        passed=row.passed,
        per_family=row.per_family,
        thresholds=row.thresholds,
        failure_classes=classes,
        created_at=row.created_at,
        verdicts=row.verdicts,
        report=gate.verdicts_as_text(row),
    )


def _run_out(r: metrics.RecentRun) -> RecentRunOut:
    return RecentRunOut(**{k: getattr(r, k) for k in RecentRunOut.model_fields})


def _detail_out(d: review.RunDetail) -> RunDetailOut:
    template = trace_url_template()
    return RunDetailOut(
        **{
            k: getattr(d, k)
            for k in RunDetailOut.model_fields
            if k not in ("scores", "judge_verdicts", "labels", "trace_url")
        },
        trace_url=template.format(trace_id=d.trace_id) if template else None,
        scores=[ScoreLineOut(**{k: getattr(s, k) for k in ScoreLineOut.model_fields}) for s in d.scores],
        judge_verdicts=[
            ScoreLineOut(**{k: getattr(s, k) for k in ScoreLineOut.model_fields}) for s in d.judge_verdicts
        ],
        labels=[LabelLineOut(**{k: getattr(label, k) for k in LabelLineOut.model_fields}) for label in d.labels],
    )


def _quality_out(v: metrics.VersionQuality) -> VersionQualityOut:
    return VersionQualityOut(
        agent_key=v.agent_key,
        version_key=v.version_key,
        agent_name=v.agent_name,
        pipeline=v.pipeline,
        agent_id=v.agent_id,
        version_id=v.version_id,
        deployed=v.deployed,
        offline=_rate(v.offline) or RateOut(n=0, passed=0, rate=None),
        offline_threshold=v.offline_threshold,
        offline_previous=_rate(v.offline_previous),
        online=_rate(v.online) or RateOut(n=0, passed=0, rate=None),
        judge=_rate(v.judge) or RateOut(n=0, passed=0, rate=None),
        thumbs=_rate(v.thumbs) or RateOut(n=0, passed=0, rate=None),
        expert=_rate(v.expert) or RateOut(n=0, passed=0, rate=None),
        tool_grounded=_rate(v.tool_grounded) or RateOut(n=0, passed=0, rate=None),
        regressed=v.regressed,
        diverged=v.diverged,
    )


def _calibration_out(c: reports.Calibration) -> CalibrationOut:
    return CalibrationOut(
        labelled=c.labelled,
        tp=c.tp,
        fn=c.fn,
        fp=c.fp,
        tn=c.tn,
        tpr=c.tpr,
        tnr=c.tnr,
        agreement=c.agreement,
        kappa=c.kappa,
        trusted=c.trusted,
        min_labels=PM_THRESHOLDS.calibration_min_labels,
        tpr_min=PM_THRESHOLDS.judge_tpr_min,
        tnr_min=PM_THRESHOLDS.judge_tnr_min,
        disagreements=[
            DisagreementOut(**{k: getattr(d, k) for k in DisagreementOut.model_fields}) for d in c.disagreements
        ],
    )


# -- Reads ------------------------------------------------------------------- #


@router.get("/overview", response_model=OverviewOut)
def overview(
    _pm: PmUser,
    db: Db,
    window_days: WindowDays = DEFAULT_WINDOW_DAYS,
    include_simulated: IncludeSimulated = False,
) -> OverviewOut:
    """Every widget of ADR-0053 §4 in one payload, over the last `window_days`."""
    kw = {"window_days": window_days, "include_simulated": include_simulated}
    foot = metrics.simulation_footprint(db, include_simulated=include_simulated)
    outcomes = metrics.outcome_breakdown(db, **kw)
    funnel = metrics.approval_funnel(db, **kw)
    slices = metrics.failure_slices(db, **kw)
    traces = metrics.trace_coverage(db, **kw)
    t = PM_THRESHOLDS
    return OverviewOut(
        window_days=window_days,
        include_simulated=include_simulated,
        generated_at=datetime.now(),
        thresholds=ThresholdsOut(**{k: getattr(t, k) for k in ThresholdsOut.model_fields}),
        simulation=SimulationFootprintOut(
            simulated_runs=foot.simulated_runs,
            total_runs=foot.total_runs,
            simulated_pending_approvals=foot.simulated_pending_approvals,
            shown=foot.shown,
        ),
        traces=TraceCoverageOut(runs=traces.runs, with_trace=traces.with_trace),
        alerts=[AlertOut(**{k: getattr(a, k) for k in AlertOut.model_fields}) for a in reports.alerts(db, **kw)],
        success_by_trigger=[
            TriggerRateOut(trigger=x.trigger, runs=x.runs, successes=x.successes, simulated=x.simulated, rate=x.rate)
            for x in metrics.success_by_trigger(db, **kw)
        ],
        outcomes=OutcomeBreakdownOut(
            slices=[OutcomeSliceOut(outcome=s.outcome, runs=s.runs) for s in outcomes.slices],
            self_fix_ratio=outcomes.self_fix_ratio,
            self_fix_healthy_above=metrics.SELF_FIX_HEALTHY_RATIO,
            unclassified_errors=outcomes.unclassified_errors,
            success_with_tool_failures=outcomes.success_with_tool_failures,
        ),
        cost_by_agent=[
            AgentCostOut(
                agent_key=a.agent_key,
                agent_name=a.agent_name,
                pipeline=a.pipeline,
                agent_id=a.agent_id,
                runs=a.runs,
                successes=a.successes,
                cost_usd=a.cost_usd,
                cost_per_run=a.cost_per_run,
                cost_per_success=a.cost_per_success,
                latency_p50=a.latency_p50,
                latency_p90=a.latency_p90,
                deployment_status=a.deployment_status,
            )
            for a in metrics.cost_by_agent(db, **kw)
        ],
        cost_by_model=[
            ModelCostOut(
                model=m.model,
                runs=m.runs,
                cost_usd=m.cost_usd,
                tokens=m.tokens,
                cost_per_run=(m.cost_usd / m.runs) if m.runs else None,
            )
            for m in metrics.cost_by_model(db, **kw)
        ],
        # Not filtered by `include_simulated`: nothing here is ever simulated —
        # every row is a real call to a real provider. Windowed by the same
        # `window_days` as everything else, so the two halves of the bill are
        # always being read over the same days.
        platform_spend=[PlatformSpendOut(**row) for row in spend_by_purpose(since_days=window_days)],
        quality_by_version=[_quality_out(v) for v in metrics.quality_by_version(db, **kw)],
        approvals=ApprovalFunnelOut(
            pending=funnel.pending,
            decided=funnel.decided,
            approved=funnel.approved,
            rejected=funnel.rejected,
            approval_rate=funnel.approval_rate,
            avg_wait_minutes=funnel.avg_wait_minutes,
            p90_wait_minutes=funnel.p90_wait_minutes,
            oldest_pending_minutes=funnel.oldest_pending_minutes,
            pending_list=[
                PendingApprovalOut(**{k: getattr(p, k) for k in PendingApprovalOut.model_fields})
                for p in funnel.pending_list
            ],
        ),
        feedback_by_week=[
            FeedbackWeekOut(week_start=w.week_start, thumbs_up=w.thumbs_up, thumbs_down=w.thumbs_down, ratio=w.ratio)
            for w in metrics.feedback_by_week(db, **kw)
        ],
        feedback_notes=[
            FeedbackNoteOut(**{k: getattr(n, k) for k in FeedbackNoteOut.model_fields})
            for n in metrics.recent_feedback_notes(db, include_simulated=include_simulated)
        ],
        failure_slices=FailureSlicesOut(
            by_node_type=[SliceCountOut(key=s.key, runs=s.runs) for s in slices.by_node_type],
            by_action=[SliceCountOut(key=s.key, runs=s.runs) for s in slices.by_action],
            by_trace_code=[SliceCountOut(key=s.key, runs=s.runs) for s in slices.by_trace_code],
            failed_runs=slices.failed_runs,
        ),
        calibration=_calibration_out(reports.calibration(db, **kw)),
        review_queue=review.queue_size(db, include_simulated=include_simulated),
        activation=ActivationFunnelOut(
            **{
                k: getattr(metrics.activation_funnel(db, include_simulated=include_simulated), k)
                for k in ActivationFunnelOut.model_fields
            }
        ),
        gate_history=[_gate_out(g) for g in metrics.gate_history(db)],
        trace_codes=dict(TRACE_CODES),
    )


@router.get("/runs", response_model=list[RecentRunOut])
def runs(
    _pm: PmUser,
    db: Db,
    window_days: WindowDays = DEFAULT_WINDOW_DAYS,
    include_simulated: IncludeSimulated = False,
    outcome: Annotated[str | None, Query(max_length=40)] = None,
    trigger: Annotated[str | None, Query(max_length=40)] = None,
    agent_key: Annotated[str | None, Query(max_length=120)] = None,
    failure_node_type: Annotated[str | None, Query(max_length=60)] = None,
    needs_review: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = metrics.RECENT_RUNS_LIMIT,
) -> list[RecentRunOut]:
    """The runs behind the board, newest first, filterable by the words the widgets group by."""
    return [
        _run_out(r)
        for r in metrics.recent_runs(
            db,
            window_days=window_days,
            include_simulated=include_simulated,
            outcome=outcome,
            trigger=trigger,
            agent_key=agent_key,
            failure_node_type=failure_node_type,
            needs_review=needs_review,
            limit=limit,
        )
    ]


@router.get("/runs/{run_id}", response_model=RunDetailOut)
def run_detail(_pm: PmUser, db: Db, run_id: int) -> RunDetailOut:
    """One run with its trace, scores and labels — the review screen's whole payload."""
    detail = review.run_detail(db, run_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Không có lần chạy #{run_id}")
    return _detail_out(detail)


@router.get("/review/next", response_model=RunDetailOut | None)
def review_next(
    _pm: PmUser,
    db: Db,
    include_simulated: IncludeSimulated = False,
    agent_key: Annotated[str | None, Query(max_length=120)] = None,
) -> RunDetailOut | None:
    """The next unlabelled production run worth a person's minute, or nothing when the queue is empty."""
    detail = review.next_to_review(db, include_simulated=include_simulated, agent_key=agent_key)
    return None if detail is None else _detail_out(detail)


@router.get("/versions/{version_id}/report", response_model=VersionReportOut)
def version_report(
    _pm: PmUser, db: Db, version_id: int, window_days: WindowDays = DEFAULT_WINDOW_DAYS
) -> VersionReportOut:
    """Slide 18's table for one V2 version: candidate against the previous version's eval, with the regressions."""
    report = reports.version_report(db, version_id=version_id, window_days=window_days)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Không có version #{version_id}")
    return VersionReportOut(
        agent_id=report.agent_id,
        agent_name=report.agent_name,
        version_id=report.version_id,
        baseline_version_id=report.baseline_version_id,
        threshold=report.threshold,
        candidate=_rate(report.candidate) or RateOut(n=0, passed=0, rate=None),
        baseline=_rate(report.baseline),
        lines=[
            CategoryLineOut(
                category=line.category,
                baseline=_rate(line.baseline),
                candidate=_rate(line.candidate) or RateOut(n=0, passed=0, rate=None),
                delta=line.delta,
                noise=line.noise,
            )
            for line in report.lines
        ],
        regressions=[
            RegressionOut(input_message=r.input_message, criterion=r.criterion, reason=r.reason)
            for r in report.regressions
        ],
        online=_rate(report.online) or RateOut(n=0, passed=0, rate=None),
        judge=_rate(report.judge) or RateOut(n=0, passed=0, rate=None),
        thumbs=_rate(report.thumbs) or RateOut(n=0, passed=0, rate=None),
        expert=_rate(report.expert) or RateOut(n=0, passed=0, rate=None),
        verdict=report.verdict,
        notes=report.notes,
        candidate_judge_model=report.candidate_judge_model,
        baseline_judge_model=report.baseline_judge_model,
        judge_changed=report.judge_changed,
    )


@router.get("/gate/runs/{gate_run_id}", response_model=GateRunDetailOut)
def gate_run_detail(_pm: PmUser, db: Db, gate_run_id: int) -> GateRunDetailOut:
    row = db.get(MetaAgentGateRun, gate_run_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Không có lần chạy gate #{gate_run_id}")
    return _gate_detail(row)


# -- Levers ------------------------------------------------------------------ #


@router.put("/deployments/{agent_id}/status", response_model=DeploymentStatusOut)
def set_deployment_status(_pm: PmUser, db: Db, agent_id: int, payload: DeploymentStatusRequest) -> DeploymentStatusOut:
    """The kill switch: pause or resume any V2 agent's Deployment."""
    try:
        deployment = controls.pause_or_resume(db, agent_id=agent_id, serving=payload.serving)
    except controls.ControlError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    return DeploymentStatusOut(
        agent_id=deployment.agent_id,
        deployment_id=deployment.id,
        status=deployment.status.value,
        updated_at=deployment.updated_at,
    )


@router.post("/runs/{run_id}/feedback", response_model=FeedbackOut, status_code=status.HTTP_201_CREATED)
def leave_feedback(pm: PmUser, db: Db, run_id: int, payload: FeedbackRequest) -> FeedbackOut:
    """Thumbs up/down on one run, with an optional note — the PM's quick signal, distinct from the expert label."""
    try:
        score = controls.leave_feedback(
            db, run_id=run_id, thumbs_up=payload.thumbs_up, comment=payload.comment, given_by=pm.id
        )
    except controls.ControlError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    return FeedbackOut(
        score_id=score.id, run_id=run_id, thumbs_up=score.value >= 1, comment=score.comment, created_at=score.created_at
    )


@router.post("/runs/{run_id}/label", response_model=LabelOut, status_code=status.HTTP_201_CREATED)
def label_run(
    pm: PmUser, db: Db, run_id: int, payload: LabelRequest, include_simulated: IncludeSimulated = False
) -> LabelOut:
    """The expert's verdict: Đạt / Không đạt, trace codes, a note — ADR-0053 §3 layer three."""
    try:
        label = review.label_run(
            db, run_id=run_id, reviewer_id=pm.id, passed=payload.passed, codes=payload.codes, note=payload.note
        )
    except review.ReviewError as exc:
        code = status.HTTP_404_NOT_FOUND if "Không có lần chạy" in str(exc) else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(status_code=code, detail=str(exc)) from None
    return LabelOut(
        label_id=label.id,
        run_id=run_id,
        passed=label.passed,
        codes=list(label.codes or []),
        queue_remaining=review.queue_size(db, include_simulated=include_simulated),
    )


@router.post("/runs/{run_id}/promote", response_model=PromoteOut, status_code=status.HTTP_201_CREATED)
def promote_run(_pm: PmUser, db: Db, run_id: int, payload: PromoteRequest) -> PromoteOut:
    """Turn a bad run into an `eval_cases` row on its version — playbook §2, rule 1. Input prefilled from the trace."""
    try:
        promoted = controls.promote_run_to_case(
            db,
            run_id=run_id,
            input_message=payload.input_message,
            expected=payload.expected,
            criterion=payload.criterion,
        )
    except controls.ControlError as exc:
        code = status.HTTP_404_NOT_FOUND if "Không có lần chạy" in str(exc) else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise HTTPException(status_code=code, detail=str(exc)) from None
    if promoted.case is None:
        return PromoteOut(
            case_id=None,
            version_id=None,
            golden_line=promoted.golden_line,
            note="Run UC-1 không có version V2 để gắn case — dán dòng JSONL vào golden/ qua diff.",
        )
    return PromoteOut(
        case_id=promoted.case.id,
        version_id=promoted.case.introduced_in_version_id,
        golden_line=promoted.golden_line,
        note="Đã thêm case vào version.",
    )


@router.post("/judge/sample", response_model=JudgeSampleOut, status_code=status.HTTP_201_CREATED)
async def judge_sample(_pm: PmUser, db: Db, model_factory: ModelFactory, payload: JudgeSampleRequest) -> JudgeSampleOut:
    """Judge up to `limit` unjudged production runs — layer two, sampled, spends tokens."""
    try:
        report = await judge_module.judge_sample(
            db, model_factory=model_factory, limit=payload.limit, agent_key=payload.agent_key
        )
    except Exception as exc:  # noqa: BLE001 — no key, model outage: a 503, not a crash
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Không chấm được mẫu: {exc}"
        ) from None
    return JudgeSampleOut(
        judged=[
            JudgedRunOut(
                run_id=r.run_id,
                agent_name=r.agent_name,
                passed=r.passed,
                verdicts=[
                    CriterionVerdictOut(category=v.category, criterion=v.criterion, passed=v.passed, reason=v.reason)
                    for v in r.verdicts
                ],
            )
            for r in report.judged
        ],
        passed=report.passed,
        skipped=report.skipped,
    )


@router.post("/agents/{agent_id}/approvals/{approval_id}", response_model=DecisionOut)
async def decide_approval(
    pm: PmUser,
    db: Db,
    agent_id: int,
    approval_id: int,
    payload: DecisionRequest,
    start_run: StartRun,
    checkpointer: Checkpointer,
    knowledge: Knowledge,
    databases: Databases,
) -> DecisionOut:
    """Decide one run waiting on a person; resume its graph in the background if one is really waiting.

    `agent_id` is in the path because `get_knowledge_store` — which the resumed
    graph's `KnowledgeSearch` nodes read through — is keyed by it, the same way
    the owner's route is. It is checked against the row, so a decision cannot be
    filed under one agent for a pause that belongs to another.
    """
    try:
        decision = controls.decide_pending_run(
            db,
            approval_id=approval_id,
            agent_id=agent_id,
            decided_by=pm.id,
            approved=payload.approved,
            reason=payload.reason,
        )
    except controls.ControlError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None

    row = decision.approval
    if not decision.resumable:
        return DecisionOut(
            approval_id=row.id,
            status=row.status.value,
            resumed=False,
            note="Đã ghi quyết định. Đây là run giả lập — không có checkpoint nào để chạy tiếp.",
        )

    version = db.get(AgentVersion, row.version_id)
    if version is None:
        return DecisionOut(
            approval_id=row.id, status=row.status.value, resumed=False, note="Version không còn — chỉ ghi quyết định."
        )
    try:
        workflow = workflow_of(version)
        ensure_runnable(workflow)
    except Exception as exc:  # noqa: BLE001 — the decision stands; the resume is what cannot happen
        return DecisionOut(
            approval_id=row.id,
            status=row.status.value,
            resumed=False,
            note=f"Đã ghi quyết định; không chạy tiếp được: {exc}",
        )

    leg = parked_leg(db, row)
    run = start_run.resumed(leg.identity, Conversation(thread=leg.identity.thread, checkpointer=checkpointer))
    resumption = Resumption(
        run_id=leg.run_id,
        decision=ApprovalDecision(status=row.status.value, reason=row.reason, decided_by=pm.id),
        carried_seconds=leg.carried_seconds,
        carried_usage=leg.carried_usage,
    )
    # The asker the first leg read data as (DS-05) — a PM's approval does not change who was asking.
    reader = databases.asked_by(leg.run_as)
    stream = (
        _workflow_sandbox_stream(workflow, leg.input_message, run, knowledge, reader, resume=(leg, resumption))
        if leg.sandbox
        else _workflow_event_stream(workflow, leg.input_message, run, knowledge, reader, resume=resumption)
    )

    # Drained *inside* the request, not as a `BackgroundTasks` job. A
    # background task runs after the response is sent, and on Cloud Run that is
    # the moment the instance's CPU is throttled to near nothing — the resume
    # would crawl or stall, the decision row would say "resumed" and the run
    # would never arrive. The run is bounded by its own ledger (wall-clock and
    # token ceilings), so holding the request open for it is holding it open
    # for a known maximum, which is what a PM pressing "approve" is waiting
    # for anyway. The decision was already committed above: a resume that dies
    # here leaves the record and says so.
    try:
        async for _line in stream:
            pass
    except Exception as exc:  # noqa: BLE001 — the decision stands; the resume is what did not
        logger.warning("Chạy tiếp run sau khi PM duyệt thất bại (approval %s)", row.id, exc_info=True)
        return DecisionOut(
            approval_id=row.id,
            status=row.status.value,
            resumed=False,
            note=f"Đã ghi quyết định; chạy tiếp graph thất bại giữa chừng: {exc}",
        )
    return DecisionOut(
        approval_id=row.id,
        status=row.status.value,
        resumed=True,
        note="Đã ghi quyết định và chạy tiếp graph xong — kết quả vào trace/run như một lần chạy bình thường.",
    )


@router.post("/gate/runs", response_model=GateRunBatchOut, status_code=status.HTTP_201_CREATED)
async def run_gate(pm: PmUser, db: Db, payload: GateRunRequest) -> GateRunBatchOut:
    """Run the meta agent's regression gate and keep the answer — playbook step 3.

    `mode=real` spends tokens on every case and needs a model key; `simulated`
    drives the same grader over the scripted oracle and is what the board
    shows until the first real run. Both write a row; only `real` measures.
    """
    rows = []
    for i in range(payload.repeats):
        seed = None if payload.seed is None else payload.seed + i
        try:
            rows.append(
                await gate.run_and_record(
                    db, mode=payload.mode, triggered_by=pm.id, fidelity=payload.fidelity, seed=seed
                )
            )
        except Exception as exc:  # noqa: BLE001 — a real run with no key, or a model outage, is a 503 not a crash
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Không chạy được gate ({payload.mode}): {exc}"
            ) from None
    passed_every_time: set[str] | None = None
    for row in rows:
        passed = {v["case_id"] for v in row.verdicts if v.get("passed")}
        passed_every_time = passed if passed_every_time is None else passed_every_time & passed
    unsat = list(rows[0].thresholds.get("_unsatisfiable_cases", [])) if rows else []
    return GateRunBatchOut(
        runs=[_gate_detail(row) for row in rows],
        always_passed=sorted(passed_every_time or set()),
        unsatisfiable=unsat,
    )
