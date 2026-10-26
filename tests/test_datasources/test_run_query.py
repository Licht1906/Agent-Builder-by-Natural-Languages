"""`Dialect.run_query` against a real database, once per engine — DS-04 / ADR-0059 §5, DS-06.

A small receivables view is built in the test database for each test. What is
pinned here is the SQL the dialect builds from a checked query: values bound
as parameters, the asker's scope ANDed in before any grouping, the asker's
attributes set on the session, the row ceiling kept. The same assertions run
on Postgres and on SQL Server (`tests.test_datasources.backends`).
"""

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from src.datasources.dialects import QueryRequest
from src.datasources.query import StructuredQuery
from tests.test_datasources.backends import Backend

COLUMNS = ("customer_id", "sales_rep_id", "department", "amount_due", "days_overdue", "note")


@pytest.fixture(autouse=True)
def receivables(backend: Backend) -> Iterator[None]:
    backend.create_values_view(
        "v_test_ar",
        COLUMNS,
        [
            ("C1", "E102", "SALES", Decimal("100.50"), 45, "gọi lại"),
            ("C2", "E102", "SALES", Decimal("20.00"), 10, None),
            ("C3", "E200", "SALES", Decimal("300.00"), 60, "đã hứa trả"),
            ("C4", "E300", "FIN", Decimal("999.00"), 90, None),
        ],
    )
    backend.create_session_view("v_test_who", "employee_code")
    yield
    backend.drop_view("v_test_ar")
    backend.drop_view("v_test_who")


def _run(
    backend: Backend,
    query: dict[str, Any],
    *,
    scope: tuple[tuple[str, Any], ...] = (),
    session: dict[str, Any] | None = None,
    limit: int = 200,
    view_columns: tuple[str, ...] = COLUMNS,
) -> list[dict[str, Any]]:
    found = backend.dialect.run_query(
        backend.dsn,
        QueryRequest(
            query=StructuredQuery.model_validate(query),
            view_columns=view_columns,
            scope=scope,
            session_attributes=session or {},
            limit=limit,
        ),
        trusted_hosts=backend.trusted,
    )
    return [dict(zip(found.columns, row, strict=True)) for row in found.rows]


def test_filters_order_and_columns_are_what_the_query_asked_for(backend: Backend) -> None:
    rows = _run(
        backend,
        {
            "view": "v_test_ar",
            "columns": ["customer_id", "days_overdue"],
            "filters": [{"column": "days_overdue", "op": ">", "value": 30}],
            "order_by": [{"column": "days_overdue", "dir": "desc"}],
        },
    )

    assert rows == [
        {"customer_id": "C4", "days_overdue": 90},
        {"customer_id": "C3", "days_overdue": 60},
        {"customer_id": "C1", "days_overdue": 45},
    ]


def test_the_askers_scope_is_anded_in(backend: Backend) -> None:
    query = {"view": "v_test_ar", "columns": ["customer_id"], "order_by": [{"column": "customer_id", "dir": "asc"}]}

    rep = _run(backend, query, scope=(("sales_rep_id", "E102"),))
    lead = _run(backend, query, scope=(("department", "SALES"),))

    assert [row["customer_id"] for row in rep] == ["C1", "C2"]
    assert [row["customer_id"] for row in lead] == ["C1", "C2", "C3"]


def test_the_scope_applies_before_grouping(backend: Backend) -> None:
    rows = _run(
        backend,
        {
            "view": "v_test_ar",
            "columns": ["department"],
            "group_by": ["department"],
            "aggregates": [
                {"fn": "sum", "column": "amount_due", "as": "total"},
                {"fn": "count", "column": "*", "as": "n"},
            ],
        },
        scope=(("sales_rep_id", "E102"),),
    )

    assert rows == [{"department": "SALES", "total": pytest.approx(120.5), "n": 2}]


def test_a_value_that_looks_like_sql_is_only_ever_a_value(backend: Backend) -> None:
    rows = _run(
        backend,
        {
            "view": "v_test_ar",
            "columns": ["customer_id"],
            "filters": [{"column": "customer_id", "op": "=", "value": "'; DROP VIEW v_test_ar; --"}],
        },
    )

    assert rows == []
    assert len(_run(backend, {"view": "v_test_ar", "columns": ["customer_id"]})) == 4


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ([{"column": "customer_id", "op": "in", "value": ["C1", "C3"]}], ["C1", "C3"]),
        ([{"column": "customer_id", "op": "not_in", "value": ["C1", "C3"]}], ["C2", "C4"]),
        ([{"column": "days_overdue", "op": "between", "value": [10, 45]}], ["C1", "C2"]),
        ([{"column": "note", "op": "is_null"}], ["C2", "C4"]),
        ([{"column": "note", "op": "is_not_null"}], ["C1", "C3"]),
        ([{"column": "note", "op": "like", "value": "%hứa%"}], ["C3"]),
        ([{"column": "days_overdue", "op": "!=", "value": 45}], ["C2", "C3", "C4"]),
        ([{"column": "days_overdue", "op": "<=", "value": "45"}], ["C1", "C2"]),
    ],
)
def test_every_whitelisted_operator(backend: Backend, filters: list[dict[str, Any]], expected: list[str]) -> None:
    rows = _run(
        backend,
        {"view": "v_test_ar", "columns": ["customer_id"], "filters": filters, "order_by": [{"column": "customer_id"}]},
    )

    assert [row["customer_id"] for row in rows] == expected


def test_no_columns_means_every_column_of_the_view(backend: Backend) -> None:
    rows = _run(backend, {"view": "v_test_ar", "filters": [{"column": "customer_id", "op": "=", "value": "C4"}]})

    assert list(rows[0]) == list(COLUMNS)


def test_the_row_ceiling_is_kept(backend: Backend) -> None:
    assert len(_run(backend, {"view": "v_test_ar", "columns": ["customer_id"]}, limit=2)) == 2


def test_the_askers_attributes_are_on_the_session_for_the_query(backend: Backend) -> None:
    rows = _run(backend, {"view": "v_test_who"}, session={"employee_code": "E102"}, view_columns=("who",))

    assert rows == [{"who": "E102"}]


def test_an_attribute_name_that_is_not_a_plain_name_never_reaches_the_session(backend: Backend) -> None:
    rows = _run(backend, {"view": "v_test_who"}, session={"employee_code; reset all": "x"}, view_columns=("who",))

    assert rows == [{"who": None}]


def test_an_attribute_named_in_capitals_still_reaches_the_session(backend: Backend) -> None:
    """Setting names are case-insensitive in Postgres; an employee view's `Employee_Code` is the same setting."""
    rows = _run(backend, {"view": "v_test_who"}, session={"Employee_Code": "E102"}, view_columns=("who",))

    assert rows == [{"who": "E102"}]
