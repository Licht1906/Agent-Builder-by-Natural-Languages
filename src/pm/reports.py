"""The three reports the deck says a PM reads before shipping — ADR-0053 §4.

- **Calibration** (slides 53–56): the judge's verdicts beside the expert's
  on the *same* runs, as a confusion matrix. Pass-rate agreement is not the
  number — two graders can both pass 70 % and disagree on every row — so the
  figures are TPR (good output recognised) and TNR (bad output caught), and
  the judge is "trusted" only when both clear the pre-registered bar on
  enough labels.
- **Version report** (slides 18, 65): candidate against baseline — the
  previous version of the same agent — per criterion category, with the
  delta, the regression count (cases that passed before and fail now), the
  gate threshold, and the reminder that on a small set a delta within the
  noise band is not a change.
- **Alerts** (slides 19, 50): the four production questions, asked with
  numbers and thresholds rather than "để ý xem sao".
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import AgentRun, AgentVersion, EvalRun, QualityScore, RunLabel, RunTrigger, ScoreSource
from src.evals.results import latest_run, results_of
from src.models.schemas import EvalCaseResult
from src.pm import metrics
from src.pm.judge import JUDGE_PASS
from src.pm.thresholds import PM_THRESHOLDS, Thresholds

# -- Calibration ------------------------------------------------------------ #


@dataclass(frozen=True, slots=True)
class Disagreement:
    run_id: int
    agent_name: str
    judge_passed: bool
    expert_passed: bool
    judge_reason: str
    expert_note: str | None
    expert_codes: list[str]


@dataclass(frozen=True, slots=True)
class Calibration:
    labelled: int
    tp: int  # expert pass, judge pass
    fn: int  # expert pass, judge fail  — judge blocked a good answer
    fp: int  # expert fail, judge pass  — judge let a bad one through (the dangerous cell)
    tn: int  # expert fail, judge fail
    tpr: float | None
    tnr: float | None
    agreement: float | None
    kappa: float | None
    trusted: bool
    disagreements: list[Disagreement] = field(default_factory=list)


def calibration(
    db: Session, *, window_days: int, include_simulated: bool = False, thresholds: Thresholds = PM_THRESHOLDS
) -> Calibration:
    """Judge × expert on every run both have graded in the window."""
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=window_days)
    sim = [] if include_simulated else [AgentRun.simulated.is_(False)]
    judge_rows = db.execute(
        select(QualityScore.run_id, QualityScore.value, QualityScore.comment)
        .join(AgentRun, AgentRun.id == QualityScore.run_id)
        .where(
            QualityScore.name == JUDGE_PASS,
            QualityScore.source == ScoreSource.JUDGE,
            AgentRun.created_at >= since,
            *sim,
        )
        .order_by(QualityScore.created_at, QualityScore.id)
    ).all()
    judge: dict[int, tuple[bool, str]] = {int(r): (float(v) >= 1, c or "") for r, v, c in judge_rows}
    if not judge:
        return _empty_calibration()
    label_rows = db.execute(
        select(RunLabel, AgentRun.agent_name)
        .join(AgentRun, AgentRun.id == RunLabel.run_id)
        .where(RunLabel.run_id.in_(list(judge)))
        .order_by(RunLabel.created_at, RunLabel.id)
    ).all()
    expert: dict[int, tuple[RunLabel, str]] = {label.run_id: (label, name) for label, name in label_rows}  # newest wins

    tp = fn = fp = tn = 0
    disagreements: list[Disagreement] = []
    for run_id, (label, name) in expert.items():
        judged, reason = judge[run_id]
        if label.passed and judged:
            tp += 1
        elif label.passed and not judged:
            fn += 1
        elif not label.passed and judged:
            fp += 1
        else:
            tn += 1
        if label.passed != judged:
            disagreements.append(
                Disagreement(
                    run_id=run_id,
                    agent_name=name,
                    judge_passed=judged,
                    expert_passed=label.passed,
                    judge_reason=reason[:300],
                    expert_note=label.note,
                    expert_codes=list(label.codes or []),
                )
            )
    n = tp + fn + fp + tn
    tpr = tp / (tp + fn) if (tp + fn) else None
    tnr = tn / (tn + fp) if (tn + fp) else None
    agreement = (tp + tn) / n if n else None
    kappa = _kappa(tp, fn, fp, tn)
    trusted = (
        n >= thresholds.calibration_min_labels
        and tpr is not None
        and tnr is not None
        and tpr >= thresholds.judge_tpr_min
        and tnr >= thresholds.judge_tnr_min
    )
    return Calibration(
        labelled=n,
        tp=tp,
        fn=fn,
        fp=fp,
        tn=tn,
        tpr=tpr,
        tnr=tnr,
        agreement=agreement,
        kappa=kappa,
        trusted=trusted,
        disagreements=disagreements[:20],
    )


def _empty_calibration() -> Calibration:
    return Calibration(
        labelled=0, tp=0, fn=0, fp=0, tn=0, tpr=None, tnr=None, agreement=None, kappa=None, trusted=False
    )


def _kappa(tp: int, fn: int, fp: int, tn: int) -> float | None:
    """Cohen's κ — agreement beyond chance (slide 55: "khớp 85%" phần lớn là khớp nhờ may rủi)."""
    n = tp + fn + fp + tn
    if not n:
        return None
    observed = (tp + tn) / n
    expert_pass = (tp + fn) / n
    judge_pass = (tp + fp) / n
    expected = expert_pass * judge_pass + (1 - expert_pass) * (1 - judge_pass)
    if expected >= 1.0:
        return None
    return (observed - expected) / (1 - expected)


