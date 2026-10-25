"""Layer two — the LLM judge on a *sample* of production runs. ADR-0053 §3.

The deck's rules, applied literally:

- **Sampled, not universal** (slide 50: *"sample 1–10% session production
  để chấm tiếp"*). Nothing here runs by itself: a PM presses the button on the
  board or the ops script runs on a schedule, both with a `limit`, and every
  call spends tokens only on runs nobody has judged yet.
- **One judge, one criterion, a binary verdict** (slide 52). A run is judged
  once per judge-category criterion of the version's own spec
  (`source_groundedness`, `correctness`, `relevance` — the three ADR-0028
  routes to the judge), and the verdicts land as separate
  `quality_scores(source=judge, name=judge:<category>)` rows; `judge_pass`
  is the conjunction, kept so calibration has one number per run to put
  against the expert's one.
- **Evidence beside the answer** (ADR-0052): the judge reads the run's own
  tool calls and results, never the prose alone.
- **Referent** (slide 41): a run with no spoken answer, or that never
  finished, is not a candidate — there is nothing to grade against a
  criterion — and the code layer has already said what it can about links
  and ids.

What this is not: calibrated. The judge's verdicts are opinions until
`src.pm.reports.calibration` shows them agreeing with expert labels (TPR /
TNR over the thresholds in `src.pm.thresholds`); the board says which of the
two states it is in. And it is the meta agent's model — the same family as
the agents it judges, against slide 52's advice — which ADR-0053 §3 records
as a debt rather than hides.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from src.db.models import AgentRun, AgentVersion, QualityScore, RunOutcome, RunTrigger, ScoreSource
from src.evals.judge import judge_answer
from src.evals.workflow_runner import collector_notes, tool_evidence
from src.models.schemas import SandboxTranscriptEntry
from src.models.solution_spec import SuccessCriterionCategory
from src.services.llm import ChatModelFactory

logger = logging.getLogger(__name__)

JUDGE_PASS = "judge_pass"
JUDGE_PREFIX = "judge:"

#: The categories ADR-0028 leaves to the judge; the rule-based three are
#: graded off the transcript in eval and have no production counterpart yet.
JUDGE_CATEGORIES = (
    SuccessCriterionCategory.SOURCE_GROUNDEDNESS,
    SuccessCriterionCategory.CORRECTNESS,
    SuccessCriterionCategory.RELEVANCE,
)

#: What a run is judged on when its version has no spec to take criteria
#: from (a V1 agent, a UC-1 spec): the platform's own two floors.
_PLATFORM_CRITERIA: tuple[tuple[str, str], ...] = (
    (
        SuccessCriterionCategory.CORRECTNESS.value,
        "Câu trả lời đúng với yêu cầu và chỉ nói những gì có trong bằng chứng công cụ — không bịa thêm, "
        "không bỏ sót kết quả công cụ đã trả về, không nói 'không tìm thấy' khi công cụ có tìm thấy",
    ),
    (
        SuccessCriterionCategory.RELEVANCE.value,
        "Câu trả lời đi đúng vào việc người dùng hỏi, không lạc đề, và chỉ hỏi lại thứ thật sự còn thiếu",
    ),
)


@dataclass(frozen=True, slots=True)
class CriterionVerdict:
    category: str
    criterion: str
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class JudgedRun:
    run_id: int
    agent_name: str
    passed: bool
    verdicts: list[CriterionVerdict] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class JudgeSampleReport:
    judged: list[JudgedRun]
    #: Runs that were candidates but could not be judged (no answer, no transcript) — said, not hidden.
    skipped: int

    @property
    def passed(self) -> int:
        return sum(1 for r in self.judged if r.passed)


def candidate_runs(db: Session, *, limit: int, agent_key: str | None = None) -> list[AgentRun]:
    """Production runs that finished with an answer and have no judge score yet, newest first."""
    judged = exists().where(QualityScore.run_id == AgentRun.id, QualityScore.source == ScoreSource.JUDGE)
    statement = (
        select(AgentRun)
        .where(
            AgentRun.trigger == RunTrigger.MANUAL.value,
            AgentRun.outcome == RunOutcome.SUCCESS.value,
            AgentRun.simulated.is_(False),
            AgentRun.answer.isnot(None),
            AgentRun.answer != "",
            ~judged,
        )
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(limit)
    )
    if agent_key:
        statement = statement.where(AgentRun.agent_key == agent_key)
    return list(db.execute(statement).scalars())


def criteria_for(db: Session, run: AgentRun) -> list[tuple[str, str]]:
    """`(category, text)` pairs the run is judged on — the version's own judge criteria, else the platform's."""
    if run.version_id is not None:
        version = db.get(AgentVersion, run.version_id)
        spec = getattr(version, "solution_spec", None) or {}
        found = [
            (str(c.get("category")), str(c.get("text") or "").strip())
            for c in (spec.get("success_criteria") or [])
            if isinstance(c, dict) and c.get("category") in {cat.value for cat in JUDGE_CATEGORIES} and c.get("text")
        ]
        if found:
            return found
    return list(_PLATFORM_CRITERIA)


async def judge_run(db: Session, run: AgentRun, *, model_factory: ChatModelFactory) -> JudgedRun | None:
    """Judge one run on every criterion and file the verdicts. `None` when there is nothing to judge."""
    if not (run.answer or "").strip() or not run.transcript:
        return None
    transcript = [SandboxTranscriptEntry.model_validate(line) for line in run.transcript]
    evidence = tool_evidence(transcript)
    notes = _notes_for(db, run, transcript)
    verdicts: list[CriterionVerdict] = []
    for category, text in criteria_for(db, run):
        verdict = await judge_answer(
            criterion=text,
            expected=text,
            input_message=run.input_message or "",
            answer=run.answer or "",
            model_factory=model_factory,
            evidence=evidence,
            notes=notes,
        )
        verdicts.append(
            CriterionVerdict(category=category, criterion=text, passed=verdict.passed, reason=verdict.reason)
        )
        db.add(_score(run, f"{JUDGE_PREFIX}{category}", verdict.passed, verdict.reason))
    passed = all(v.passed for v in verdicts)
    summary = "; ".join(f"{v.category}: {'đạt' if v.passed else 'trượt'}" for v in verdicts)
    db.add(_score(run, JUDGE_PASS, passed, summary))
    db.commit()
    return JudgedRun(run_id=run.id, agent_name=run.agent_name, passed=passed, verdicts=verdicts)


async def judge_sample(
    db: Session, *, model_factory: ChatModelFactory, limit: int, agent_key: str | None = None
) -> JudgeSampleReport:
    """Judge up to `limit` unjudged production runs. Each run commits on its own, so a model outage mid-batch keeps what was judged."""
    judged: list[JudgedRun] = []
    skipped = 0
    for run in candidate_runs(db, limit=limit, agent_key=agent_key):
        result = await judge_run(db, run, model_factory=model_factory)
        if result is None:
            skipped += 1
        else:
            judged.append(result)
    return JudgeSampleReport(judged=judged, skipped=skipped)


def _score(run: AgentRun, name: str, passed: bool, comment: str) -> QualityScore:
    return QualityScore(
        agent_id=run.agent_id,
        version_id=run.version_id,
        run_cost_id=run.run_cost_id,
        run_id=run.id,
        name=name,
        value=1.0 if passed else 0.0,
        comment=comment[:2000],
        source=ScoreSource.JUDGE,
    )


def _notes_for(db: Session, run: AgentRun, transcript: Sequence[SandboxTranscriptEntry]) -> list[str]:
    """The collector notes a V2 run's workflow explains; nothing for a run whose graph cannot be read."""
    if run.version_id is None:
        return []
    try:
        from src.workflow.generator import workflow_of

        version = db.get(AgentVersion, run.version_id)
        if version is None:
            return []
        return collector_notes(transcript, workflow_of(version))
    except Exception:  # noqa: BLE001 — a note the judge does not get is a slightly stricter judge, not an error
        return []
