"""UC1-19 (#135) — `default_tool_implementations`'s real `erp.query_receivables`/
`erp.query_customer`, against a freshly migrated database (`schema.sql`'s real
views), the same `migrated_database_url` pattern `test_uc1_roles.py` and
`test_seed_uc1_demo_data.py` already use.

UC1-20 (#136) added `_OWN_RECORDS`/`_DEPARTMENT`/`_ALL` — the same fixture data,
queried through three different `RowScope`s, is what proves §9.2's RLS is
actually reachable end to end through this module's own real SQL, not only
through the `SET LOCAL`-by-hand cases `test_uc1_roles.py` already covers.
"""

import psycopg
import pytest

from src.compiler.tools import RowScope, default_tool_implementations
from src.services.vault_secret_store import VaultUnavailableError
from tests.test_db.conftest import upgrade_to_head


@pytest.fixture
def migrated_database_url(empty_database_url: str) -> str:
    upgrade_to_head(empty_database_url)
    return empty_database_url


#: `scope="all"` — the same "see everything" view every pre-UC1-20 test in this
#: file already asserted, kept as a named constant rather than repeated inline
#: so a reader sees at a glance which tests are deliberately scope-agnostic.
_ALL = RowScope(principal_id="", department="", scope="all")


def _seed(dsn: str) -> None:
    """`cust-a`/`cust-b` belong to rep `E102` (`SALES`); `cust-c` belongs to a
    different rep in a different department — two disjoint receivables, which
    is what makes "two different `RowScope`s see two different row sets"
    (AC-2) a fact this fixture can actually demonstrate rather than assume.
    """
    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (customer_id, customer_unique_id, customer_city, customer_state) VALUES "
            "('cust-a', 'cust-a-u', 'Sao Paulo', 'SP'), ('cust-b', 'cust-b-u', 'Rio de Janeiro', 'RJ'), "
            "('cust-c', 'cust-c-u', 'Curitiba', 'PR')"
        )
        cur.execute(
            "INSERT INTO orders (order_id, customer_id, order_status, order_purchase_timestamp) VALUES "
            "('order-a', 'cust-a', 'delivered', now() - interval '60 days'), "
            "('order-b', 'cust-b', 'delivered', now() - interval '60 days'), "
            "('order-c', 'cust-c', 'delivered', now() - interval '60 days')"
        )
        cur.execute(
            "INSERT INTO fin_invoice (order_id, customer_id, amount_due, amount_paid, issued_at, due_date) VALUES "
            "('order-a', 'cust-a', 500.00, 0, current_date - 45, current_date - 45), "
            "('order-b', 'cust-b', 300.00, 0, current_date - 5, current_date - 5), "
            "('order-c', 'cust-c', 900.00, 0, current_date - 10, current_date - 10)"
        )
        cur.execute(
            "INSERT INTO crm_sales_assignment (customer_id, sales_rep_id, department) VALUES "
            "('cust-a', 'E102', 'SALES'), ('cust-b', 'E102', 'SALES'), ('cust-c', 'E999', 'SUPPORT')"
        )
        conn.commit()


def _tools(dsn: str) -> dict:
    return default_tool_implementations(resolve_credential=lambda ref: dsn)


def test_query_receivables_returns_real_rows_with_a_source_citation(migrated_database_url: str) -> None:
    _seed(migrated_database_url)
    impl = _tools(migrated_database_url)["erp.query_receivables"]

    result = impl({}, _ALL)

    assert result["source"] == "v_ar_open"
    assert result["row_count"] == 3
    assert {row["order_id"] for row in result["rows"]} == {"order-a", "order-b", "order-c"}
    assert "error" not in result


def test_query_receivables_filters_by_days_overdue_gt(migrated_database_url: str) -> None:
    _seed(migrated_database_url)
    impl = _tools(migrated_database_url)["erp.query_receivables"]

    result = impl({"days_overdue_gt": 30}, _ALL)

    assert [row["order_id"] for row in result["rows"]] == ["order-a"]


def test_query_receivables_never_projects_never_expose_columns(migrated_database_url: str) -> None:
    _seed(migrated_database_url)
    impl = _tools(migrated_database_url)["erp.query_receivables"]

    result = impl({}, _ALL)

    for row in result["rows"]:
        assert "bank_account" not in row
        assert "tax_id" not in row
        assert "credit_limit_internal" not in row


