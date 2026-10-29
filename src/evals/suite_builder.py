"""Building an agent's suite up to ≥100 live cases with next to no typing — EV-03 / ADR-0069 §1.

Three sources, consulted in this order, each only while the suite is short:

1. **The shared library** (`src.evals.library`): the templates whose
   `applies_when` the version's capabilities satisfy, filled by code. Pulled
   once per version — a template added to the library later reaches the suite
   when it is built for the *next* version, and a template edited later
   changes no case already filled (`extra.template_id` dedups; nothing reads
   the library back).
2. **Business cases from the data** (agents that read a Hệ CSDL): for each
   person the employee view knows and each view the graph reads, questions
   whose answer key is a structured query on that person's frozen view —
   counts, totals, the largest and smallest row, a threshold, a breakdown.
   Recorded like every case (EV-02); a case whose answer key comes out empty,
   all zeros or unreadable is dropped at once (ADR-0069 §3).
3. **The model** (`generate_workflow_cases`), once, for whatever criteria the
   spec states that have no generated case yet.

"Live" is counted the way a run classifies (`runnable_cases`): applicable to
this graph, with the frozen data it needs, and `live` rather than `orphan`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from src.datasources.query import StructuredQuery
from src.datasources.reader import DatabaseReader
from src.db.models import AgentVersion, EvalCase, EvalCaseOrigin, EvalCassette
from src.evals.applicability import Applicability
from src.evals.cases import get_or_create_suite, list_cases, save_generated
from src.evals.cassettes import answer_key, cassette_problem, cassettes_for, data_reads, record_missing, sources_of
from src.evals.checks import key_values
from src.evals.library import AgentProfile, LibraryTemplate, fill, load_library, matching, profile_of
from src.evals.suite_changes import retire_stale
from src.evals.workflow_cases import generate_workflow_cases, prohibition_criterion
from src.evals.workflow_runner import runnable_cases
from src.models.eval_check import AnswersFromQuery, check_of, check_payload, describe_check
from src.models.node_registry import DataQueryConfig
from src.models.solution_spec import AgentSolutionSpecification, SuccessCriterionCategory
from src.models.workflow import WorkflowJSON
from src.services.llm import ChatModelFactory

#: ADR-0069 §1: every agent's suite has at least this many live cases.
TARGET_LIVE = 100

#: The criterion every data case is filed under.
DATA_CRITERION = "Trả lời đúng số liệu tính từ dữ liệu, trong phạm vi người hỏi được xem"

#: A text column with this few distinct values is a breakdown worth asking for.
_MAX_GROUPS = 10


@dataclass(frozen=True, slots=True)
class BuildReport:
    """What one build added, what it dropped, and where the suite stands."""

    library_added: int
    data_added: int
    generated_added: int
    dropped: int
    live_count: int
    target: int
    retired: int = 0


async def build_suite(
    db: Session,
    version: AgentVersion,
    *,
    workflow: WorkflowJSON,
    spec: AgentSolutionSpecification,
    reader: DatabaseReader | None,
    model_factory: ChatModelFactory,
    library: Sequence[LibraryTemplate] | None = None,
    target: int = TARGET_LIVE,
) -> BuildReport:
    """Fill `version`'s agent suite towards `target` live cases — library, then data, then the model."""
    criteria = frozenset(criterion.text for criterion in spec.success_criteria)
    # EV-05: what this version can no longer grade leaves first; its new
    # criteria get cases below whether or not the suite is short.
    retired = retire_stale(db, version, workflow=workflow, spec=spec)
    profile = profile_of(workflow, spec, reader)

    library_added = 0
    if not _library_pulled_for(db, version):
        library_added = _add_library_cases(db, version, library if library is not None else load_library(), profile)
    if reader is not None:
        record_missing(db, list_cases(db, version.agent_id), workflow, reader)

    data_added = dropped = 0
    if reader is not None and data_reads(workflow) and _live(db, version, workflow, criteria) < target:
        data_added, dropped = _add_data_cases(db, version, workflow, reader, profile.askers, criteria, target)

    generated_added = 0
    missing = _uncovered(db, version, spec)
    if missing is not None or _live(db, version, workflow, criteria) < target:
        asked = missing if missing is not None and _live(db, version, workflow, criteria) >= target else spec
        generation = await generate_workflow_cases(asked, workflow, model_factory=model_factory)
        saved = save_generated(db, version.agent_id, version.id, generation)
        generated_added = len(saved.cases)
        if reader is not None:
            record_missing(db, saved.cases, workflow, reader)

    return BuildReport(
        library_added=library_added,
        data_added=data_added,
        generated_added=generated_added,
        dropped=dropped,
        live_count=_live(db, version, workflow, criteria),
        target=target,
        retired=len(retired),
    )