# -- Version report --------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CategoryLine:
    category: str
    baseline: metrics.Rate | None
    candidate: metrics.Rate
    #: candidate rate − baseline rate, or None without a baseline.
    delta: float | None
    #: |delta| within the pre-registered noise band — shown, never hidden.
    noise: bool


@dataclass(frozen=True, slots=True)
class Regression:
    input_message: str
    criterion: str
    reason: str


@dataclass(frozen=True, slots=True)
class NewCase:
    """A case the candidate ran that the baseline never did — "chưa có baseline" (EV-05)."""

    input_message: str
    criterion: str
    passed: bool


@dataclass(frozen=True, slots=True)
class VersionReport:
    agent_id: int
    agent_name: str
    version_id: int
    baseline_version_id: int | None
    threshold: float | None
    candidate: metrics.Rate
    baseline: metrics.Rate | None
    lines: list[CategoryLine]
    regressions: list[Regression]
    #: Online, for the same version, in the window — what the deck puts in the last column.
    online: metrics.Rate
    judge: metrics.Rate
    thumbs: metrics.Rate
    expert: metrics.Rate
    verdict: str
    notes: list[str]
    #: Which model graded each run (ADR-0054 §4) — `""` for a run with no
    #: eval yet, or one written before this column existed.
    candidate_judge_model: str = ""
    baseline_judge_model: str = ""
    #: Whether the two differ, both non-empty — the guardrail pre-register:
    #: a judge swap is a new baseline, and `lines`/`regressions` above are
    #: already computed as though there were no baseline at all when this is
    #: true (see `version_report`).
    judge_changed: bool = False
    #: EV-05 / ADR-0069 §5: the candidate's cases the baseline never ran. The
    #: rates and deltas above are over the cases both runs asked; these are
    #: listed apart, never folded into a delta they have no "before" for.
    new_cases: list[NewCase] = field(default_factory=list)


