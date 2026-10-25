"""UC1-28 (#129), spec.md §11.2 — loads and validates `golden/uc1_ar_v3.jsonl`,
the golden set every version's smoke test grades before `submit-for-approval`
(FR-33, UC1-39 / #127) is allowed to fire.

Two grading paths, kept apart on purpose (decision 2026-08-13, §11.2's own
words: "cố tình không gộp chung"): eight cases with `judge: llm` go to the
DeepEval-*style* LLM judge (UC1-29 / #130 — one model call on this platform's
own seam, not the `deepeval` package's algorithm; that module's docstring says
exactly what it is and is not); the other fifteen with `judge: rule` are read
straight off a sandbox transcript (UC1-30 / #131) without ever asking a model to
decide a case that already has a machine-checkable answer.

This module only loads and validates the file's *shape* — one Pydantic model
per group, so a rule-based case cannot carry a free-form `expected_output`
(the AC's own "15 case rule-based không chứa expected_output dạng văn xuôi tự
do") and a `judge: llm` case cannot silently drift into `judge: rule`'s
territory. Nothing here runs a case; that is UC1-29/UC1-30's job.
"""

import json
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from src.agentspec.spec import DataScope

#: `golden_set_ref: golden/uc1_ar_v3.jsonl` in `agentspec.yaml` (§7) — relative
#: to the repo root, which is what this constant resolves to. A caller holding
#: a different root (a packaged deployment, a test fixture) passes its own
#: path to `load_golden_set` instead of relying on this default.
DEFAULT_GOLDEN_SET_PATH = Path(__file__).resolve().parents[2] / "golden" / "uc1_ar_v3.jsonl"


class GoldenCaseGroup(StrEnum):
    """The five groups §11.2's table names, each with its own fixed case count."""

    CORRECT_LOOKUP = "correct_lookup"
    REFUSAL = "refusal"
    LEAK = "leak"
    INJECTION = "injection"
    CROSS_TENANT = "cross_tenant"


class GoldenCaseJudge(StrEnum):
    """Which of §11.2's two graders reads this case — never both."""

    LLM = "llm"
    RULE = "rule"


#: How many cases each group must contain — §11.2's table, and this ticket's
#: own AC ("23 case tồn tại, đúng nhóm").
GROUP_SIZES: dict[GoldenCaseGroup, int] = {
    GoldenCaseGroup.CORRECT_LOOKUP: 8,
    GoldenCaseGroup.REFUSAL: 5,
    GoldenCaseGroup.LEAK: 4,
    GoldenCaseGroup.INJECTION: 3,
    GoldenCaseGroup.CROSS_TENANT: 3,
}

TOTAL_CASE_COUNT = sum(GROUP_SIZES.values())


class GoldenPrincipal(BaseModel):
    """Whoever is asking, in a case — the same three claims `src.policy.pep.Principal`
    takes from a JWT (`sub` is derived from `employee_id` at run time, never stored
    twice here).
    """

    model_config = ConfigDict(extra="forbid")

    employee_id: str = Field(..., min_length=1)
    roles: list[str] = Field(..., min_length=1)
    department: str = Field(..., min_length=1)


class _GoldenCaseBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    input: str = Field(..., min_length=1, description="Câu hỏi đưa cho agent trong lượt sandbox chạy case này")
    principal: GoldenPrincipal


class CorrectLookupCase(_GoldenCaseBase):
    """§11.2's "tra cứu đúng" — DeepEval's `LLMTestCase` field names (`input`/`expected_output`/`retrieval_context`).

    The *shape* is genuinely borrowed: a case written this way could be handed to
    the library unchanged. What grades it is not the library — see
    `src.evals.uc1_deepeval_runner`.
    """

    group: Literal[GoldenCaseGroup.CORRECT_LOOKUP]
    judge: Literal[GoldenCaseJudge.LLM]
    expected_output: str = Field(..., min_length=1)
    retrieval_context: list[str] = Field(..., min_length=1)


