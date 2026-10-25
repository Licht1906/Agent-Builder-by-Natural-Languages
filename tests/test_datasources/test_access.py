"""Quy tắc truy cập — the engine's truth table, its save-time checks, and YAML both ways. DS-03 / ADR-0059 §6.

Pure: no database is touched. `resolve_principal` is handed a scripted lookup;
the dialect's real one has its own test against Postgres.
"""

import unicodedata
from typing import Any

import pytest
from pydantic import ValidationError

from src.datasources.access import (
    AccessRules,
    All,
    Deny,
    DenyCause,
    Principal,
    Where,
    access_problems,
    from_yaml,
    resolve_principal,
    scope_for,
    to_yaml,
)
from src.datasources.dialects import ColumnRef, Introspection, ViewInfo

#: The spec's own example, verbatim in shape.
SPEC_YAML = """\
identity:
  view: v_employees
  match_by: email
  attributes:
    employee_code: emp_id
    department: dept_code
    position: position
rules:
  - position: "Nhân viên KD"
    views:
      v_ar_open: { where: { sales_rep_id: employee_code } }
      v_customer_safe: { where: { sales_rep_id: employee_code } }
  - position: "Trưởng nhóm KD"
    views:
      v_ar_open: { where: { department: department } }
  - position: "Kế toán trưởng"
    views:
      v_ar_open: all
"""

RULES = from_yaml(SPEC_YAML)

REP = Principal(
    match_value="an@cty.vn", attributes={"employee_code": "E102", "department": "SALES", "position": "Nhân viên KD"}
)
LEAD = Principal(
    match_value="binh@cty.vn", attributes={"employee_code": "E200", "department": "SALES", "position": "Trưởng nhóm KD"}
)
CHIEF = Principal(
    match_value="chi@cty.vn", attributes={"employee_code": "E300", "department": "FIN", "position": "Kế toán trưởng"}
)


# --------------------------------------------------------------------------- #
# scope_for — the truth table
# --------------------------------------------------------------------------- #


def test_an_asker_who_is_not_in_the_employee_view_is_denied() -> None:
    assert isinstance(scope_for(RULES, None, "v_ar_open"), Deny)


def test_a_position_with_no_rule_is_denied() -> None:
    intern = Principal(
        match_value="x@cty.vn", attributes={"employee_code": "E9", "department": "SALES", "position": "Thực tập"}
    )

    assert isinstance(scope_for(RULES, intern, "v_ar_open"), Deny)


def test_a_view_the_rule_does_not_list_is_denied() -> None:
    assert isinstance(scope_for(RULES, CHIEF, "v_customer_safe"), Deny)
    assert isinstance(scope_for(RULES, LEAD, "v_customer_safe"), Deny)


def test_all_is_granted_only_where_it_is_written() -> None:
    assert scope_for(RULES, CHIEF, "v_ar_open") == All()
    assert not isinstance(scope_for(RULES, LEAD, "v_ar_open"), All)


def test_where_pairs_the_view_column_with_the_askers_own_value() -> None:
    assert scope_for(RULES, REP, "v_ar_open") == Where(pairs=(("sales_rep_id", "E102"),))
    assert scope_for(RULES, LEAD, "v_ar_open") == Where(pairs=(("department", "SALES"),))


def test_several_where_pairs_are_all_kept() -> None:
    rules = from_yaml(
        SPEC_YAML.replace(
            "{ where: { department: department } }", "{ where: { department: department, region: employee_code } }"
        )
    )

    assert scope_for(rules, LEAD, "v_ar_open") == Where(pairs=(("department", "SALES"), ("region", "E200")))


@pytest.mark.parametrize("missing", [None, ""])
def test_an_asker_whose_attribute_is_empty_is_denied_rather_than_matched_on_nothing(missing: Any) -> None:
    rep = Principal(match_value="an@cty.vn", attributes={**REP.attributes, "employee_code": missing})

    assert isinstance(scope_for(RULES, rep, "v_ar_open"), Deny)