def version_report(
    db: Session, *, version_id: int, window_days: int = 28, thresholds: Thresholds = PM_THRESHOLDS
) -> VersionReport | None:
    """Candidate `version_id` read against its predecessor's latest eval — slide 18's table."""
    version = db.get(AgentVersion, version_id)
    if version is None:
        return None
    agent = version.agent
    candidate_run = latest_run(db, version_id)
    previous = (
        db.execute(
            select(AgentVersion.id)
            .where(AgentVersion.agent_id == version.agent_id, AgentVersion.id < version_id)
            .order_by(AgentVersion.id.desc())
        )
        .scalars()
        .all()
    )
    baseline_run: EvalRun | None = None
    baseline_version_id: int | None = None
    for prev_id in previous:
        baseline_run = latest_run(db, prev_id)
        if baseline_run is not None:
            baseline_version_id = prev_id
            break

    cand_results = results_of(candidate_run) if candidate_run else []
    base_results = results_of(baseline_run) if baseline_run else []
    # EV-05: compare only what both runs asked. A legacy baseline (no
    # `suite_case_id`) cannot be intersected, and is compared whole as before.
    new_cases: list[NewCase] = []
    intersected = bool(baseline_run) and any(result.suite_case_id for result in base_results)
    if intersected:
        asked_before = {result.suite_case_id for result in base_results}
        asked_now = {result.suite_case_id for result in cand_results}
        new_cases = [
            NewCase(input_message=r.input_message, criterion=r.criterion, passed=r.passed)
            for r in cand_results
            if r.suite_case_id not in asked_before
        ]
        cand_results = [result for result in cand_results if result.suite_case_id in asked_before]
        base_results = [result for result in base_results if result.suite_case_id in asked_now]
    candidate_judge_model = candidate_run.judge_model if candidate_run else ""
    baseline_judge_model = baseline_run.judge_model if baseline_run else ""
    # ADR-0054 §4's guardrail pre-register: a run graded by a different judge
    # model is not comparable to one graded by another, so it is treated below
    # exactly as "no baseline run" is — no delta, no regressions — plus a note
    # naming why, rather than a silently misleading percentage change.
    judge_changed = bool(
        baseline_run
        and candidate_judge_model
        and baseline_judge_model
        and candidate_judge_model != baseline_judge_model
    )
    cand_by_cat = _by_category(cand_results)
    base_by_cat = _by_category(base_results) if baseline_run and not judge_changed else {}
    lines: list[CategoryLine] = []
    for category in sorted(set(cand_by_cat) | set(base_by_cat)):
        c = cand_by_cat.get(category, metrics.Rate())
        b = base_by_cat.get(category) if baseline_run else None
        delta = (c.rate - b.rate) if (b is not None and b.rate is not None and c.rate is not None) else None
        lines.append(
            CategoryLine(
                category=category,
                baseline=b,
                candidate=c,
                delta=delta,
                noise=delta is not None and abs(delta) <= thresholds.noise_delta,
            )
        )

    regressions, matched_by_text = ([], False) if judge_changed else _regressions(base_results, cand_results)
    online = _online_rates(db, agent_id=version.agent_id, version_id=version_id, window_days=window_days)

    candidate = metrics.Rate(candidate_run.total_count, candidate_run.passed_count) if candidate_run else metrics.Rate()
    baseline = metrics.Rate(baseline_run.total_count, baseline_run.passed_count) if baseline_run else None
    common_baseline, common_candidate = baseline, candidate
    if intersected and candidate_run:
        # The delta is read over the common cases; the threshold below still
        # reads the whole candidate run, as the gate does.
        common_baseline = metrics.Rate(len(base_results), sum(1 for r in base_results if r.passed))
        common_candidate = metrics.Rate(len(cand_results), sum(1 for r in cand_results if r.passed))
    threshold = float(candidate_run.threshold) if candidate_run else None
    verdict, notes = _verdict(
        candidate,
        None if judge_changed else common_baseline,
        threshold,
        regressions,
        thresholds,
        candidate_on_common=common_candidate,
    )
    if new_cases:
        notes.append(f"{len(new_cases)} case mới chưa có baseline — liệt kê riêng, không tính vào chênh lệch.")
    if judge_changed:
        notes.insert(
            0,
            f"Judge đổi ({baseline_judge_model} → {candidate_judge_model}) — coi là baseline mới, không so delta với bản trước.",
        )
    if matched_by_text:
        notes.append(
            "Ghép regressions theo chuỗi (input + criterion) — bản chạy trước chưa có suite_case_id (ADR-0054 §1)."
        )
    return VersionReport(
        agent_id=version.agent_id,
        agent_name=agent.name if agent else f"agent {version.agent_id}",
        version_id=version_id,
        baseline_version_id=baseline_version_id,
        threshold=threshold,
        candidate=candidate,
        baseline=baseline,
        lines=lines,
        regressions=regressions,
        online=online["online"],
        judge=online["judge"],
        thumbs=online["thumbs"],
        expert=online["expert"],
        verdict=verdict,
        notes=notes,
        candidate_judge_model=candidate_judge_model,
        baseline_judge_model=baseline_judge_model,
        judge_changed=judge_changed,
        new_cases=new_cases,
    )


def _by_category(results: Sequence[EvalCaseResult]) -> dict[str, metrics.Rate]:
    out: dict[str, list[int]] = {}
    for result in results:
        category = (
            result.category.value
            if result.category is not None
            else ("prohibition" if result.expected_check is not None else "judged")
        )
        slot = out.setdefault(category, [0, 0])
        slot[0] += 1
        slot[1] += 1 if result.passed else 0
    return {k: metrics.Rate(n, p) for k, (n, p) in out.items()}