class RefusalCase(_GoldenCaseBase):
    """§11.2's "từ chối đúng" — graded by matching against a fixed set of refusal signals, never free prose."""

    group: Literal[GoldenCaseGroup.REFUSAL]
    judge: Literal[GoldenCaseJudge.RULE]
    expected_refusal_signals: list[str] = Field(..., min_length=1)


class LeakCase(_GoldenCaseBase):
    """§11.2's "rò rỉ cột nhạy cảm" — `forbidden_columns` names exactly which `prohibitions.never_expose` columns matter here."""

    group: Literal[GoldenCaseGroup.LEAK]
    judge: Literal[GoldenCaseJudge.RULE]
    forbidden_columns: list[str] = Field(..., min_length=1)


class InjectionCase(_GoldenCaseBase):
    """§11.2's "prompt injection" — no extra fields: the check is structural
    ("không có tool scope: write nào được PEP allow trong transcript"), not a
    comparison against anything this case itself carries.
    """

    group: Literal[GoldenCaseGroup.INJECTION]
    judge: Literal[GoldenCaseJudge.RULE]


class CrossTenantCase(_GoldenCaseBase):
    """§11.2's "phân quyền chéo" — `expected_row_filter_scope` is compared against
    the `row_filter.scope` the PEP actually applied, read straight off the transcript.
    """

    group: Literal[GoldenCaseGroup.CROSS_TENANT]
    judge: Literal[GoldenCaseJudge.RULE]
    expected_row_filter_scope: DataScope


UC1GoldenCase = Annotated[
    CorrectLookupCase | RefusalCase | LeakCase | InjectionCase | CrossTenantCase, Field(discriminator="group")
]

_CASE_ADAPTER: TypeAdapter[UC1GoldenCase] = TypeAdapter(UC1GoldenCase)


class GoldenSetShapeError(ValueError):
    """The file parsed as JSONL but its cases do not match §11.2's fixed composition.

    Carries every group's actual count so a caller sees the whole picture in one
    error, the same "collect every problem" shape `SpecValidationError` uses.
    """

    def __init__(self, counts: dict[GoldenCaseGroup, int]) -> None:
        self.counts = counts
        super().__init__(
            "golden set không đúng thành phần §11.2 — kỳ vọng "
            f"{dict(GROUP_SIZES)}, thực tế {counts}"
        )


def validate_golden_case(payload: dict[str, object]) -> UC1GoldenCase:
    """One golden case's raw dict (a JSONL line, or an `EvalCase.extra` a
    golden-set import wrote — see `src.evals.uc1_suite`), validated the same
    way `load_golden_set` validates a file's lines. The two share this one
    call so a case round-trips through `model_dump(mode="json")` and back
    without a second, hand-written mapping to keep in sync.
    """
    return _CASE_ADAPTER.validate_python(payload)


def load_golden_set(path: Path | str = DEFAULT_GOLDEN_SET_PATH) -> list[UC1GoldenCase]:
    """Every case in `path`, parsed and validated against its own group's shape.

    One JSON object per line (JSONL, not a JSON array) — the format `golden_set_ref`
    names and the one a golden set actually gets diffed and appended to line by line.
    Blank lines are skipped so a trailing newline is never a parse error.
    """
    text = Path(path).read_text(encoding="utf-8")
    return [_CASE_ADAPTER.validate_python(json.loads(line)) for line in text.splitlines() if line.strip()]


def group_counts(cases: Sequence[UC1GoldenCase]) -> dict[GoldenCaseGroup, int]:
    counts: dict[GoldenCaseGroup, int] = dict.fromkeys(GoldenCaseGroup, 0)
    for case in cases:
        counts[case.group] += 1
    return counts


def assert_golden_set_composition(cases: Sequence[UC1GoldenCase]) -> None:
    """Raise `GoldenSetShapeError` unless every group has exactly the count §11.2's table names."""
    counts = group_counts(cases)
    if counts != GROUP_SIZES:
        raise GoldenSetShapeError(counts)
