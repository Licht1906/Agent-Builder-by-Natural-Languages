"""UC1-15 (#116) — the seed script, run for real against a freshly migrated database.

`tests/test_db/test_migrations.py` already proves the schema applies; this
proves the *data* on top of it produces the exact counts spec.md §15's demo
(and tickets.md's own AC) depends on — 23 overdue-30-day rows for the
manager persona, 7 for the rep persona, read through `v_ar_open` under
`agt_ar_assistant_ro` the same way the deployed agent would.
"""

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine, text

from scripts.seed_uc1_demo_data import (
    MANAGER_EMPLOYEE_ID,
    MANAGER_TARGET_OVERDUE_ROWS,
    REP_EMPLOYEE_ID,
    REP_TARGET_OVERDUE_ROWS,
    seed,
)
from tests.test_db.conftest import upgrade_to_head


def _overdue_count_via_rls(dsn: str, principal_id: str) -> int:
    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE agt_ar_assistant_ro")
        cur.execute("SET LOCAL app.department = 'SALES'")
        cur.execute("SET LOCAL app.scope = 'own_records'")
        # `SET`/`SET LOCAL` do not accept bind parameters over the extended
        # protocol — `sql.Literal` does the equivalent escaping instead.
        cur.execute(sql.SQL("SET LOCAL app.principal_id = {}").format(sql.Literal(principal_id)))
        cur.execute("SELECT count(*) FROM v_ar_open WHERE days_overdue > 30")
        row = cur.fetchone()
        assert row is not None
        conn.rollback()
        return row[0]


@pytest.fixture
def migrated_database_url(empty_database_url: str) -> str:
    upgrade_to_head(empty_database_url)
    return empty_database_url


def test_seed_produces_the_exact_demo_row_counts(migrated_database_url: str) -> None:
    with psycopg.connect(migrated_database_url, autocommit=False) as conn, conn.cursor() as cur:
        seed(cur, reset=False)
        conn.commit()

    manager_rows = _overdue_count_via_rls(migrated_database_url, MANAGER_EMPLOYEE_ID)
    rep_rows = _overdue_count_via_rls(migrated_database_url, REP_EMPLOYEE_ID)

    assert manager_rows == MANAGER_TARGET_OVERDUE_ROWS
    assert rep_rows == REP_TARGET_OVERDUE_ROWS


def test_seeding_twice_without_reset_refuses(migrated_database_url: str) -> None:
    with psycopg.connect(migrated_database_url, autocommit=False) as conn, conn.cursor() as cur:
        seed(cur, reset=False)
        conn.commit()

    with psycopg.connect(migrated_database_url, autocommit=False) as conn, conn.cursor() as cur:
        with pytest.raises(RuntimeError, match="already present"):
            seed(cur, reset=False)
        conn.rollback()


def test_reset_reseeds_to_the_same_counts(migrated_database_url: str) -> None:
    with psycopg.connect(migrated_database_url, autocommit=False) as conn, conn.cursor() as cur:
        seed(cur, reset=False)
        conn.commit()
    with psycopg.connect(migrated_database_url, autocommit=False) as conn, conn.cursor() as cur:
        seed(cur, reset=True)
        conn.commit()

    assert _overdue_count_via_rls(migrated_database_url, MANAGER_EMPLOYEE_ID) == MANAGER_TARGET_OVERDUE_ROWS
    assert _overdue_count_via_rls(migrated_database_url, REP_EMPLOYEE_ID) == REP_TARGET_OVERDUE_ROWS


def test_sensitive_fin_invoice_columns_never_appear_in_the_views(migrated_database_url: str) -> None:
    with psycopg.connect(migrated_database_url, autocommit=False) as conn, conn.cursor() as cur:
        seed(cur, reset=False)
        conn.commit()

    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        ar_open_columns = {
            row[0]
            for row in connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'v_ar_open'")
            )
        }
        customer_safe_columns = {
            row[0]
            for row in connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'v_customer_safe'")
            )
        }
    engine.dispose()

    never_expose = {"bank_account", "tax_id", "credit_limit_internal"}
    assert not (ar_open_columns & never_expose)
    assert not (customer_safe_columns & never_expose)
