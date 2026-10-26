"""The engines a dialect suite runs against — DS-06's "bộ test tích hợp của DS-04 chạy xanh trên SQL Server".

A `Backend` is one real database a test can build views in and point a
dialect at: the suite's own Postgres, always, and a SQL Server when
`TEST_MSSQL_URL` names one (CI starts a container for it). A suite that takes
the `backend` fixture runs once per engine, the same assertions both times.

The SQL Server database is the URL's own and must end in `_test`, the rule
`tests.db_target` keeps for Postgres: this module creates and drops views in
it, and nothing should look disposable that is not.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pymssql
from sqlalchemy import text
from sqlalchemy.engine import URL

from src.datasources.dialects import Dialect, dialect_for
from src.datasources.dialects.sqlserver import parse_dsn
from src.db.base import engine
from src.db.models import DatabaseDialect
from tests.db_target import TEST_DB_SUFFIX

#: Where SQL Server is, as the DSN a creator would paste — `mssql://user:password@host:port/<name>_test`.
MSSQL_URL_VARIABLE = "TEST_MSSQL_URL"

Row = Sequence[Any]


@dataclass(frozen=True)
class Backend:
    """One engine: the dialect for it, a DSN it accepts, and a way to build test views in it."""

    kind: DatabaseDialect
    dsn: str
    trusted: frozenset[str]

    @property
    def dialect(self) -> Dialect:
        return dialect_for(self.kind)

    def create_values_view(self, name: str, columns: Sequence[str], rows: Sequence[Row]) -> None:
        """`name` as a view over literal rows — `VALUES` in both engines, each literal in that engine's spelling."""
        values = ", ".join("(" + ", ".join(self._literal(value) for value in row) + ")" for row in rows)
        verb = "CREATE OR ALTER VIEW" if self.kind is DatabaseDialect.SQLSERVER else "CREATE OR REPLACE VIEW"
        self.execute(f"{verb} {name} AS SELECT * FROM (VALUES {values}) AS t({', '.join(columns)})")

    def create_session_view(self, name: str, attribute: str) -> None:
        """A one-column view `who` showing the asker attribute `attribute` as the session carries it."""
        if self.kind is DatabaseDialect.SQLSERVER:
            body = f"SELECT CAST(SESSION_CONTEXT(N'{attribute}') AS nvarchar(200)) AS who"
            self.execute(f"CREATE OR ALTER VIEW {name} AS {body}")
        else:
            self.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT current_setting('app.{attribute}', true) AS who")

    def drop_view(self, name: str) -> None:
        self.execute(f"DROP VIEW IF EXISTS {name}")

    def execute(self, statement: str) -> None:
        """One statement as the test's own account — the arranging half, never the dialect under test."""
        if self.kind is DatabaseDialect.SQLSERVER:
            connection = _mssql_admin(self.dsn)
            try:
                connection.cursor().execute(statement)
            finally:
                connection.close()
        else:
            with engine.begin() as connection:
                connection.execute(text(statement))

    def scalar(self, statement: str) -> Any:
        """The first column of the first row `statement` returns, read as the test's own account."""
        if self.kind is DatabaseDialect.SQLSERVER:
            connection = _mssql_admin(self.dsn)
            try:
                cursor = connection.cursor()
                cursor.execute(statement)
                row = cursor.fetchone()
                return row[0] if row else None
            finally:
                connection.close()
        with engine.connect() as connection:
            return connection.execute(text(statement)).scalar()

    def _literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, Decimal):
            return f"CAST({value} AS numeric(10, 2))"
        if isinstance(value, int):
            return str(value)
        quoted = "'" + str(value).replace("'", "''") + "'"
        # `N'…'` in SQL Server, or "hứa" arrives as "h?a"; Postgres reads `N'…'` as `bpchar`, so plain there.
        return f"N{quoted}" if self.kind is DatabaseDialect.SQLSERVER else quoted


def postgres_backend() -> Backend:
    url: URL = engine.url.set(drivername="postgresql")
    return Backend(
        kind=DatabaseDialect.POSTGRESQL,
        dsn=url.render_as_string(hide_password=False),
        trusted=frozenset({str(engine.url.host)}),
    )


def sqlserver_backend() -> Backend | None:
    """The SQL Server `TEST_MSSQL_URL` names, its database created if missing — `None` when it names none."""
    dsn = os.environ.get(MSSQL_URL_VARIABLE)
    if not dsn:
        return None
    target = parse_dsn(dsn)
    if not target.database.endswith(TEST_DB_SUFFIX):
        raise RuntimeError(
            f"{MSSQL_URL_VARIABLE} must name a database ending in {TEST_DB_SUFFIX!r} — the suite builds views in it"
        )
    connection = _mssql_admin(dsn, database="master")
    try:
        # A bound name, then `QUOTENAME`: `CREATE DATABASE` takes no parameter.
        connection.cursor().execute(
            "IF DB_ID(%s) IS NULL BEGIN DECLARE @sql nvarchar(400) = N'CREATE DATABASE ' + QUOTENAME(%s); "
            "EXEC (@sql) END",
            (target.database, target.database),
        )
    finally:
        connection.close()
    return Backend(kind=DatabaseDialect.SQLSERVER, dsn=dsn, trusted=frozenset({target.host}))


def _mssql_admin(dsn: str, *, database: str | None = None) -> Any:
    target = parse_dsn(dsn)
    return pymssql.connect(
        server=target.host,
        port=str(target.port or 1433),
        user=target.user,
        password=target.password,
        database=target.database if database is None else database,
        login_timeout=10,
        autocommit=True,
    )


__all__ = ["MSSQL_URL_VARIABLE", "Backend", "postgres_backend", "sqlserver_backend"]
