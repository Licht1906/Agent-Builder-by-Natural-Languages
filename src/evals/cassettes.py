"""Eval cassettes: the views an eval case reads, frozen when the case is written — EV-02 / ADR-0069 §2–3.

An eval run must grade a Baseline and a Candidate on the same data, and a live
database moves between two runs. So when a case is written, each view the
agent's `data_query` nodes may read is read once, for real, as the case's
`run_as` — through `PlatformDatabaseReader`, every gate and scope included —
and the rows are frozen in `eval_cassettes`. During the run the case's reader
is a `CassetteReader`: the model's query runs on those rows
(`src.datasources.in_memory`) and the creator's database is never touched.

Three facts decide whether a case can run at all, and each is a reason in the
run's `skipped[]` rather than a silent live read (`cassette_problem`):

- every `(source, view)` the graph reads has a cassette for this case;
- each cassette's view still has the columns it was recorded with
  (`schema_hash`) — a probe that found new ones makes it stale;
- an `AnswersFromQuery` case's answer key is not empty or all zeros, unless the
  case deliberately asks about nothing.

A case with no `run_as` needs no cassette: nobody is asking, so every read is
denied before any data would be read, live or frozen.

**Rehearsal is not affected.** A trial run still reads live
(`LIVE_READONLY`, ADR-0023 §4); only eval needs data that stands still.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.datasources.access import Deny, DenyCause
from src.datasources.dialects.base import QueryRows
from src.datasources.in_memory import run_in_memory
from src.datasources.query import StructuredQuery, effective_limit, query_problems
from src.datasources.reader import Asker, DatabaseReader, QueryOutcome, describe
from src.db.models import DatabaseSource, EvalCase, EvalCassette
from src.evals.checks import trivial_answer
from src.models.eval_check import AnswersFromQuery, check_of
from src.models.node_registry import DataQueryConfig, NodeType
from src.models.workflow import WorkflowJSON

#: The most rows one view is frozen with. A larger view is refused rather than
#: cut: a cassette missing rows would answer "which customer is most overdue"
#: from the ones that happened to fit.
SNAPSHOT_MAX_ROWS = 5000


def schema_hash(columns: Sequence[str]) -> str:
    """A view's columns, as the fingerprint a cassette is recorded against."""
    return hashlib.sha256(json.dumps(list(columns)).encode("utf-8")).hexdigest()[:16]


def data_reads(workflow: WorkflowJSON) -> list[tuple[str, str]]:
    """Every `(source_id, view)` a `data_query` node of `workflow` may read, in graph order."""
    pairs = [
        (str(node.config.get("source_id") or ""), str(view))
        for node in workflow.nodes_of_type(NodeType.DATA_QUERY)
        for view in node.config.get("views") or []
    ]
    return list(dict.fromkeys(pair for pair in pairs if pair[0]))


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RecordReport:
    """What one recording pass did: how many cases got fresh cassettes, and why the others could not."""

    recorded: int = 0
    problems: dict[int, str] = field(default_factory=dict)


def record_case(db: Session, case: EvalCase, workflow: WorkflowJSON, reader: DatabaseReader) -> str | None:
    """Freeze every view `workflow` reads as `case.run_as` sees it, replacing the case's old cassettes.

    Returns why the case cannot be recorded, or `None`. A read the asker is
    denied is frozen as the denial — that is the answer the case is about — and
    a read that fails is frozen as the failure, so the case is skipped with
    the reason rather than run on half its data.
    """
    db.execute(delete(EvalCassette).where(EvalCassette.case_id == case.id))
    if not case.run_as:
        db.commit()
        return None
    asker = reader.asked_by(case.run_as)
    problems: list[str] = []
    for source_id, view in data_reads(workflow):
        config = DataQueryConfig.model_construct(source_id=source_id, views=[view], max_rows=SNAPSHOT_MAX_ROWS + 1)
        columns = asker.columns_of(config).get(view)
        if columns is None:
            problems.append(f"Hệ CSDL {source_id} không cho đọc view {view}")
            continue
        outcome = asker.read(config, StructuredQuery(view=view))
        db.add(
            EvalCassette(
                case_id=case.id,
                source_id=int(source_id),
                view=view,
                run_as=case.run_as,
                schema_hash=schema_hash(columns),
                payload=_frozen(outcome, columns),
            )
        )
    db.commit()
    return "; ".join(problems) or None


