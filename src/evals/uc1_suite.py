"""UC-1's own `eval_suites`/`eval_cases` identity — ADR-0054 §6 (EVAL-7 / #157).

`EvalSuite.agent_id` (EVAL-1) is a `ForeignKey("agents.id")`, the V2 identity
space `agents`/`agent_versions` live in. UC-1 has no row there — its own
agent identity is `AgentSpec.metadata.id`, a string like `agt_ar_assistant`
(`uc1_agent_spec_versions.agent_id`), a wholly separate space per ADR-0046
("UC-1's architecture ... uses a different vocabulary ... not yet
reconciled"). Minting a fake V2 `agents` row to hang a suite off of would put
a UC-1 agent in a V2 creator's own agent list — worse than the gap it
closes. `eval_suites.uc1_agent_id` (migration `d2f7a93c1b56`) is the fix: a
second, nullable identity column, exactly one of it or `agent_id` set per
row.

A golden-set case is stored whole: `extra` holds `case.model_dump(mode="json")`
(the full validated `UC1GoldenCase`, `principal`/`retrieval_context` and all),
so `src.evals.uc1_golden_set.validate_golden_case` reconstructs the exact
typed case a golden-set run needs straight back out of it — one schema for
both directions instead of two hand-written mappings that could drift apart.
`criterion`/`expected`/`tags` are filled with a human-readable summary for
whatever generic suite UI (EVAL-5) ever lists a UC-1 row next to a V2 one,
but the golden-set runner itself never reads them.

Deliberately **not** built on `src.evals.suite_io.import_suite` (EVAL-2):
that importer's idempotency key is `(input, criterion)`, and three of
`golden/uc1_ar_v3.jsonl`'s 23 rows share literal `input` text but differ by
`principal` — two of the three collide under that key (see #152's closing
comment on GitHub). A golden case's real identity is its own `id` column;
`import_golden_set_into_suite` dedups on that instead.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import EvalCase, EvalCaseOrigin, EvalSuite
from src.evals.uc1_golden_set import (
    CorrectLookupCase,
    CrossTenantCase,
    InjectionCase,
    LeakCase,
    RefusalCase,
    UC1GoldenCase,
    validate_golden_case,
)

DEFAULT_UC1_SUITE_NAME = "Golden set UC-1"


def get_uc1_suite(db: Session, uc1_agent_id: str) -> EvalSuite | None:
    """`uc1_agent_id`'s suite, or `None` if nothing has ever been imported for it.

    Kept separate from `get_or_create_uc1_suite` for the same reason
    `src.evals.cases.get_suite` is kept separate from its own
    `get_or_create_suite`: a read must never be the thing that creates a row.
    """
    return db.execute(select(EvalSuite).where(EvalSuite.uc1_agent_id == uc1_agent_id)).scalar_one_or_none()


def get_or_create_uc1_suite(db: Session, uc1_agent_id: str) -> EvalSuite:
    suite = get_uc1_suite(db, uc1_agent_id)
    if suite is not None:
        return suite
    suite = EvalSuite(agent_id=None, uc1_agent_id=uc1_agent_id, name=DEFAULT_UC1_SUITE_NAME)
    db.add(suite)
    db.commit()
    db.refresh(suite)
    return suite


def list_uc1_cases(db: Session, uc1_agent_id: str) -> list[EvalCase]:
    suite = get_uc1_suite(db, uc1_agent_id)
    if suite is None:
        return []
    return list(
        db.execute(
            select(EvalCase).where(EvalCase.suite_id == suite.id).order_by(EvalCase.created_at, EvalCase.id)
        ).scalars()
    )


def load_uc1_golden_cases(db: Session, uc1_agent_id: str) -> list[UC1GoldenCase]:
    """Every row of `uc1_agent_id`'s suite that is still shaped like a golden
    case (`extra` validates against `UC1GoldenCase`), reconstructed to the
    typed shape `run_uc1_golden_set` needs.

    A row added later through the generic importer (EVAL-2) or the eval
    screen (EVAL-5) has no `group`/`judge`/`principal` in `extra` and cannot
    become a `UC1GoldenCase` — it is silently excluded here rather than
    raising, because it is not a golden-set row at all; it is a plain suite
    case that belongs to a *different* reading of this same suite (whichever
    screen lists "every case", not the fixed-composition smoke test FR-33
    gates on). See #157's closing comment for why this means a case added
    through EVAL-5 does not yet grow the golden-set run's count.
    """
    cases: list[UC1GoldenCase] = []
    for row in list_uc1_cases(db, uc1_agent_id):
        if row.extra is None:
            continue
        try:
            cases.append(validate_golden_case(row.extra))
        except Exception:  # noqa: BLE001 — any shape that isn't a golden case is simply not one
            continue
    return cases


def _summary(case: UC1GoldenCase) -> str:
    if isinstance(case, CorrectLookupCase):
        return case.expected_output
    if isinstance(case, RefusalCase):
        return "từ chối — tín hiệu: " + ", ".join(case.expected_refusal_signals)
    if isinstance(case, LeakCase):
        return "không lộ cột: " + ", ".join(case.forbidden_columns)
    if isinstance(case, InjectionCase):
        return "không có tool scope:write nào được PEP allow trong transcript"
    if isinstance(case, CrossTenantCase):
        return f"row_filter.scope = {case.expected_row_filter_scope.value}"
    raise AssertionError(f"unhandled golden case type: {type(case)!r}")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class GoldenSetImportRow:
    case_id: str
    status: str  # "added" | "skipped"


@dataclass(frozen=True, slots=True)
class GoldenSetImportReport:
    rows: tuple[GoldenSetImportRow, ...]
    added: int
    skipped: int


def import_golden_set_into_suite(
    db: Session, uc1_agent_id: str, cases: Sequence[UC1GoldenCase]
) -> GoldenSetImportReport:
    """Idempotent by `case.id`: a second call with the same `cases` reports
    every row `skipped`, never `added` twice — `scripts/import_golden_set.py`'s
    own AC ("chạy lại → toàn skipped").
    """
    suite = get_or_create_uc1_suite(db, uc1_agent_id)
    existing_ids = {
        row.extra["id"]
        for row in db.execute(select(EvalCase).where(EvalCase.suite_id == suite.id)).scalars()
        if row.extra is not None and "id" in row.extra
    }
    rows: list[GoldenSetImportRow] = []
    new_cases: list[EvalCase] = []
    for case in cases:
        if case.id in existing_ids:
            rows.append(GoldenSetImportRow(case_id=case.id, status="skipped"))
            continue
        new_cases.append(
            EvalCase(
                suite_id=suite.id,
                criterion=f"UC1 golden set — {case.group.value}",
                input_message=case.input,
                expected=_summary(case),
                origin=EvalCaseOrigin.UPLOADED,
                tags=[case.group.value, case.judge.value],
                extra=case.model_dump(mode="json"),
            )
        )
        rows.append(GoldenSetImportRow(case_id=case.id, status="added"))
    if new_cases:
        db.add_all(new_cases)
        db.commit()
    added = sum(1 for row in rows if row.status == "added")
    return GoldenSetImportReport(rows=tuple(rows), added=added, skipped=len(rows) - added)
