"""Importing/exporting a creator's own test set — one schema, three formats. ADR-0054 §2.

The golden set lecture point (s40-44) is that a test set belongs to the person
who understands the business, and that person usually already has it in a
spreadsheet. Before this module the only way in was one case at a time through
a form (`src.evals.cases.add_case`); UC-1's own golden set lived as a JSONL file
edited by pull request because there was nowhere else to put it. Both are the
same gap: no door from "a file I already have" to a suite a run can read.

One schema serves CSV, JSONL and XLSX, because a creator's tool of choice is not
this platform's business, and because export has to answer import — round-trip
is what lets a team that wants its test set in git keep it there.

Row-level, never file-level. A file with 18 good rows and 2 broken ones must not
lose the 18 to the 2 (ADR-0054 §2's "dòng lỗi không làm hỏng dòng khác") — every
row gets its own verdict, and the only whole-file refusal is the row-count
ceiling: each case is a full sandboxed run plus a judging call, and 500 is where
"upload my test set" stops being distinguishable from "run up the creator's
bill" (ADR-0011's own phrase for the same trade, reused here rather than
reinvented).
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any, Literal

RowStatus = Literal["added", "skipped", "error"]

from openpyxl import load_workbook
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Agent, AgentVersion, EvalCase, EvalCaseOrigin
from src.evals.applicability import Applicability, applicability_of, ungrounded_reason
from src.evals.cases import get_or_create_suite, get_suite
from src.models.eval_check import ExpectedCheck, check_of, check_payload
from src.models.solution_spec import SuccessCriterionCategory
from src.models.workflow import WorkflowJSON
from src.solution.specs import SpecStageError, solution_spec_of
from src.workflow.generator import WorkflowStageError, workflow_of

#: ADR-0054 §2 — "mỗi case sau này là một lần chạy sandbox + judge": the ceiling
#: is a cost ceiling, not a format limitation.
MAX_ROWS = 500

#: The columns the general schema names. Anything else lands in `extra`.
CANONICAL_COLUMNS = frozenset({"input", "expected", "criterion", "category", "expected_check", "tags"})

#: Columns accepted in place of `expected` when it is absent — ADR-0054 §2's own
#: reconciliation of UC-1's golden-set shape (`expected_output`,
#: `expected_refusal_signals`) with the general schema. The original column is
#: also kept in `extra`, unabridged, so nothing the file said is lost to the
#: mapping.
_EXPECTED_ALIASES = ("expected_output", "expected_refusal_signals")

#: Same idea for `tags` — ADR-0054 §2 names `group` as UC-1's stand-in for a tag.
_TAG_ALIASES = ("group",)

#: ADR-0054 §2: a row with no `criterion` is the creator's own case, judge-graded.
DEFAULT_CRITERION = "Người tạo tự nạp"

_TEMPLATE_HEADER = ["input", "expected", "criterion", "category", "expected_check", "tags"]
_TEMPLATE_ROWS = [
    [
        "Khách hỏi phí giao hàng nội thành bao nhiêu?",
        "Trả lời đúng biểu phí giao hàng nội thành trong tài liệu",
        "Trả lời đúng chính sách phí giao hàng",
        "correctness",
        "",
        "chính-sách;phí",
    ],
    [
        "Huỷ đơn DH-9 giúp tôi",
        "Workflow phải gọi hành động orders.cancel_order",
        "",
        "tool_correctness",
        '{"kind": "calls_action", "action": "orders.cancel_order"}',
        "",
    ],
    [
        "Thời tiết hôm nay thế nào?",
        "Từ chối trả lời vì ngoài phạm vi nghiệp vụ",
        "Từ chối câu hỏi ngoài phạm vi",
        "correctness",
        "",
        "ngoài-phạm-vi",
    ],
]


class SuiteImportError(ValueError):
    """A whole file that cannot be imported — unreadable format, or over the row ceiling. 422 at the route."""


@dataclass(frozen=True, slots=True)
class ImportedRow:
    """One row of an uploaded file, normalized to the general schema's column names."""

    row_number: int
    input: str | None
    expected: str | None
    criterion: str | None
    category: str | None
    expected_check_raw: object | None
    tags: list[str]
    extra: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RowResult:
    """One row's verdict — always one of these, never a raised exception past parsing."""

    row: int
    status: RowStatus
    reason: str | None = None
    case: EvalCase | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class ImportReport:
    rows: list[RowResult]
    dry_run: bool

    @property
    def added_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "added")

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "skipped")

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "error")


