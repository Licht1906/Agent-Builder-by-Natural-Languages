#!/usr/bin/env python3
"""UC1-14 (#115) — turns spec.md §10.4's manual verification table into something CI runs.

Three checks, each straight off AC-6/AC-7 and the ticket's own acceptance
criteria (`docs/usecase_spec/uc1-ar-assistant/tickets.md`, UC1-14):

  1. `agt_ar_assistant_ro` cannot read `fin_invoice` at all (AC-6), and holds
     exactly the grants `schema.sql` gives it — nothing more. A PR that
     accidentally `GRANT`s an extra privilege fails this, not just AC-6.
  2. Two different principals querying `v_ar_open` under `own_records` scope
     get two different row sets — the row-level security schema.sql builds,
     proven with two rows this script inserts and rolls back itself.
  3. `v_audit_integrity` is empty over an intact chain and finds exactly the
     broken link after a row is tampered with (AC-7).

Everything runs inside one transaction that is always rolled back — this
script never leaves a row behind, so it is safe to run repeatedly, in CI,
against a real database, right after `alembic upgrade head`.

Usage:
    python scripts/verify_uc1_data_surface.py
Exit code 0 on every check passing, 1 otherwise (see `--help` for none — this
script takes no arguments on purpose, so it cannot be run with a flag that
skips a check).
"""

import sys
from typing import NoReturn

import psycopg
from psycopg import sql
from psycopg.errors import InsufficientPrivilege

from src.config import get_settings

_APPLICATION_ROLE = "agt_ar_assistant_ro"

#: Exactly what `schema.sql` §5 grants at the *table* level — a PR that adds a
#: GRANT this script doesn't expect fails the "no extra privileges" check by
#: construction. `orders` is deliberately absent here: its grant is
#: column-restricted (`GRANT SELECT (order_id, order_status) ON orders`),
#: which `information_schema.role_table_grants` never lists — that grant is
#: checked separately, against `column_privileges`, below.
_EXPECTED_GRANTS = {
    ("v_ar_open", "SELECT"),
    ("v_customer_safe", "SELECT"),
    ("crm_sales_assignment", "SELECT"),
}

#: `security_invoker` views need real (narrow) column grants on the tables they
#: read — `fin_invoice`'s three `never_expose` columns (`bank_account`, `tax_id`,
#: `credit_limit_internal`) and everything on `customers` beyond the three
#: `v_customer_safe` projects are deliberately absent from every value here.
_EXPECTED_COLUMN_GRANTS = {
    "orders": {"order_id", "order_status"},
    "fin_invoice": {"order_id", "customer_id", "amount_due", "amount_paid", "due_date", "settled_at"},
    "customers": {"customer_id", "customer_city", "customer_state"},
}


class VerificationError(Exception):
    """One check's expectation did not hold. Carries the sentence a CI log should show."""


def _fail(message: str) -> NoReturn:
    raise VerificationError(message)


def check_role_has_only_the_expected_grants(cur: psycopg.Cursor) -> None:
    """Table-level grants match `schema.sql` §5 exactly, then column-level ones do too."""
    _check_table_level_grants(cur)
    _check_column_level_grants(cur)


def _check_table_level_grants(cur: psycopg.Cursor) -> None:
    cur.execute(
        """
        SELECT table_name, privilege_type
        FROM information_schema.role_table_grants
        WHERE grantee = %s AND table_schema = 'public'
        """,
        (_APPLICATION_ROLE,),
    )
    actual = {(table, privilege) for table, privilege in cur.fetchall()}
    extra = actual - _EXPECTED_GRANTS
    missing = _EXPECTED_GRANTS - actual
    if extra:
        _fail(f"{_APPLICATION_ROLE} holds grant(s) schema.sql never gives it: {sorted(extra)}")
    if missing:
        _fail(f"{_APPLICATION_ROLE} is missing expected grant(s): {sorted(missing)}")