def record_missing(
    db: Session, cases: Iterable[EvalCase], workflow: WorkflowJSON, reader: DatabaseReader, *, again: bool = False
) -> RecordReport:
    """Record every case whose cassettes are missing or stale for `workflow` — every case, when `again`.

    What the doors that write cases call after writing them, and what "Ghi lại
    dữ liệu đóng băng" calls for the whole suite. A graph that reads no Hệ CSDL
    records nothing.
    """
    if not data_reads(workflow):
        return RecordReport()
    sources = _sources(db, workflow)
    recorded = 0
    problems: dict[int, str] = {}
    for case in cases:
        if not again and not _needs_recording(case, workflow, _cassettes_of(db, [case.id]).get(case.id, []), sources):
            continue
        problem = record_case(db, case, workflow, reader)
        if problem:
            problems[case.id] = problem
        else:
            recorded += 1
    return RecordReport(recorded=recorded, problems=problems)


def _frozen(outcome: QueryOutcome, columns: Sequence[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {"source_name": outcome.source_name, "columns": list(columns)}
    if outcome.denied is not None:
        payload["denied"] = {"cause": outcome.denied.cause.value, "reason": outcome.denied.reason}
    elif outcome.rows is None:
        payload["error"] = outcome.refusal or "không đọc được"
    elif len(outcome.rows.rows) > SNAPSHOT_MAX_ROWS:
        payload["error"] = f"view có hơn {SNAPSHOT_MAX_ROWS} dòng trong phạm vi người hỏi — quá lớn để đóng băng"
    else:
        payload["columns"] = list(outcome.rows.columns)
        payload["rows"] = [[_json_value(value) for value in row] for row in outcome.rows.rows]
        payload["scope"] = [[column, _json_value(value)] for column, value in (outcome.scope or ())]
    return payload


def _json_value(value: Any) -> Any:
    """A database value as a JSON scalar: an integral decimal as an int, dates as ISO strings."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


# --------------------------------------------------------------------------- #
# Can the case run?
# --------------------------------------------------------------------------- #


def cassettes_for(db: Session, case_ids: Sequence[int]) -> dict[int, list[EvalCassette]]:
    """Every case's cassettes, by case id — one query for a whole run."""
    return _cassettes_of(db, case_ids)


def cassette_problem(
    case: EvalCase,
    workflow: WorkflowJSON,
    cassettes: Sequence[EvalCassette],
    sources: Mapping[str, DatabaseSource],
) -> str | None:
    """Why `case` cannot run on frozen data against `workflow` — or `None` when it can.

    The sentences say *not run*, never *failed*: the fix is recording again
    (or correcting the case's query), not rewriting the agent.
    """
    reads = data_reads(workflow)
    if not reads or not case.run_as:
        return None
    by_key = {(str(cassette.source_id), cassette.view): cassette for cassette in cassettes}
    for source_id, view in reads:
        cassette = by_key.get((source_id, view))
        if cassette is None or cassette.run_as != case.run_as:
            return f"Chưa có dữ liệu đóng băng của view {view} cho {case.run_as} — bấm “Ghi lại dữ liệu đóng băng”"
        source = sources.get(source_id)
        if source is None:
            return f"Hệ CSDL {source_id} của view {view} không còn — không có dữ liệu để chấm"
        if cassette.schema_hash != schema_hash(source.view_columns.get(view, [])):
            return f"Cấu trúc view {view} đã đổi từ lúc đóng băng dữ liệu — bấm “Ghi lại dữ liệu đóng băng”"
        if "error" in cassette.payload:
            return f"Không đóng băng được view {view}: {cassette.payload['error']}"
    check = check_of(case.expected_check)
    if isinstance(check, AnswersFromQuery):
        key = answer_key(check, cassettes, run_as=case.run_as)
        if key is None:
            return f"Không tính được đáp án từ dữ liệu đóng băng của view {check.query.view}"
        empty = trivial_answer(key, check.query)
        if check.expects_no_rows and key.rows:
            return "Case kiểm “không có dữ liệu” nhưng dữ liệu đóng băng có dòng khớp — sửa truy vấn của case"
        if empty and not check.expects_no_rows:
            return "Đáp án tính từ truy vấn là rỗng hoặc toàn số 0 — case không chấm được gì; sửa truy vấn của case"
    return None


def sources_of(db: Session, workflow: WorkflowJSON) -> dict[str, DatabaseSource]:
    """The Hệ CSDL rows `workflow` reads, by id as the graph spells it."""
    return _sources(db, workflow)


def answer_key(check: AnswersFromQuery, cassettes: Sequence[EvalCassette], *, run_as: str | None) -> QueryRows | None:
    """`check.query` run on the case's frozen view — the answer key, computed. `None` when there is none."""
    reader = CassetteReader.of(cassettes, asker=run_as)
    found = next((cassette for cassette in cassettes if cassette.view == check.query.view), None)
    if found is None:
        return None
    config = DataQueryConfig.model_construct(
        source_id=str(found.source_id), views=[found.view], max_rows=SNAPSHOT_MAX_ROWS
    )
    return reader.read(config, check.query).rows


def _needs_recording(
    case: EvalCase, workflow: WorkflowJSON, cassettes: Sequence[EvalCassette], sources: Mapping[str, DatabaseSource]
) -> bool:
    if not case.run_as:
        return False
    by_key = {(str(cassette.source_id), cassette.view): cassette for cassette in cassettes}
    for source_id, view in data_reads(workflow):
        cassette = by_key.get((source_id, view))
        source = sources.get(source_id)
        if cassette is None or cassette.run_as != case.run_as or source is None:
            return True
        if cassette.schema_hash != schema_hash(source.view_columns.get(view, [])):
            return True
    return False


def _cassettes_of(db: Session, case_ids: Sequence[int]) -> dict[int, list[EvalCassette]]:
    found: dict[int, list[EvalCassette]] = {}
    if not case_ids:
        return found
    for cassette in db.scalars(select(EvalCassette).where(EvalCassette.case_id.in_(list(case_ids)))):
        found.setdefault(cassette.case_id, []).append(cassette)
    return found


def _sources(db: Session, workflow: WorkflowJSON) -> dict[str, DatabaseSource]:
    found: dict[str, DatabaseSource] = {}
    for source_id, _ in data_reads(workflow):
        if source_id in found:
            continue
        try:
            source = db.get(DatabaseSource, int(source_id))
        except ValueError:
            source = None
        if source is not None:
            found[source_id] = source
    return found


# --------------------------------------------------------------------------- #
# Replaying
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Frozen:
    """One cassette's payload, detached from its row so a run can hold it after the session closes."""

    source_id: str
    view: str
    run_as: str | None
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CassetteReader:
    """`DatabaseReader` over one case's cassettes — what every `data_query` in an eval run reads through.

    Answers the four gates' questions from what was frozen: a view the case
    has no cassette for is refused, a query naming columns the view did not
    have is refused (`query_problems`, as live), a frozen denial is a denial,
    and anything else is the model's query run on the frozen rows. Nothing
    here can reach a database.
    """

    frozen: tuple[_Frozen, ...]
    asker: str | None

    @classmethod
    def of(cls, cassettes: Iterable[EvalCassette], *, asker: str | None) -> "CassetteReader":
        return cls(
            frozen=tuple(
                _Frozen(str(cassette.source_id), cassette.view, cassette.run_as, dict(cassette.payload))
                for cassette in cassettes
            ),
            asker=asker,
        )

    def asked_by(self, asker: str | None) -> "CassetteReader":
        return replace(self, asker=asker or None)

    def askers(self, configs: Sequence[DataQueryConfig], text: str) -> list[Asker]:
        return []

    def columns_of(self, config: DataQueryConfig) -> dict[str, list[str]]:
        return {
            item.view: list(item.payload.get("columns", []))
            for item in self._for(config.source_id)
            if item.view in config.views
        }

    def read(self, config: DataQueryConfig, query: StructuredQuery) -> QueryOutcome:
        if not self.asker:
            return QueryOutcome(
                rows=None, source_name="", view=query.view, description="", denied=Deny(cause=DenyCause.UNKNOWN_ASKER)
            )
        frozen = {item.view: item for item in self._for(config.source_id) if item.run_as == self.asker}
        name = next(iter(frozen.values())).payload.get("source_name", "") if frozen else ""
        columns = {view: list(item.payload.get("columns", [])) for view, item in frozen.items()}
        problems = query_problems(
            query, views=[view for view in config.views if view in frozen], columns_by_view=columns
        )
        if problems:
            refusal = "; ".join(problems) if frozen else f"không có dữ liệu đóng băng cho view {query.view}"
            return QueryOutcome(rows=None, source_name=name, view=query.view, description="", refusal=refusal)
        payload = frozen[query.view].payload
        if "denied" in payload:
            denied = Deny(cause=DenyCause(payload["denied"]["cause"]), reason=payload["denied"]["reason"])
            return QueryOutcome(rows=None, source_name=name, view=query.view, description="", denied=denied)
        if "error" in payload:
            return QueryOutcome(rows=None, source_name=name, view=query.view, description="", refusal=payload["error"])
        rows = run_in_memory(
            query,
            columns=payload["columns"],
            rows=payload["rows"],
            limit=effective_limit(query, max_rows=config.max_rows),
        )
        scope = tuple((str(column), value) for column, value in payload.get("scope", []))
        return QueryOutcome(
            rows=rows,
            source_name=name,
            view=query.view,
            description=describe(query, scoped=bool(scope)),
            scope=scope,
        )

    def _for(self, source_id: str) -> list[_Frozen]:
        return [item for item in self.frozen if item.source_id == str(source_id)]