def test_an_asker_with_no_position_is_denied() -> None:
    nobody = Principal(
        match_value="x@cty.vn", attributes={"employee_code": "E1", "department": "SALES", "position": None}
    )

    assert isinstance(scope_for(RULES, nobody, "v_ar_open"), Deny)


def test_a_deny_says_why_without_naming_another_asker() -> None:
    denied = scope_for(RULES, None, "v_ar_open")

    assert isinstance(denied, Deny)
    assert denied.cause is DenyCause.UNKNOWN_ASKER
    assert "không có quyền" in denied.reason


@pytest.mark.parametrize("value", [["E102", "E200"], {"code": "E102"}, b"E102"])
def test_a_where_value_that_is_not_a_single_scalar_is_denied(value: Any) -> None:
    """An array or json column on the employee view must not become `IN (...)` or a match on a blob."""
    rep = Principal(match_value="an@cty.vn", attributes={**REP.attributes, "employee_code": value})

    assert scope_for(RULES, rep, "v_ar_open") == Deny(cause=DenyCause.MISSING_ATTRIBUTE)


def test_a_where_value_is_trimmed_like_a_position() -> None:
    rep = Principal(match_value="an@cty.vn", attributes={**REP.attributes, "employee_code": "  E102 "})

    assert scope_for(RULES, rep, "v_ar_open") == Where(pairs=(("sales_rep_id", "E102"),))


def test_a_position_matches_through_spacing_and_unicode_composition() -> None:
    """Vietnamese typed on one keyboard and stored by another can differ only in how a diacritic is encoded."""
    decomposed = unicodedata.normalize("NFD", "  Nhân viên KD ")
    rep = Principal(match_value="an@cty.vn", attributes={**REP.attributes, "position": decomposed})

    assert scope_for(RULES, rep, "v_ar_open") == Where(pairs=(("sales_rep_id", "E102"),))


def test_a_position_that_differs_in_case_is_a_different_position() -> None:
    rep = Principal(match_value="an@cty.vn", attributes={**REP.attributes, "position": "nhân viên kd"})

    assert scope_for(RULES, rep, "v_ar_open") == Deny(cause=DenyCause.NO_RULE)


# --------------------------------------------------------------------------- #
# The model itself
# --------------------------------------------------------------------------- #


def test_identity_must_name_the_position_attribute() -> None:
    with pytest.raises(ValidationError, match="position"):
        from_yaml(SPEC_YAML.replace("    position: position\n", ""))


def test_a_grant_is_either_all_or_where_and_nothing_else() -> None:
    with pytest.raises(ValidationError):
        from_yaml(SPEC_YAML.replace("v_ar_open: all", "v_ar_open: everything"))
    with pytest.raises(ValidationError):
        from_yaml(SPEC_YAML.replace("v_ar_open: all", "v_ar_open: { where: {} }"))


def test_a_position_cannot_have_two_rules() -> None:
    with pytest.raises(ValidationError, match="Nhân viên KD"):
        from_yaml(SPEC_YAML + '  - position: "Nhân viên KD"\n    views:\n      v_ar_open: all\n')


def test_a_where_names_an_attribute_the_identity_declares() -> None:
    with pytest.raises(ValidationError, match="region"):
        from_yaml(SPEC_YAML.replace("{ where: { department: department } }", "{ where: { department: region } }"))


# --------------------------------------------------------------------------- #
# YAML both ways
# --------------------------------------------------------------------------- #


def test_yaml_round_trips_to_the_same_rules() -> None:
    assert from_yaml(to_yaml(RULES)) == RULES


def test_yaml_written_twice_is_written_the_same() -> None:
    once = to_yaml(RULES)

    assert to_yaml(from_yaml(once)) == once


def test_yaml_keeps_vietnamese_positions_readable() -> None:
    assert "Nhân viên KD" in to_yaml(RULES)


def test_yaml_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(ValueError):
        from_yaml("- just a list")