def test_query_customer_returns_masked_contact_email(migrated_database_url: str) -> None:
    _seed(migrated_database_url)
    impl = _tools(migrated_database_url)["erp.query_customer"]

    result = impl({"customer_id": "cust-a"}, _ALL)

    assert result["source"] == "v_customer_safe"
    assert len(result["rows"]) == 1
    assert result["rows"][0]["customer_id"] == "cust-a"
    assert "@masked.local" in result["rows"][0]["contact_email"]


def test_an_unresolvable_credential_reports_an_error_and_no_row_count() -> None:
    """A missing credential must not be describable as "zero matching rows".

    The `rows` key is what makes the difference, which is why this asserts its
    absence and not merely the presence of `error`: while this returned
    `{"rows": [], "note": ...}`, a real turn read it as an empty result set and
    told the user there were no overdue receivables — with a citation — over a
    `v_ar_open` holding 231 of them.
    """
    impl = default_tool_implementations(resolve_credential=lambda ref: "")["erp.query_receivables"]

    result = impl({}, _ALL)

    assert "rows" not in result
    assert result == {"source": "v_ar_open", "error": "chưa có kết nối ERP nào được cấu hình"}


def test_an_unreachable_vault_is_reported_as_an_incident_not_as_no_connection() -> None:
    """`resolve_credential_via` no longer flattens the two — see its docstring."""

    def _unavailable(_ref: str) -> str:
        raise VaultUnavailableError("vault sealed")

    impl = default_tool_implementations(resolve_credential=_unavailable)["erp.query_receivables"]

    result = impl({}, _ALL)

    assert "rows" not in result
    assert "Vault" in result["error"]


def test_an_unreachable_database_returns_a_structured_error_instead_of_raising() -> None:
    impl = default_tool_implementations(resolve_credential=lambda ref: "postgresql://ro:pw@nonexistent-host/db")[
        "erp.query_receivables"
    ]

    result = impl({}, _ALL)

    assert "rows" not in result
    assert result["source"] == "v_ar_open"
    assert "error" in result


def test_two_own_records_principals_see_two_disjoint_row_sets(migrated_database_url: str) -> None:
    """UC1-20 (#136), FR-41/AC-2: "sales_rep và sales_manager hỏi cùng câu... nhận
    kết quả khác nhau đúng phạm vi phụ trách" — proven here with two real,
    concurrent-shaped `RowScope`s against the same seeded rows, the same
    query, the same tool implementation.
    """
    _seed(migrated_database_url)
    impl = _tools(migrated_database_url)["erp.query_receivables"]

    rep_e102 = RowScope(principal_id="E102", department="SALES", scope="own_records")
    rep_e999 = RowScope(principal_id="E999", department="SUPPORT", scope="own_records")

    result_e102 = impl({}, rep_e102)
    result_e999 = impl({}, rep_e999)

    assert {row["order_id"] for row in result_e102["rows"]} == {"order-a", "order-b"}
    assert {row["order_id"] for row in result_e999["rows"]} == {"order-c"}


def test_department_scope_sees_every_rep_in_that_department_but_no_other(migrated_database_url: str) -> None:
    _seed(migrated_database_url)
    impl = _tools(migrated_database_url)["erp.query_receivables"]

    sales_dept = RowScope(principal_id="", department="SALES", scope="department")
    support_dept = RowScope(principal_id="", department="SUPPORT", scope="department")

    result_sales = impl({}, sales_dept)
    result_support = impl({}, support_dept)

    assert {row["order_id"] for row in result_sales["rows"]} == {"order-a", "order-b"}
    assert {row["order_id"] for row in result_support["rows"]} == {"order-c"}


def test_fail_closed_scope_returns_zero_rows_instead_of_everything(migrated_database_url: str) -> None:
    """§9.2/§20: "một transaction không set GUC phải trả 0 dòng, không phải toàn
    bộ dữ liệu" — modelled here as an unrecognised `scope` value, the same
    `app_scope()` fallback an actually-missing GUC would hit (`coalesce(...,
    'none')`), since `RowScope.scope` is always set to *something* by
    `execute_tool_call` and this file's own tool implementations always issue
    all three `SET LOCAL`s — there is no code path left that skips them.
    """
    _seed(migrated_database_url)
    impl = _tools(migrated_database_url)["erp.query_receivables"]

    result = impl({}, RowScope(principal_id="E102", department="SALES", scope="none"))

    assert result["rows"] == []