def _check_column_level_grants(cur: psycopg.Cursor) -> None:
    # `orders`, `fin_invoice`, `customers` are column-restricted — a table-level
    # grant check would not catch a PR that widened any of them to every
    # column (in `fin_invoice`'s case, to the three never_expose columns).
    for table, expected_columns in _EXPECTED_COLUMN_GRANTS.items():
        cur.execute(
            """
            SELECT column_name FROM information_schema.column_privileges
            WHERE grantee = %s AND table_name = %s AND privilege_type = 'SELECT'
            """,
            (_APPLICATION_ROLE, table),
        )
        granted_columns = {row[0] for row in cur.fetchall()}
        if granted_columns != expected_columns:
            _fail(f"{table} column grants for {_APPLICATION_ROLE} changed: {sorted(granted_columns)}")


def check_rls_is_enabled_and_forced(cur: psycopg.Cursor) -> None:
    """`crm_sales_assignment` has both `ENABLE` and `FORCE ROW LEVEL SECURITY` set."""
    cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'crm_sales_assignment'")
    row = cur.fetchone()
    if row is None:
        _fail("crm_sales_assignment does not exist")
    enabled, forced = row
    if not enabled:
        _fail("crm_sales_assignment: ROW LEVEL SECURITY is disabled")
    if not forced:
        _fail("crm_sales_assignment: FORCE ROW LEVEL SECURITY is off — table owner would bypass RLS")


def check_agent_role_cannot_read_fin_invoice(cur: psycopg.Cursor) -> None:
    """AC-6: `SET LOCAL ROLE agt_ar_assistant_ro; SELECT * FROM fin_invoice;` -> permission denied."""
    cur.execute(sql.SQL("SAVEPOINT ac6"))
    try:
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(_APPLICATION_ROLE)))
        cur.execute("SELECT * FROM fin_invoice")
    except InsufficientPrivilege:
        cur.execute(sql.SQL("ROLLBACK TO SAVEPOINT ac6"))
    else:
        cur.execute(sql.SQL("ROLLBACK TO SAVEPOINT ac6"))
        _fail(f"{_APPLICATION_ROLE} was able to SELECT fin_invoice — AC-6 violated")
    finally:
        cur.execute(sql.SQL("RESET ROLE"))


def _insert_two_reps_with_one_customer_each(cur: psycopg.Cursor) -> None:
    cur.execute(
        """
        INSERT INTO customers (customer_id, customer_unique_id, customer_city, customer_state)
        VALUES ('verify-cust-a', 'verify-cust-a-u', 'Sao Paulo', 'SP'),
               ('verify-cust-b', 'verify-cust-b-u', 'Rio de Janeiro', 'RJ')
        """
    )
    cur.execute(
        """
        INSERT INTO orders (order_id, customer_id, order_status, order_purchase_timestamp)
        VALUES ('verify-order-a', 'verify-cust-a', 'delivered', now() - interval '60 days'),
               ('verify-order-b', 'verify-cust-b', 'delivered', now() - interval '60 days')
        """
    )
    cur.execute(
        """
        INSERT INTO fin_invoice (order_id, customer_id, amount_due, amount_paid, issued_at, due_date)
        VALUES ('verify-order-a', 'verify-cust-a', 500.00, 0, current_date - 45, current_date - 35),
               ('verify-order-b', 'verify-cust-b', 700.00, 0, current_date - 45, current_date - 35)
        """
    )
    cur.execute(
        """
        INSERT INTO crm_sales_assignment (customer_id, sales_rep_id, department)
        VALUES ('verify-cust-a', 'verify-rep-a', 'SALES'),
               ('verify-cust-b', 'verify-rep-b', 'SALES')
        """
    )


def _customer_ids_visible_to(cur: psycopg.Cursor, principal_id: str) -> set[str]:
    # Postgres's `SET`/`SET LOCAL` do not accept bind parameters over the
    # extended protocol — `sql.Literal` does the equivalent escaping instead.
    cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(_APPLICATION_ROLE)))
    cur.execute(sql.SQL("SET LOCAL app.principal_id = {}").format(sql.Literal(principal_id)))
    cur.execute("SET LOCAL app.department = 'SALES'")
    cur.execute("SET LOCAL app.scope = 'own_records'")
    cur.execute(
        "SELECT customer_id FROM v_ar_open WHERE customer_id IN ('verify-cust-a', 'verify-cust-b')"
    )
    visible = {row[0] for row in cur.fetchall()}
    cur.execute(sql.SQL("RESET ROLE"))
    return visible