def test_yaml_with_the_same_view_twice_under_one_position_is_refused() -> None:
    """`safe_load` keeps the last of two keys; a grant that silently replaces another is not a grant anyone wrote."""
    doubled = SPEC_YAML.replace(
        "      v_ar_open: all\n", "      v_ar_open: all\n      v_ar_open: { where: { department: department } }\n"
    )

    with pytest.raises(ValueError, match="v_ar_open"):
        from_yaml(doubled)


# --------------------------------------------------------------------------- #
# access_problems — checked against the last probe, at save time
# --------------------------------------------------------------------------- #

SNAPSHOT = Introspection(
    views=(
        ViewInfo(name="v_employees", columns=("email", "emp_id", "dept_code", "position"), row_count=20),
        ViewInfo(name="v_ar_open", columns=("customer_id", "sales_rep_id", "department", "days_overdue"), row_count=90),
        ViewInfo(name="v_customer_safe", columns=("customer_id", "sales_rep_id"), row_count=40),
    ),
    base_tables=("fin_invoice",),
)
ALLOWED = ["v_employees", "v_ar_open", "v_customer_safe"]


def test_rules_that_match_the_database_have_no_problems() -> None:
    assert access_problems(RULES, allowed_views=ALLOWED, snapshot=SNAPSHOT) == []


def test_a_column_that_does_not_exist_is_named_exactly() -> None:
    rules = from_yaml(
        SPEC_YAML.replace("v_customer_safe: { where: { sales_rep_id:", "v_customer_safe: { where: { rep_code:")
    )

    assert access_problems(rules, allowed_views=ALLOWED, snapshot=SNAPSHOT) == [
        "view v_customer_safe không có cột rep_code (quy tắc của chức vụ Nhân viên KD)"
    ]


def test_an_identity_column_that_does_not_exist_is_named_exactly() -> None:
    rules = from_yaml(SPEC_YAML.replace("department: dept_code", "department: dept"))

    assert access_problems(rules, allowed_views=ALLOWED, snapshot=SNAPSHOT) == [
        "view nhân viên v_employees không có cột dept (thuộc tính department)"
    ]


def test_a_view_that_is_not_allowed_is_a_problem() -> None:
    problems = access_problems(RULES, allowed_views=["v_employees", "v_ar_open"], snapshot=SNAPSHOT)

    assert problems == ["view v_customer_safe chưa được tick cho phép (quy tắc của chức vụ Nhân viên KD)"]


def test_the_employee_view_must_be_allowed_too() -> None:
    problems = access_problems(RULES, allowed_views=["v_ar_open", "v_customer_safe"], snapshot=SNAPSHOT)

    assert problems[0] == "view nhân viên v_employees chưa được tick cho phép"


# --------------------------------------------------------------------------- #
# resolve_principal — the one function that reads anything
# --------------------------------------------------------------------------- #


def test_resolve_principal_reads_the_declared_attributes_off_the_employee_row() -> None:
    asked: list[tuple[str, str, Any, tuple[str, ...]]] = []

    def lookup(match: ColumnRef, value: Any, columns: tuple[str, ...]) -> dict[str, Any] | None:
        asked.append((match.view, match.column, value, columns))
        return {"emp_id": "E102", "dept_code": "SALES", "position": "Nhân viên KD"}

    principal = resolve_principal(RULES, "an@cty.vn", lookup=lookup)

    assert principal == REP
    assert asked == [("v_employees", "email", "an@cty.vn", ("emp_id", "dept_code", "position"))]


def test_resolve_principal_finds_nobody_when_the_lookup_does_not() -> None:
    assert resolve_principal(RULES, "la@cty.vn", lookup=lambda *_: None) is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_resolve_principal_never_looks_up_a_blank_value(blank: str) -> None:
    def lookup(*_: Any) -> dict[str, Any]:
        raise AssertionError("a blank asker must not reach the database")

    assert resolve_principal(RULES, blank, lookup=lookup) is None


def test_access_rules_parse_from_a_plain_dict_too() -> None:
    assert AccessRules.model_validate(RULES.model_dump(mode="json")) == RULES