def _uncovered(
    db: Session, version: AgentVersion, spec: AgentSolutionSpecification
) -> AgentSolutionSpecification | None:
    """`spec` narrowed to the criteria and prohibitions no case of the suite covers yet — `None` when all are.

    A reworded criterion is a new criterion: its old cases were retired, and
    this is what gets it new ones even when the suite is already past 100.
    """
    covered = {case.criterion for case in list_cases(db, version.agent_id)}
    criteria = [criterion for criterion in spec.success_criteria if criterion.text not in covered]
    prohibitions = [item for item in spec.prohibitions if prohibition_criterion(item) not in covered]
    if not criteria and not prohibitions:
        return None
    return spec.model_copy(update={"success_criteria": criteria, "prohibitions": prohibitions})


# --------------------------------------------------------------------------- #
# 1. The library
# --------------------------------------------------------------------------- #


def _library_pulled_for(db: Session, version: AgentVersion) -> bool:
    """Whether this version has already taken its templates — the library moves only on a new version."""
    return any(
        case.origin is EvalCaseOrigin.LIBRARY and case.introduced_in_version_id == version.id
        for case in list_cases(db, version.agent_id)
    )


def _add_library_cases(
    db: Session, version: AgentVersion, library: Sequence[LibraryTemplate], profile: AgentProfile
) -> int:
    suite = get_or_create_suite(db, version.agent_id)
    have = {
        (str((case.extra or {}).get("template_id")), case.run_as)
        for case in list_cases(db, version.agent_id)
        if case.origin is EvalCaseOrigin.LIBRARY
    }
    rows = []
    for template in matching(library, profile):
        filled = fill(template, profile)
        if filled is None or (filled.template_id, filled.run_as) in have:
            continue
        rows.append(
            EvalCase(
                suite_id=suite.id,
                introduced_in_version_id=version.id,
                criterion=filled.criterion,
                input_message=filled.input_message,
                expected=_expected_of(filled.expected_check),
                origin=EvalCaseOrigin.LIBRARY,
                category=filled.category,
                expected_check=filled.expected_check,
                tags=list(filled.tags),
                run_as=filled.run_as,
                extra={"template_id": filled.template_id},
            )
        )
        have.add((filled.template_id, filled.run_as))
    db.add_all(rows)
    db.commit()
    return len(rows)


def _expected_of(payload: dict[str, Any]) -> str:
    check = check_of(payload)
    assert check is not None  # the library validator refuses a template without a rule
    return describe_check(check)


# --------------------------------------------------------------------------- #
# 2. Business cases from the data
# --------------------------------------------------------------------------- #


def _add_data_cases(
    db: Session,
    version: AgentVersion,
    workflow: WorkflowJSON,
    reader: DatabaseReader,
    askers: Sequence[str],
    criteria: frozenset[str],
    target: int,
) -> tuple[int, int]:
    """Data questions, asker by asker, until the suite is live enough — `(kept, dropped)`."""
    suite = get_or_create_suite(db, version.agent_id)
    asked = {(case.input_message, case.run_as) for case in list_cases(db, version.agent_id)}
    sources = sources_of(db, workflow)
    kept = dropped = 0
    for asker in askers:
        if _live(db, version, workflow, criteria) >= target:
            break
        rows = []
        for source_id, view in data_reads(workflow):
            for question, query in _questions(reader, asker, source_id, view):
                if (question, asker) in asked:
                    continue
                check = AnswersFromQuery(query=query)
                rows.append(
                    EvalCase(
                        suite_id=suite.id,
                        introduced_in_version_id=version.id,
                        criterion=DATA_CRITERION,
                        input_message=question,
                        expected=describe_check(check),
                        origin=EvalCaseOrigin.GENERATED,
                        category=SuccessCriterionCategory.CORRECTNESS,
                        expected_check=check_payload(check),
                        run_as=asker,
                        extra={"generator": "data"},
                    )
                )
                asked.add((question, asker))
        if not rows:
            continue
        db.add_all(rows)
        db.commit()
        record_missing(db, rows, workflow, reader)
        frozen = cassettes_for(db, [row.id for row in rows])
        for row in rows:
            if cassette_problem(row, workflow, frozen.get(row.id, []), sources) is None:
                row.expected = _stated_answer(row, frozen.get(row.id, []))
                kept += 1
            else:
                db.delete(row)
                dropped += 1
        db.commit()
    return kept, dropped


