"""A suite that follows its spec, and a reviewer who sees how it moved — EV-05 / ADR-0069 §4–6.

**Retire, never keep an orphan.** When a version's spec or graph moves on, a
case that the previous version could grade but this one cannot — its criterion
reworded or dropped (`orphan`), its rule naming an action or node that is gone
(`ungrounded`) — is retired by the system: `retired_in_version_id`, kept, out of
the suite. This replaces ADR-0054 §1's "orphan vẫn chạy, vẫn chấm". A case that
was already unfit before the change is not this change's doing and is left as it
was. New cases for the new criteria come from the suite builder (EV-03).

**Every change is written down** (`eval_suite_changes`), with who and when: the
system's retirements, the creator's edits and deletions, and the creator's read
of a failing case ("agent sai" / "case sai"). The approval screen shows the
changes since the baseline in two groups, so rewording a criterion to shed the
cases that fail is visible as exactly that — N retired, **K of them failing at
the baseline** — next to what the creator removed by hand.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    AgentVersion,
    EvalCase,
    EvalRun,
    EvalSuiteChange,
    SuiteChangeKind,
    TriageVerdict,
)
from src.evals.applicability import Applicability, applicability_of, ungrounded_reason
from src.evals.cases import get_suite, list_cases
from src.evals.results import latest_run, results_of
from src.models.solution_spec import AgentSolutionSpecification
from src.models.workflow import WorkflowJSON
from src.solution.specs import SpecStageError, solution_spec_of
from src.workflow.generator import WorkflowStageError, workflow_of

_STALE = frozenset({Applicability.ORPHAN, Applicability.UNGROUNDED})


def retire_stale(
    db: Session, version: AgentVersion, *, workflow: WorkflowJSON, spec: AgentSolutionSpecification
) -> list[EvalCase]:
    """Retire every case `version` can no longer grade but its predecessor could — returns them.

    Only on the agent's newest version: re-running an old version must not
    rewrite the suite its successors are graded on. Each retirement records
    whether the case was failing in the baseline's gate run.
    """
    if not _is_newest(db, version):
        return []
    previous = _previous_version(db, version)
    before = _documents_of(previous) if previous is not None else None
    if before is None:
        return []
    before_workflow, before_spec = before
    criteria_now = frozenset(criterion.text for criterion in spec.success_criteria)
    criteria_before = frozenset(criterion.text for criterion in before_spec.success_criteria)
    baseline = baseline_run(db, version)
    failing = {result.suite_case_id for result in results_of(baseline) if not result.passed} if baseline else set()

    retired: list[EvalCase] = []
    for case in list_cases(db, version.agent_id):
        now = applicability_of(case, success_criteria=criteria_now, workflow=workflow)
        if now not in _STALE:
            continue
        if applicability_of(case, success_criteria=criteria_before, workflow=before_workflow) in _STALE:
            continue
        reason = (
            ungrounded_reason(case)
            if now is Applicability.UNGROUNDED
            else f"Tiêu chí “{case.criterion}” không còn trong spec của version này"
        )
        case.retired_in_version_id = version.id
        case.retired_reason = reason
        case.retired_at = datetime.now(UTC).replace(tzinfo=None)
        case.retired_failing_at_baseline = case.id in failing
        record_change(
            db,
            case,
            SuiteChangeKind.RETIRED,
            version_id=version.id,
            detail={"reason": reason, "failing_at_baseline": case.retired_failing_at_baseline},
        )
        retired.append(case)
    db.commit()
    return retired


def record_change(
    db: Session,
    case: EvalCase,
    kind: SuiteChangeKind,
    *,
    version_id: int | None,
    actor_id: int | None = None,
    verdict: TriageVerdict | None = None,
    detail: dict[str, Any] | None = None,
) -> EvalSuiteChange:
    """Write one change down, with what the case said at the time. The caller commits."""
    change = EvalSuiteChange(
        suite_id=case.suite_id,
        case_id=case.id,
        version_id=version_id,
        kind=kind,
        actor_id=actor_id,
        verdict=verdict,
        detail={"criterion": case.criterion, "input_message": case.input_message, **(detail or {})},
    )
    db.add(change)
    return change


def triage(
    db: Session, case: EvalCase, verdict: TriageVerdict, *, version_id: int, actor_id: int, run_id: int | None
) -> EvalSuiteChange:
    """The creator's read of a failing case — "agent sai" or "case sai" — with who and when (ADR-0069 §4)."""
    change = record_change(
        db, case, SuiteChangeKind.TRIAGED, version_id=version_id, actor_id=actor_id, verdict=verdict,
        detail={"run_id": run_id},
    )  # fmt: skip
    db.commit()
    db.refresh(change)
    return change