# --------------------------------------------------------------------------- #
# Parsing: three formats → one shape
# --------------------------------------------------------------------------- #


def parse_rows(content: bytes, filename: str) -> list[ImportedRow]:
    """`content` as the file it claims to be (by extension) — normalized rows, not yet validated."""
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix == "csv":
        raw_rows = _read_csv(content)
    elif suffix in ("jsonl", "ndjson"):
        raw_rows = _read_jsonl(content)
    elif suffix in ("xlsx", "xlsm"):
        raw_rows = _read_xlsx(content)
    else:
        raise SuiteImportError(f"Không đọc được định dạng '.{suffix}' — chỉ nhận CSV, JSONL, XLSX")
    return [_normalize(raw, row_number) for row_number, raw in enumerate(raw_rows, start=1)]


def _read_csv(content: bytes) -> list[dict[str, Any]]:
    text = content.decode("utf-8-sig")  # utf-8-sig eats a BOM if present, no-ops if not
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _read_jsonl(content: bytes) -> list[dict[str, Any]]:
    text = content.decode("utf-8-sig")
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SuiteImportError(f"Dòng JSONL không đọc được: {exc}") from None
        if not isinstance(parsed, dict):
            raise SuiteImportError("Mỗi dòng JSONL phải là một object")
        rows.append(parsed)
    return rows


def _read_xlsx(content: bytes) -> list[dict[str, Any]]:
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several distinct types for a bad file
        raise SuiteImportError(f"Không đọc được file XLSX: {exc}") from None
    sheet = workbook.active
    if sheet is None:
        raise SuiteImportError("File XLSX không có sheet nào")
    rows_iter = sheet.iter_rows(values_only=True)
    try:
        header = [str(cell).strip() if cell is not None else "" for cell in next(rows_iter)]
    except StopIteration:
        return []
    rows: list[dict[str, Any]] = []
    for values in rows_iter:
        if all(value is None for value in values):
            continue
        rows.append({header[i]: values[i] for i in range(len(header)) if i < len(values)})
    return rows