def _stated_answer(case: EvalCase, cassettes: Sequence[EvalCassette]) -> str:
    """The computed answer key in words — so the creator reads what "đạt" means, not only that a query decides it."""
    check = check_of(case.expected_check)
    assert isinstance(check, AnswersFromQuery)
    key = answer_key(check, cassettes, run_as=case.run_as)
    assert key is not None  # `cassette_problem` already refused a case without one
    return (
        f"Câu trả lời phải nêu: {', '.join(key_values(key, check.query))} — tính từ truy vấn trên dữ liệu đã đóng băng"
    )


def _questions(reader: DatabaseReader, asker: str, source_id: str, view: str) -> list[tuple[str, StructuredQuery]]:
    """The questions one asker's view supports, read off a live look at it — what the data actually has."""
    config = DataQueryConfig.model_construct(source_id=source_id, views=[view], max_rows=1000)
    outcome = reader.asked_by(asker).read(config, StructuredQuery(view=view))
    if outcome.rows is None or not outcome.rows.rows:
        return []
    columns = list(outcome.rows.columns)
    values = {column: [row[index] for row in outcome.rows.rows] for index, column in enumerate(columns)}
    numeric = [column for column in columns if _numeric(column, values[column])]
    texts = [column for column in columns if column not in numeric and not _identifier(column)]
    label = next((column for column in columns if column not in numeric), columns[0])

    questions: list[tuple[str, dict[str, Any]]] = [
        (
            f"Trong {view}, tôi đang có tất cả bao nhiêu dòng?",
            {"view": view, "aggregates": [{"fn": "count", "column": "*", "as": "so_dong"}]},
        )
    ]
    for column in numeric:
        threshold = int(median(float(value) for value in values[column] if value is not None))
        questions += [
            (
                f"Tổng {column} trong {view} của tôi là bao nhiêu?",
                {"view": view, "aggregates": [{"fn": "sum", "column": column, "as": "tong"}]},
            ),
            (
                f"Dòng nào trong {view} có {column} lớn nhất, và giá trị đó là bao nhiêu?",
                {"view": view, "columns": [label, column], "order_by": [{"column": column, "dir": "desc"}], "limit": 1},
            ),
            (
                f"Dòng nào trong {view} có {column} nhỏ nhất, và giá trị đó là bao nhiêu?",
                {"view": view, "columns": [label, column], "order_by": [{"column": column, "dir": "asc"}], "limit": 1},
            ),
            (
                f"Trong {view}, có bao nhiêu dòng có {column} lớn hơn {threshold}?",
                {
                    "view": view,
                    "filters": [{"column": column, "op": ">", "value": threshold}],
                    "aggregates": [{"fn": "count", "column": "*", "as": "so_dong"}],
                },
            ),
        ]
    for column in texts:
        distinct = {value for value in values[column] if value is not None}
        if 2 <= len(distinct) <= _MAX_GROUPS:
            questions.append(
                (
                    f"Trong {view}, mỗi {column} có bao nhiêu dòng?",
                    {
                        "view": view,
                        "group_by": [column],
                        "aggregates": [{"fn": "count", "column": "*", "as": "so_dong"}],
                        "order_by": [{"column": column, "dir": "asc"}],
                    },
                )
            )
    return [(question, StructuredQuery.model_validate(query)) for question, query in questions]


def _numeric(column: str, values: Sequence[Any]) -> bool:
    present = [value for value in values if value is not None]
    return (
        not _identifier(column)
        and bool(present)
        and all(not isinstance(value, bool) and _is_number(value) for value in present)
    )


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return not isinstance(value, str)


def _identifier(column: str) -> bool:
    """An id is a label, not a quantity — summing invoice numbers answers nothing."""
    return column == "id" or column.endswith("_id")


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #


def _live(db: Session, version: AgentVersion, workflow: WorkflowJSON, criteria: frozenset[str]) -> int:
    runnable, _ = runnable_cases(db, list_cases(db, version.agent_id), workflow=workflow, success_criteria=criteria)
    return sum(1 for applicability in runnable.values() if applicability is Applicability.LIVE)