def check_two_principals_see_two_different_row_sets(cur: psycopg.Cursor) -> None:
    """Two `own_records`-scoped principals querying `v_ar_open` see only their own customer."""
    cur.execute(sql.SQL("SAVEPOINT scope_check"))
    try:
        _insert_two_reps_with_one_customer_each(cur)

        seen_by_a = _customer_ids_visible_to(cur, "verify-rep-a")
        seen_by_b = _customer_ids_visible_to(cur, "verify-rep-b")

        if seen_by_a != {"verify-cust-a"}:
            _fail(f"verify-rep-a should see only their own customer, saw {sorted(seen_by_a)}")
        if seen_by_b != {"verify-cust-b"}:
            _fail(f"verify-rep-b should see only their own customer, saw {sorted(seen_by_b)}")
        if seen_by_a == seen_by_b:
            _fail("two different principals under own_records scope saw the same row set")
    finally:
        cur.execute(sql.SQL("ROLLBACK TO SAVEPOINT scope_check"))


def _insert_two_audit_rows(cur: psycopg.Cursor) -> tuple[int, int]:
    cur.execute(
        """
        INSERT INTO audit_log (agent_id, principal_id, tool, effect, reasons, action_hash, payload)
        VALUES
            ('agt_ar_assistant', 'verify-rep-a', 'erp.query_receivables', 'allow', '[]', 'verify-hash-1', '{}'),
            ('agt_ar_assistant', 'verify-rep-a', 'erp.query_receivables', 'allow', '[]', 'verify-hash-2', '{}')
        RETURNING seq
        """
    )
    first_seq, second_seq = (row[0] for row in cur.fetchall())
    return first_seq, second_seq


def _tamper_with(cur: psycopg.Cursor, seq: int) -> None:
    """Bypass the anti-tamper rule on purpose, the way a real attacker with
    table ownership would have to — this is exactly what §14 says the hash
    chain cannot prevent, only make evident.
    """
    cur.execute("ALTER TABLE audit_log DISABLE RULE audit_no_update")
    cur.execute("UPDATE audit_log SET entry_hash = repeat('f', 64) WHERE seq = %s", (seq,))
    cur.execute("ALTER TABLE audit_log ENABLE RULE audit_no_update")


def check_audit_integrity_view(cur: psycopg.Cursor) -> None:
    """AC-7: `v_audit_integrity` is empty over an intact chain, and finds the break after a tamper."""
    cur.execute(sql.SQL("SAVEPOINT audit_check"))
    try:
        first_seq, second_seq = _insert_two_audit_rows(cur)

        cur.execute("SELECT seq FROM v_audit_integrity WHERE seq IN (%s, %s)", (first_seq, second_seq))
        if cur.fetchall():
            _fail("v_audit_integrity flagged a row before any tampering — chain was already broken")

        _tamper_with(cur, first_seq)

        cur.execute("SELECT seq FROM v_audit_integrity WHERE seq = %s", (second_seq,))
        broken = cur.fetchone()
        if broken is None:
            _fail("v_audit_integrity did not detect the tampered row's downstream break")
    finally:
        cur.execute(sql.SQL("ROLLBACK TO SAVEPOINT audit_check"))


CHECKS = [
    ("agt_ar_assistant_ro has only its expected grants", check_role_has_only_the_expected_grants),
    ("crm_sales_assignment has RLS enabled and forced", check_rls_is_enabled_and_forced),
    ("AC-6: agt_ar_assistant_ro cannot SELECT fin_invoice", check_agent_role_cannot_read_fin_invoice),
    ("two principals under own_records see different rows", check_two_principals_see_two_different_row_sets),
    ("AC-7: v_audit_integrity detects a tampered row", check_audit_integrity_view),
]


def main() -> int:
    dsn = get_settings().database_url
    failures: list[str] = []

    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        for label, check in CHECKS:
            try:
                check(cur)
            except VerificationError as exc:
                failures.append(f"{label}: {exc}")
                print(f"FAIL  {label}\n      {exc}")
            else:
                print(f"OK    {label}")
        # Nothing this script does should survive it — the fixture rows, the
        # tampered hash, all of it, whether every check passed or not.
        conn.rollback()

    if failures:
        print(f"\n{len(failures)} check(s) failed.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