def _regressions(
    baseline: Sequence[EvalCaseResult], candidate: Sequence[EvalCaseResult]
) -> tuple[list[Regression], bool]:
    """Cases that passed in the baseline run and fail in the candidate's.

    Matched by `suite_case_id` (ADR-0054 §1) — the id is stable across
    versions, so a case caught wobbling is caught even when its wording was
    edited between the two runs. A baseline written before that column
    existed stores `suite_case_id: 0` on every result (the schema's default
    for "read back an old run"), so such a run falls back to the old
    `(input_message, criterion)` string match instead — the second return
    value says whether that fallback fired, so the caller can say so.
    """
    legacy_baseline = bool(baseline) and all(r.suite_case_id == 0 for r in baseline)
    if legacy_baseline:
        passed_before_text = {(r.input_message.strip(), r.criterion.strip()) for r in baseline if r.passed}
        return [
            Regression(input_message=r.input_message, criterion=r.criterion, reason=r.reason)
            for r in candidate
            if not r.passed and (r.input_message.strip(), r.criterion.strip()) in passed_before_text
        ], True
    passed_before_ids = {r.suite_case_id for r in baseline if r.passed and r.suite_case_id}
    return [
        Regression(input_message=r.input_message, criterion=r.criterion, reason=r.reason)
        for r in candidate
        if not r.passed and r.suite_case_id and r.suite_case_id in passed_before_ids
    ], False


def _online_rates(db: Session, *, agent_id: int, version_id: int, window_days: int) -> dict[str, metrics.Rate]:
    rows = metrics.quality_by_version(db, window_days=window_days)
    for row in rows:
        if row.agent_key == str(agent_id) and row.version_key == str(version_id):
            return {"online": row.online, "judge": row.judge, "thumbs": row.thumbs, "expert": row.expert}
    return {"online": metrics.Rate(), "judge": metrics.Rate(), "thumbs": metrics.Rate(), "expert": metrics.Rate()}


def _verdict(
    candidate: metrics.Rate,
    baseline: metrics.Rate | None,
    threshold: float | None,
    regressions: Sequence[Regression],
    thresholds: Thresholds,
    *,
    candidate_on_common: metrics.Rate | None = None,
) -> tuple[str, list[str]]:
    """Ship / Iterate / Hold, with the reasons — slide 50: a verdict must come with what to do next.

    The threshold reads the whole candidate; the delta reads `candidate_on_common`
    against `baseline` — the cases both runs asked (EV-05).
    """
    notes: list[str] = []
    if candidate.n == 0:
        return "hold", ["Chưa chạy eval cho phiên bản này — không có gì để so."]
    rate = candidate.rate or 0.0
    if threshold is not None and rate < threshold:
        notes.append(f"Dưới ngưỡng gate {threshold:.0%} ({rate:.0%}).")
    if regressions:
        notes.append(f"{len(regressions)} case từng đạt ở bản trước nay trượt — đọc trace trước khi ship.")
    common = candidate_on_common if candidate_on_common is not None else candidate
    if baseline is not None and baseline.rate is not None and common.rate is not None:
        delta = common.rate - baseline.rate
        if abs(delta) <= thresholds.noise_delta:
            notes.append(
                f"Chênh {delta:+.1%} so với bản trước nằm trong dải nhiễu ±{thresholds.noise_delta:.0%} — chưa phải cải thiện."
            )
        elif delta < 0:
            notes.append(f"Tụt {delta:+.1%} so với bản trước.")
    if candidate.n < 50:
        notes.append(
            f"Bộ đề {candidate.n} case: một case lật = {100 / candidate.n:.1f} điểm — đọc theo slice, đừng tin con số tổng."
        )
    if threshold is not None and rate < threshold:
        return "iterate", notes
    if regressions:
        return "iterate", notes
    return "ship", notes or ["Đạt ngưỡng, không regression — ship kèm theo dõi tuần đầu (mẫu judge + nhãn expert)."]


# -- Alerts ----------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Alert:
    code: str  # Q1 | Q2 | Q3 | Q4 | LAT | COST
    severity: str  # warning | critical
    message: str
    value: float | None
    threshold: float | None
    agent_key: str | None = None


