"""UC1-28 (#129) — one test per rule `load_golden_set` enforces, run against
the real `golden/uc1_ar_v3.jsonl` (not a copy): the file this ticket produces
*is* the artifact under test, the same way `test_agentspec/conftest.py`'s
`reference_spec` parses the real `agentspec.yaml` rather than a fixture of it.
"""

import json

import pytest
from pydantic import ValidationError

from src.agentspec.spec import DataScope
from src.evals.uc1_golden_set import (
    DEFAULT_GOLDEN_SET_PATH,
    GROUP_SIZES,
    TOTAL_CASE_COUNT,
    CorrectLookupCase,
    CrossTenantCase,
    GoldenCaseGroup,
    GoldenCaseJudge,
    GoldenSetShapeError,
    InjectionCase,
    LeakCase,
    RefusalCase,
    assert_golden_set_composition,
    group_counts,
    load_golden_set,
)


def test_the_real_golden_set_has_exactly_23_cases_in_the_right_groups() -> None:
    """AC: "23 case tồn tại, đúng nhóm, đúng metric, đúng cờ judge"."""
    cases = load_golden_set()
    assert len(cases) == TOTAL_CASE_COUNT == 23
    assert group_counts(cases) == GROUP_SIZES
    assert_golden_set_composition(cases)  # does not raise


def test_every_correct_lookup_case_is_llm_judged_with_deepeval_shaped_fields() -> None:
    cases = load_golden_set()
    lookup_cases = [case for case in cases if case.group is GoldenCaseGroup.CORRECT_LOOKUP]
    assert len(lookup_cases) == GROUP_SIZES[GoldenCaseGroup.CORRECT_LOOKUP]
    for case in lookup_cases:
        assert isinstance(case, CorrectLookupCase)
        assert case.judge is GoldenCaseJudge.LLM
        assert case.expected_output
        assert case.retrieval_context


def test_the_15_rule_based_cases_carry_no_free_prose_expected_output() -> None:
    """AC: "15 case rule-based không chứa expected_output dạng văn xuôi tự do"."""
    cases = load_golden_set()
    rule_cases = [case for case in cases if case.judge is GoldenCaseJudge.RULE]
    assert len(rule_cases) == 15
    for case in rule_cases:
        assert not hasattr(case, "expected_output")


def test_refusal_cases_carry_machine_checkable_refusal_signals() -> None:
    cases = load_golden_set()
    refusal_cases = [case for case in cases if case.group is GoldenCaseGroup.REFUSAL]
    assert len(refusal_cases) == GROUP_SIZES[GoldenCaseGroup.REFUSAL]
    for case in refusal_cases:
        assert isinstance(case, RefusalCase)
        assert case.expected_refusal_signals


def test_leak_cases_name_forbidden_columns_drawn_from_never_expose() -> None:
    cases = load_golden_set()
    leak_cases = [case for case in cases if case.group is GoldenCaseGroup.LEAK]
    assert len(leak_cases) == GROUP_SIZES[GoldenCaseGroup.LEAK]
    never_expose = {"bank_account", "tax_id", "personal_phone", "national_id", "credit_limit_internal"}
    for case in leak_cases:
        assert isinstance(case, LeakCase)
        assert set(case.forbidden_columns) <= never_expose


def test_injection_cases_carry_no_extra_expectation_the_check_is_structural() -> None:
    cases = load_golden_set()
    injection_cases = [case for case in cases if case.group is GoldenCaseGroup.INJECTION]
    assert len(injection_cases) == GROUP_SIZES[GoldenCaseGroup.INJECTION]
    for case in injection_cases:
        assert isinstance(case, InjectionCase)


def test_cross_tenant_cases_use_two_different_principals_with_an_expected_scope() -> None:
    cases = load_golden_set()
    cross_tenant_cases = [case for case in cases if case.group is GoldenCaseGroup.CROSS_TENANT]
    assert len(cross_tenant_cases) == GROUP_SIZES[GoldenCaseGroup.CROSS_TENANT]
    principals = {case.principal.employee_id for case in cross_tenant_cases}
    assert len(principals) >= 2, "phân quyền chéo cần ít nhất hai principal khác nhau"
    for case in cross_tenant_cases:
        assert isinstance(case, CrossTenantCase)
        assert case.expected_row_filter_scope in set(DataScope)


def test_every_case_id_is_unique() -> None:
    cases = load_golden_set()
    ids = [case.id for case in cases]
    assert len(set(ids)) == len(ids)


def test_a_group_mismatch_is_reported_by_name(tmp_path) -> None:
    """`assert_golden_set_composition` refuses a file with the wrong per-group counts."""
    bad_path = tmp_path / "bad.jsonl"
    one_lookup_case = {
        "id": "x1",
        "group": "correct_lookup",
        "judge": "llm",
        "input": "?",
        "principal": {"employee_id": "E1", "roles": ["sales_rep"], "department": "SALES"},
        "expected_output": "y",
        "retrieval_context": ["z"],
    }
    bad_path.write_text(json.dumps(one_lookup_case) + "\n", encoding="utf-8")

    cases = load_golden_set(bad_path)
    with pytest.raises(GoldenSetShapeError) as caught:
        assert_golden_set_composition(cases)
    assert caught.value.counts[GoldenCaseGroup.CORRECT_LOOKUP] == 1


def test_a_rule_case_cannot_smuggle_in_an_llm_only_field(tmp_path) -> None:
    """`extra="forbid"` — a refusal case cannot also carry `expected_output`."""
    bad_path = tmp_path / "bad.jsonl"
    smuggled = {
        "id": "x1",
        "group": "refusal",
        "judge": "rule",
        "input": "?",
        "principal": {"employee_id": "E1", "roles": ["sales_rep"], "department": "SALES"},
        "expected_refusal_signals": ["không thể"],
        "expected_output": "câu trả lời văn xuôi lẽ ra không được ở đây",
    }
    bad_path.write_text(json.dumps(smuggled) + "\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_golden_set(bad_path)


def test_default_path_points_at_the_committed_file() -> None:
    assert DEFAULT_GOLDEN_SET_PATH.name == "uc1_ar_v3.jsonl"
    assert DEFAULT_GOLDEN_SET_PATH.exists()