def _normalize(raw: dict[str, Any], row_number: int) -> ImportedRow:
    """One raw row (any format) as an `ImportedRow` — alias resolution, `extra` catch-all."""
    extra: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in CANONICAL_COLUMNS and value not in (None, ""):
            extra[key] = value

    expected = _clean_str(raw.get("expected"))
    if expected is None:
        for alias in _EXPECTED_ALIASES:
            value = raw.get(alias)
            if value not in (None, ""):
                expected = _stringify(value)
                break
    if expected is None and raw.get("group") not in (None, ""):
        # A row whose grading lives entirely in a bespoke rule this general
        # schema does not model (e.g. UC-1's `forbidden_columns`/row-scope
        # checks — ADR-0054 §6/§7 leaves that reading to `uc1_golden_set_runner`,
        # not to this importer). Rather than reject a row EVAL-2's own test
        # names by path ("golden/uc1_ar_v3.jsonl nạp được... extra.principal
        # còn nguyên"), give it a derived, honest placeholder and point at
        # `extra` — nothing about the row's real rule is lost, since every
        # column that produced it (`forbidden_columns`, `judge`, `group`, …)
        # is preserved there unabridged.
        expected = f"Theo quy tắc nhóm '{raw['group']}' — xem 'extra' để biết chi tiết"

    tags = _parse_tags(raw.get("tags"))
    for alias in _TAG_ALIASES:
        value = raw.get(alias)
        if value not in (None, "") and str(value) not in tags:
            tags.append(str(value))

    return ImportedRow(
        row_number=row_number,
        input=_clean_str(raw.get("input")),
        expected=expected,
        criterion=_clean_str(raw.get("criterion")),
        category=_clean_str(raw.get("category")),
        expected_check_raw=raw.get("expected_check") or None,
        tags=tags,
        extra=extra,
    )


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _stringify(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return str(value).strip()


def _parse_tags(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [tag.strip() for tag in str(value).split(";") if tag.strip()]


# --------------------------------------------------------------------------- #
# Validation + import
# --------------------------------------------------------------------------- #


def import_suite(db: Session, agent: Agent, *, content: bytes, filename: str, dry_run: bool) -> ImportReport:
    """Validate every row of `content`, and — unless `dry_run` — write the good ones into `agent`'s suite.

    A whole-file `SuiteImportError` (unreadable format, over `MAX_ROWS`) stops
    before any row is even looked at. Past that point nothing raises: every row
    gets a `RowResult`, and `dry_run=True` runs the exact same validation with
    the write skipped, so the report a creator sees before committing is the
    report they would have gotten from actually running it.
    """
    rows = parse_rows(content, filename)
    if len(rows) > MAX_ROWS:
        raise SuiteImportError(f"File có {len(rows)} dòng, vượt quá tối đa {MAX_ROWS} dòng một lần nạp")

    suite = get_suite(db, agent.id)
    existing_keys = _existing_keys(db, suite.id) if suite is not None else set()

    version = _latest_version(db, agent)
    workflow = _workflow_of_or_none(version)
    category_by_criterion = _category_by_criterion(version)

    results: list[RowResult] = []
    to_write: list[EvalCase] = []
    for row in rows:
        result = _validate_row(row, existing_keys=existing_keys, workflow=workflow, category_by_criterion=category_by_criterion)
        results.append(result)
        if result.status == "added" and result.case is not None:
            existing_keys.add((result.case.input_message, result.case.criterion))
            to_write.append(result.case)

    if not dry_run and to_write:
        suite = get_or_create_suite(db, agent.id)
        for case in to_write:
            case.suite_id = suite.id
        db.add_all(to_write)
        db.commit()
        for case in to_write:
            db.refresh(case)

    return ImportReport(rows=results, dry_run=dry_run)


def _existing_keys(db: Session, suite_id: int) -> set[tuple[str, str]]:
    rows = db.execute(select(EvalCase.input_message, EvalCase.criterion).where(EvalCase.suite_id == suite_id))
    return {(input_message, criterion) for input_message, criterion in rows}


def _latest_version(db: Session, agent: Agent) -> AgentVersion | None:
    from src.agents.versions import list_versions  # local import: avoids a routes<->cases<->versions cycle

    versions = list_versions(db, agent)
    return versions[-1] if versions else None


def _workflow_of_or_none(version: AgentVersion | None) -> WorkflowJSON | None:
    if version is None:
        return None
    try:
        return workflow_of(version)
    except WorkflowStageError:
        return None


def _category_by_criterion(version: AgentVersion | None) -> dict[str, SuccessCriterionCategory]:
    if version is None:
        return {}
    try:
        spec = solution_spec_of(version)
    except SpecStageError:
        return {}
    return {criterion.text: criterion.category for criterion in spec.success_criteria}


def _validate_row(
    row: ImportedRow,
    *,
    existing_keys: set[tuple[str, str]],
    workflow: WorkflowJSON | None,
    category_by_criterion: dict[str, SuccessCriterionCategory],
) -> RowResult:
    if not row.input:
        return RowResult(row=row.row_number, status="error", reason="Thiếu cột 'input'")
    if not row.expected:
        return RowResult(row=row.row_number, status="error", reason="Thiếu cột 'expected'")

    criterion = row.criterion or DEFAULT_CRITERION

    expected_check: ExpectedCheck | None = None
    if row.expected_check_raw is not None:
        try:
            raw = row.expected_check_raw
            payload = raw if isinstance(raw, dict) else json.loads(raw if isinstance(raw, (str, bytes)) else str(raw))
            expected_check = check_of(payload)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            return RowResult(row=row.row_number, status="error", reason=f"'expected_check' không đọc được: {exc}")

    key = (row.input, criterion)
    if key in existing_keys:
        return RowResult(row=row.row_number, status="skipped", reason="Đã có case trùng (input, criterion)")

    category = _resolve_category(row.category, criterion, category_by_criterion)
    if row.category is not None and category is None:
        return RowResult(row=row.row_number, status="error", reason=f"'category' không hợp lệ: {row.category!r}")

    case = EvalCase(
        introduced_in_version_id=None,
        criterion=criterion,
        input_message=row.input,
        expected=row.expected,
        origin=EvalCaseOrigin.UPLOADED,
        category=category,
        expected_check=None if expected_check is None else check_payload(expected_check),
        tags=row.tags,
        extra=row.extra or None,
    )

    # Classified whether or not the row carries a rule. Since ADR-0054 §4's
    # `applies_when` became enforceable there is a second way for a row to be
    # ungrounded — a `context_precision` case imported against a graph that
    # retrieves nothing — and an importer that only warned about rules would
    # accept it silently and let the creator find out at the next run.
    reason = None
    applicability = applicability_of(case, success_criteria=frozenset(category_by_criterion), workflow=workflow)
    if applicability is Applicability.UNGROUNDED:
        reason = f"Đã thêm, nhưng: {ungrounded_reason(case)}"

    return RowResult(row=row.row_number, status="added", reason=reason, case=case)


def _resolve_category(
    raw_category: str | None, criterion: str, category_by_criterion: dict[str, SuccessCriterionCategory]
) -> SuccessCriterionCategory | None:
    if raw_category is not None:
        try:
            return SuccessCriterionCategory(raw_category)
        except ValueError:
            return None
    inferred = category_by_criterion.get(criterion)
    if inferred is not None:
        return inferred
    return SuccessCriterionCategory.CORRECTNESS


# --------------------------------------------------------------------------- #
# Export + template
# --------------------------------------------------------------------------- #


def export_suite(cases: list[EvalCase], fmt: str) -> bytes:
    """`cases` as a file in `fmt` ('csv' or 'jsonl') — the same schema `import_suite` reads.

    Round-trip is the point (ADR-0054 §2): exporting and re-importing into
    another agent must produce the same cases, so every field `_row_of` writes
    is one `_normalize`/`_validate_row` above already knows how to read back.
    """
    payloads = [_row_of(case) for case in cases]
    if fmt == "jsonl":
        return "\n".join(json.dumps(p, ensure_ascii=False) for p in payloads).encode("utf-8")
    if fmt == "csv":
        return _csv_of(payloads)
    raise SuiteImportError(f"Định dạng xuất không hỗ trợ: {fmt!r}")


def _row_of(case: EvalCase) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "input": case.input_message,
        "expected": case.expected,
        "criterion": case.criterion,
        "category": case.category.value if case.category else "",
        "expected_check": case.expected_check or "",
        "tags": list(case.tags or []),
    }
    if case.extra:
        payload.update(case.extra)
    return payload


def _csv_of(payloads: list[dict[str, Any]]) -> bytes:
    extra_columns: list[str] = []
    for payload in payloads:
        for key in payload:
            if key not in _TEMPLATE_HEADER and key not in extra_columns:
                extra_columns.append(key)
    header = _TEMPLATE_HEADER + extra_columns

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, extrasaction="ignore")
    writer.writeheader()
    for payload in payloads:
        row = dict(payload)
        row["tags"] = ";".join(row.get("tags") or [])
        if row.get("expected_check"):
            row["expected_check"] = json.dumps(row["expected_check"], ensure_ascii=False)
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def template_csv() -> bytes:
    """The blank starting point a creator downloads before writing their own set."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_TEMPLATE_HEADER)
    writer.writerows(_TEMPLATE_ROWS)
    return buffer.getvalue().encode("utf-8")
