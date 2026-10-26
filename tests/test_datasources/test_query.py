"""The structured query a `data_query` node's model fills in — its shape and its checks. DS-04 / ADR-0059 §5.

No text-to-SQL: the model writes this JSON, the code checks it against what
the node may read, and only then does a dialect build SQL from it. Every
refusal here is a reason the model (and the creator reading the transcript)
can act on.
"""

import pytest
from pydantic import ValidationError

from src.datasources.query import StructuredQuery, effective_limit, query_problems

VIEWS = ["v_ar_open", "v_customer_safe"]
COLUMNS = {
    "v_ar_open": ["order_id", "customer_id", "sales_rep_id", "department", "amount_due", "days_overdue"],
    "v_customer_safe": ["customer_id", "customer_name"],
    "v_employees": ["email", "position"],
}

#: The spec's own example.
EXAMPLE = {
    "view": "v_ar_open",
    "columns": ["customer_id", "amount_due", "days_overdue"],
    "filters": [{"column": "days_overdue", "op": ">", "value": 30}],
    "group_by": [],
    "aggregates": [],
    "order_by": [{"column": "days_overdue", "dir": "desc"}],
    "limit": 50,
}


def _problems(document: dict) -> list[str]:
    return query_problems(StructuredQuery.model_validate(document), views=VIEWS, columns_by_view=COLUMNS)


def test_the_specs_example_is_a_valid_query() -> None:
    assert _problems(EXAMPLE) == []


def test_a_view_outside_the_nodes_views_is_refused_even_if_it_exists() -> None:
    assert _problems({**EXAMPLE, "view": "v_employees", "columns": ["email"], "filters": [], "order_by": []}) == [
        "view v_employees không nằm trong các view node này được đọc"
    ]


@pytest.mark.parametrize(
    ("change", "column"),
    [
        ({"columns": ["customer_id", "bank_account"]}, "bank_account"),
        ({"filters": [{"column": "tax_id", "op": "=", "value": "1"}]}, "tax_id"),
        ({"order_by": [{"column": "credit_limit", "dir": "asc"}]}, "credit_limit"),
        ({"group_by": ["region"], "columns": ["region"], "order_by": []}, "region"),
    ],
)
def test_a_column_the_view_does_not_have_is_refused_by_name(change: dict, column: str) -> None:
    assert f"view v_ar_open không có cột {column}" in _problems({**EXAMPLE, **change})


def test_an_operator_outside_the_whitelist_is_refused_before_anything_else() -> None:
    with pytest.raises(ValidationError):
        StructuredQuery.model_validate({**EXAMPLE, "filters": [{"column": "days_overdue", "op": "~*", "value": "x"}]})


@pytest.mark.parametrize(
    "bad",
    [
        {"column": "days_overdue", "op": "in", "value": 30},
        {"column": "days_overdue", "op": "in", "value": []},
        {"column": "days_overdue", "op": "between", "value": [1]},
        {"column": "days_overdue", "op": "is_null", "value": 3},
        {"column": "days_overdue", "op": ">", "value": None},
        {"column": "days_overdue", "op": "=", "value": [1, 2]},
        {"column": "days_overdue", "op": "=", "value": {"$gt": 1}},
    ],
)
def test_a_value_must_fit_its_operator(bad: dict) -> None:
    with pytest.raises(ValidationError):
        StructuredQuery.model_validate({**EXAMPLE, "filters": [bad]})


def test_a_filter_value_is_data_not_sql() -> None:
    """Accepted as an ordinary string — it is a bound parameter later, never part of the statement."""
    query = StructuredQuery.model_validate(
        {**EXAMPLE, "filters": [{"column": "customer_id", "op": "=", "value": "'; DROP TABLE fin_invoice; --"}]}
    )

    assert query_problems(query, views=VIEWS, columns_by_view=COLUMNS) == []


def test_aggregates_select_only_grouped_columns() -> None:
    grouped = {
        "view": "v_ar_open",
        "columns": ["department"],
        "group_by": ["department"],
        "aggregates": [{"fn": "sum", "column": "amount_due", "as": "total_due"}],
        "order_by": [{"column": "total_due", "dir": "desc"}],
    }

    assert _problems(grouped) == []
    assert _problems({**grouped, "columns": ["department", "customer_id"]}) == [
        "cột customer_id phải nằm trong group_by khi query có aggregate"
    ]


def test_count_star_needs_no_column() -> None:
    assert _problems({"view": "v_ar_open", "aggregates": [{"fn": "count", "column": "*", "as": "n"}]}) == []


def test_only_count_may_use_star() -> None:
    assert _problems({"view": "v_ar_open", "aggregates": [{"fn": "sum", "column": "*", "as": "n"}]}) == [
        "chỉ count mới dùng được * — sum cần một cột"
    ]


def test_an_alias_must_be_a_plain_name() -> None:
    with pytest.raises(ValidationError):
        StructuredQuery.model_validate(
            {"view": "v_ar_open", "aggregates": [{"fn": "count", "column": "*", "as": "n; DROP"}]}
        )


def test_order_by_names_something_the_query_returns() -> None:
    assert _problems({**EXAMPLE, "order_by": [{"column": "order_id", "dir": "asc"}]}) == [
        "order_by order_id không nằm trong các cột query trả về"
    ]


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        StructuredQuery.model_validate({**EXAMPLE, "sql": "SELECT 1"})


@pytest.mark.parametrize(("asked", "expected"), [(None, 200), (50, 50), (5000, 200)])
def test_the_limit_is_clamped_to_the_nodes_ceiling(asked: int | None, expected: int) -> None:
    query = StructuredQuery.model_validate({**EXAMPLE, "limit": asked})

    assert effective_limit(query, max_rows=200) == expected