def latest_triage(db: Session, agent_id: int) -> dict[int, EvalSuiteChange]:
    """Each case's most recent triage, by case id — what its row shows."""
    suite = get_suite(db, agent_id)
    if suite is None:
        return {}
    found: dict[int, EvalSuiteChange] = {}
    for change in db.scalars(
        select(EvalSuiteChange)
        .where(EvalSuiteChange.suite_id == suite.id, EvalSuiteChange.kind == SuiteChangeKind.TRIAGED)
        .order_by(EvalSuiteChange.id)
    ):
        if change.case_id is not None:
            found[change.case_id] = change
    return found


def retired_cases(db: Session, agent_id: int) -> list[EvalCase]:
    """Every retired case of the agent's suite, newest retirement first — the suite's history."""
    suite = get_suite(db, agent_id)
    if suite is None:
        return []
    return list(
        db.scalars(
            select(EvalCase)
            .where(EvalCase.suite_id == suite.id, EvalCase.retired_in_version_id.isnot(None))
            .order_by(EvalCase.retired_at.desc(), EvalCase.id.desc())
        )
    )


# --------------------------------------------------------------------------- #
# What moved between the baseline and a candidate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SuiteChanges:
    """The approval screen's "Thay đổi bộ đề" — ADR-0069 §6."""

    baseline_version_id: int | None
    #: Retired by the system since the baseline.
    retired: list[EvalCase] = field(default_factory=list)
    #: …of which this many were failing in the baseline's gate run.
    retired_failing: int = 0
    #: Edited or deleted by the creator since the baseline.
    creator_changes: list[EvalSuiteChange] = field(default_factory=list)
    #: In the suite now, absent from the baseline's run — "chưa có baseline".
    new_cases: list[EvalCase] = field(default_factory=list)


def suite_changes(db: Session, version: AgentVersion) -> SuiteChanges:
    """What happened to the suite between the baseline version and `version`."""
    baseline = baseline_run(db, version)
    since = baseline.version_id if baseline is not None else 0
    suite = get_suite(db, version.agent_id)
    if suite is None:
        return SuiteChanges(baseline_version_id=None)
    retired = [
        case
        for case in retired_cases(db, version.agent_id)
        if case.retired_in_version_id is not None and since < case.retired_in_version_id <= version.id
    ]
    changes = list(
        db.scalars(
            select(EvalSuiteChange)
            .where(
                EvalSuiteChange.suite_id == suite.id,
                EvalSuiteChange.kind.in_([SuiteChangeKind.EDITED, SuiteChangeKind.DELETED]),
                EvalSuiteChange.version_id > since,
                EvalSuiteChange.version_id <= version.id,
            )
            .order_by(EvalSuiteChange.id)
        )
    )
    in_baseline = {result.suite_case_id for result in results_of(baseline)} if baseline is not None else set()
    new = [case for case in list_cases(db, version.agent_id) if case.id not in in_baseline]
    return SuiteChanges(
        baseline_version_id=baseline.version_id if baseline is not None else None,
        retired=retired,
        retired_failing=sum(1 for case in retired if case.retired_failing_at_baseline),
        creator_changes=changes,
        new_cases=new,
    )


def baseline_run(db: Session, version: AgentVersion) -> EvalRun | None:
    """The latest gate run of the newest earlier version that has one — `src.pm.reports`' baseline."""
    for previous_id in _earlier_version_ids(db, version):
        run = latest_run(db, previous_id)
        if run is not None:
            return run
    return None


def _earlier_version_ids(db: Session, version: AgentVersion) -> Sequence[int]:
    return (
        db.execute(
            select(AgentVersion.id)
            .where(AgentVersion.agent_id == version.agent_id, AgentVersion.id < version.id)
            .order_by(AgentVersion.id.desc())
        )
        .scalars()
        .all()
    )


def _is_newest(db: Session, version: AgentVersion) -> bool:
    newest = db.execute(
        select(AgentVersion.id).where(AgentVersion.agent_id == version.agent_id).order_by(AgentVersion.id.desc())
    ).scalar()
    return newest == version.id


def _previous_version(db: Session, version: AgentVersion) -> AgentVersion | None:
    for previous_id in _earlier_version_ids(db, version):
        previous = db.get(AgentVersion, previous_id)
        if previous is not None and _documents_of(previous) is not None:
            return previous
    return None


def _documents_of(version: AgentVersion) -> tuple[WorkflowJSON, AgentSolutionSpecification] | None:
    try:
        return workflow_of(version), solution_spec_of(version)
    except (WorkflowStageError, SpecStageError, ValueError):
        return None
