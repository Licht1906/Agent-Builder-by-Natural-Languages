"""The PostgreSQL dialect, against a real Postgres — DS-02 / ADR-0059 §2, §8.

The test database is that real Postgres: `conftest` applies UC-1's
`schema.sql` to it, so it holds two views (`v_ar_open`, `v_customer_safe`)
and the base tables they read (`fin_invoice`, …) — exactly the shape a
creator's staging ERP has. Nothing here is scripted; a probe that passes here
passed against Postgres.
"""

import time
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL

from src.datasources.dialects import DatabaseProbeError, ProbeFailure, schema_problems
from src.datasources.dialects.postgresql import PostgresDialect
from src.db.base import engine

DIALECT = PostgresDialect()


def _dsn(**overrides: object) -> str:
    """The test database's own DSN, with any part replaced."""
    url: URL = engine.url.set(drivername="postgresql", **overrides)  # type: ignore[arg-type]
    return url.render_as_string(hide_password=False)


#: The test database's host is local by definition, so it is trusted the way
#: an operator trusts a staging host through `database_source_trusted_hosts`.
TRUSTED = frozenset({str(engine.url.host)})


def test_introspection_lists_the_views_with_their_columns_and_row_counts() -> None:
    found = DIALECT.introspect(_dsn(), trusted_hosts=TRUSTED)

    views = {view.name: view for view in found.views}
    assert {"v_ar_open", "v_customer_safe"} <= set(views)
    assert "days_overdue" in views["v_ar_open"].columns
    assert isinstance(views["v_ar_open"].row_count, int)


def test_introspection_names_base_tables_apart_from_views() -> None:
    found = DIALECT.introspect(_dsn(), trusted_hosts=TRUSTED)

    assert "fin_invoice" in found.base_tables
    assert "fin_invoice" not in {view.name for view in found.views}


def test_system_catalogs_are_not_offered_as_views() -> None:
    found = DIALECT.introspect(_dsn(), trusted_hosts=TRUSTED)

    assert not [view.name for view in found.views if view.name.startswith(("pg_", "information_schema"))]


def test_a_wrong_password_is_named_as_such_and_the_password_is_never_echoed() -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(_dsn(password="sai-mat-khau-9137"), trusted_hosts=TRUSTED)

    assert caught.value.reason is ProbeFailure.AUTH_REJECTED
    assert "sai-mat-khau-9137" not in str(caught.value)


def test_a_database_that_does_not_exist_is_named_as_such() -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(_dsn(database="khong_co_db_nay"), trusted_hosts=TRUSTED)

    assert caught.value.reason is ProbeFailure.DATABASE_NOT_FOUND


def test_nothing_listening_fails_within_five_seconds() -> None:
    started = time.monotonic()
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(_dsn(port=1), trusted_hosts=TRUSTED)

    assert caught.value.reason is ProbeFailure.UNREACHABLE
    assert time.monotonic() - started <= 5.5


def test_a_malformed_dsn_is_refused_without_repeating_it() -> None:
    dsn = "host=db password=bi-mat-4471 =oops"
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(dsn, trusted_hosts=TRUSTED)

    assert caught.value.reason is ProbeFailure.INVALID_DSN
    assert "bi-mat-4471" not in str(caught.value)


def test_an_internal_host_nobody_trusted_is_never_dialled() -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(_dsn(), trusted_hosts=frozenset())

    assert caught.value.reason is ProbeFailure.BLOCKED_ADDRESS


def test_a_unix_socket_is_an_internal_address_too() -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect("host=/var/run/postgresql dbname=erp", trusted_hosts=frozenset())

    assert caught.value.reason is ProbeFailure.BLOCKED_ADDRESS


def test_a_hostaddr_cannot_smuggle_an_internal_address_past_a_trusted_host() -> None:
    dsn = _dsn(host="db.example.com") + "?hostaddr=169.254.169.254"
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(dsn, trusted_hosts=frozenset({"db.example.com"}))

    assert caught.value.reason is ProbeFailure.BLOCKED_ADDRESS


@pytest.mark.parametrize("keyword", ["service=erp", "passfile=/etc/passwd", "sslkey=/root/.ssh/id_rsa"])
def test_a_dsn_keyword_that_reads_files_on_this_server_is_refused(keyword: str) -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(f"host=db.example.com dbname=erp {keyword}", trusted_hosts=frozenset({"db.example.com"}))

    assert caught.value.reason is ProbeFailure.DSN_OPTION_REFUSED


def test_a_dsn_without_a_password_never_borrows_this_servers_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """libpq falls back to `PGPASSWORD`/`~/.pgpass` — the platform's own credentials, sent to a host a creator chose."""
    monkeypatch.setenv("PGPASSWORD", str(engine.url.password))
    url = engine.url
    no_password = f"host={url.host} port={url.port} dbname={url.database} user={url.username}"

    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(no_password, trusted_hosts=TRUSTED)

    assert caught.value.reason is ProbeFailure.AUTH_REJECTED


@pytest.fixture
def slow_view() -> Iterator[str]:
    """A view whose `count(*)` takes longer than a probe will wait for one view."""
    with engine.begin() as connection:
        connection.execute(text("CREATE OR REPLACE VIEW v_slow_probe AS SELECT pg_sleep(3)::text AS x"))
    yield "v_slow_probe"
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW IF EXISTS v_slow_probe"))


def test_a_view_too_slow_to_count_is_listed_without_a_count(slow_view: str) -> None:
    found = DIALECT.introspect(_dsn(), trusted_hosts=TRUSTED)

    views = {view.name: view for view in found.views}
    assert views[slow_view].row_count is None
    assert isinstance(views["v_ar_open"].row_count, int)


def test_every_session_is_read_only() -> None:
    with (
        DIALECT.connect(_dsn(), trusted_hosts=TRUSTED) as connection,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        connection.execute("CREATE TABLE should_never_exist (id int)")


def test_schema_problems_names_every_missing_view_and_column() -> None:
    found = DIALECT.introspect(_dsn(), trusted_hosts=TRUSTED)

    problems = schema_problems(
        found,
        {"v_ar_open": ["days_overdue", "khong_co_cot"], "v_khong_ton_tai": ["id"], "fin_invoice": []},
    )

    assert problems == [
        "view v_ar_open thiếu cột khong_co_cot",
        "không có view v_khong_ton_tai",
        "fin_invoice là bảng gốc, không phải view",
    ]