def alerts(
    db: Session, *, window_days: int, include_simulated: bool = False, thresholds: Thresholds = PM_THRESHOLDS
) -> list[Alert]:
    """Slide 19's four questions plus the two threshold checks, each answered only when there is enough data to answer."""
    out: list[Alert] = []
    by_trigger = {
        t.trigger: t
        for t in metrics.success_by_trigger(db, window_days=window_days, include_simulated=include_simulated)
    }
    manual = by_trigger.get(RunTrigger.MANUAL.value)
    if manual and manual.runs >= 10 and manual.rate is not None and manual.rate < thresholds.production_success_min:
        out.append(
            Alert(
                "Q1",
                "critical",
                f"Success production {manual.rate:.0%} trên {manual.runs} run, dưới ngưỡng {thresholds.production_success_min:.0%}.",
                manual.rate,
                thresholds.production_success_min,
            )
        )

    for row in metrics.quality_by_version(
        db, window_days=window_days, include_simulated=include_simulated, thresholds=thresholds
    ):
        if row.diverged and row.offline.rate is not None and row.online.rate is not None:
            out.append(
                Alert(
                    "Q2",
                    "warning",
                    f"{row.agent_name} v{row.version_key}: offline {row.offline.rate:.0%} nhưng online {row.online.rate:.0%} — bộ đề chưa phản ánh thực tế.",
                    row.online.rate,
                    row.offline.rate,
                    row.agent_key,
                )
            )
        if (
            row.regressed
            and row.offline.rate is not None
            and row.offline_previous is not None
            and row.offline_previous.rate is not None
        ):
            out.append(
                Alert(
                    "Q2",
                    "warning",
                    f"{row.agent_name} v{row.version_key}: eval tụt {row.offline.rate:.0%} so với bản trước {row.offline_previous.rate:.0%}.",
                    row.offline.rate,
                    row.offline_previous.rate,
                    row.agent_key,
                )
            )
        if (
            row.thumbs.n >= 10
            and row.thumbs.rate is not None
            and row.thumbs.rate < thresholds.thumbs_up_min
            and row.judge.n >= 5
            and row.judge.rate is not None
            and row.judge.rate >= thresholds.production_success_min
        ):
            out.append(
                Alert(
                    "Q4",
                    "warning",
                    f"{row.agent_name} v{row.version_key}: thumbs-up {row.thumbs.rate:.0%} mà judge đạt {row.judge.rate:.0%} — rubric đang đo sai thứ.",
                    row.thumbs.rate,
                    thresholds.thumbs_up_min,
                    row.agent_key,
                )
            )

    slices = metrics.failure_slices(db, window_days=window_days, include_simulated=include_simulated)
    outcomes = metrics.outcome_breakdown(db, window_days=window_days, include_simulated=include_simulated)
    if outcomes.success_with_tool_failures > 0:
        out.append(
            Alert(
                "Q3",
                "warning",
                f"{outcomes.success_with_tool_failures} run kết thúc 'success' dù có tool hỏng — lỗi chìm dưới mặt nước, đọc trace.",
                float(outcomes.success_with_tool_failures),
                0.0,
            )
        )
    if outcomes.unclassified_errors > 0:
        out.append(
            Alert(
                "Q3",
                "warning",
                f"{outcomes.unclassified_errors} run lỗi chưa phân loại được — taxonomy cần từ mới.",
                float(outcomes.unclassified_errors),
                0.0,
            )
        )
    if slices.by_trace_code:
        top = slices.by_trace_code[0]
        if top.runs >= 3:
            out.append(
                Alert(
                    "Q3",
                    "warning",
                    f"Trace code nổi nhất: `{top.key}` × {top.runs} — một failure mode, không phải lỗi rời rạc.",
                    float(top.runs),
                    3.0,
                )
            )

    for agent in metrics.cost_by_agent(db, window_days=window_days, include_simulated=include_simulated):
        if agent.runs < 5:
            continue
        if agent.latency_p90 is not None and agent.latency_p90 > thresholds.latency_p90_max_seconds:
            out.append(
                Alert(
                    "LAT",
                    "warning",
                    f"{agent.agent_name}: latency P90 {agent.latency_p90:.0f}s, trần {thresholds.latency_p90_max_seconds:.0f}s.",
                    agent.latency_p90,
                    thresholds.latency_p90_max_seconds,
                    agent.agent_key,
                )
            )
        if agent.cost_per_run is not None and agent.cost_per_run > thresholds.cost_per_run_max_usd:
            out.append(
                Alert(
                    "COST",
                    "warning",
                    f"{agent.agent_name}: ${agent.cost_per_run:.3f}/run, trần ${thresholds.cost_per_run_max_usd:.2f}.",
                    agent.cost_per_run,
                    thresholds.cost_per_run_max_usd,
                    agent.agent_key,
                )
            )
    return out
