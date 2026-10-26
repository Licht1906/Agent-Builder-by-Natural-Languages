"""The engines a dialect suite runs against — DS-06/DS-07's "bộ test tích hợp của DS-04 chạy xanh".

A `Backend` is one real database a test can build views in and point a
dialect at: the suite's own Postgres, always; a SQL Server when
`TEST_MSSQL_URL` names one, and a MySQL when `TEST_MYSQL_URL` does (CI starts
a container for each). A suite that takes the `backend` fixture runs once per
engine, the same assertions every time.

Each URL's database must end in `_test`, the rule `tests.db_target` keeps for
Postgres: this module creates and drops views in it, and nothing should look
disposable that is not.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pymssql
import pymysql
from sqlalchemy import text
from sqlalchemy.engine import URL

from src.datasources.dialects import Dialect, dialect_for
from src.datasources.dialects import mysql as mysql_dialect
from src.datasources.dialects import sqlserver as sqlserver_dialect
from src.db.base import engine
from src.db.models import DatabaseDialect
from tests.db_target import TEST_DB_SUFFIX

#: Where SQL Server is, as the DSN a creator would paste — `mssql://user:password@host:port/<name>_test`.
MSSQL_URL_VARIABLE = "TEST_MSSQL_URL"
#: Where MySQL is — `mysql://user:password@host:port/<name>_test`.
MYSQL_URL_VARIABLE = "TEST_MYSQL_URL"

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

    @property
    def carries_session_attributes(self) -> bool:
        """Whether the asker's attributes reach the database session — every engine but MySQL (ADR-0059 §8)."""
        return self.kind is not DatabaseDialect.MYSQL

    def create_values_view(self, name: str, columns: Sequence[str], rows: Sequence[Row]) -> None:
        """`name` as a view over literal rows, each literal in that engine's spelling."""
        if self.kind is DatabaseDialect.MYSQL:
            # `UNION ALL` of `SELECT`s: MySQL's `VALUES ROW(...)` names its columns `column_0`, ….
            first, *rest = rows
            head = ", ".join(
                f"{self._literal(value)} AS {column}" for value, column in zip(first, columns, strict=True)
            )
            tail = "".join(" UNION ALL SELECT " + ", ".join(self._literal(value) for value in row) for row in rest)
            self.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT {head}{tail}")
            return
        values = ", ".join("(" + ", ".join(self._literal(value) for value in row) + ")" for row in rows)
        verb = "CREATE OR ALTER VIEW" if self.kind is DatabaseDialect.SQLSERVER else "CREATE OR REPLACE VIEW"
        self.execute(f"{verb} {name} AS SELECT * FROM (VALUES {values}) AS t({', '.join(columns)})")

    def create_session_view(self, name: str, attribute: str) -> None:
        """A one-column view `who` showing the asker attribute `attribute` as the session carries it."""
        if self.kind is DatabaseDialect.SQLSERVER:
            body = f"SELECT CAST(SESSION_CONTEXT(N'{attribute}') AS nvarchar(200)) AS who"
            self.execute(f"CREATE OR ALTER VIEW {name} AS {body}")
        elif self.kind is DatabaseDialect.MYSQL:
            # Nothing to read the attribute from — the view says so by always being empty-handed.
            self.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT CAST(NULL AS CHAR) AS who")
        else:
            self.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT current_setting('app.{attribute}', true) AS who")

    def drop_view(self, name: str) -> None:
        self.execute(f"DROP VIEW IF EXISTS {name}")

    def execute(self, statement: str) -> None:
        """One statement as the test's own account — the arranging half, never the dialect under test."""
        self.scalar(statement, fetch=False)

    def scalar(self, statement: str, *, fetch: bool = True) -> Any:
        """The first column of the first row `statement` returns, read as the test's own account."""
        if self.kind is DatabaseDialect.POSTGRESQL:
            with engine.begin() as connection:
                result = connection.execute(text(statement))
                return result.scalar() if fetch else None
        connection = _admin(self.kind, self.dsn)
        try:
            cursor = connection.cursor()
            cursor.execute(statement)
            if not fetch:
                return None
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            connection.close()

    def _literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, Decimal):
            return f"CAST({value} AS decimal(10, 2))"
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
    return _external(DatabaseDialect.SQLSERVER, MSSQL_URL_VARIABLE)


def mysql_backend() -> Backend | None:
    """The MySQL `TEST_MYSQL_URL` names, its database created if missing — `None` when it names none."""
    return _external(DatabaseDialect.MYSQL, MYSQL_URL_VARIABLE)


def _external(kind: DatabaseDialect, variable: str) -> Backend | None:
    dsn = os.environ.get(variable)
    if not dsn:
        return None
    target = _target(kind, dsn)
    if not target.database.endswith(TEST_DB_SUFFIX):
        raise RuntimeError(
            f"{variable} must name a database ending in {TEST_DB_SUFFIX!r} — the suite builds views in it"
        )
    connection = _admin(kind, dsn, database="")
    try:
        if kind is DatabaseDialect.SQLSERVER:
            # A bound name, then `QUOTENAME`: `CREATE DATABASE` takes no parameter.
            connection.cursor().execute(
                "IF DB_ID(%s) IS NULL BEGIN DECLARE @sql nvarchar(400) = N'CREATE DATABASE ' + QUOTENAME(%s); "
                "EXEC (@sql) END",
                (target.database, target.database),
            )
        else:
            name = target.database.replace("`", "``")
            connection.cursor().execute(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4")
    finally:
        connection.close()
    return Backend(kind=kind, dsn=dsn, trusted=frozenset({target.host}))


def _target(kind: DatabaseDialect, dsn: str) -> Any:
    return sqlserver_dialect.parse_dsn(dsn) if kind is DatabaseDialect.SQLSERVER else mysql_dialect.parse_dsn(dsn)


def _admin(kind: DatabaseDialect, dsn: str, *, database: str | None = None) -> Any:
    """A plain connection as the URL's account — `database=""` for the server itself, to create the test one."""
    target = _target(kind, dsn)
    name = target.database if database is None else database
    if kind is DatabaseDialect.SQLSERVER:
        return pymssql.connect(
            server=target.host,
            port=str(target.port or 1433),
            user=target.user,
            password=target.password,
            database=name or "master",
            login_timeout=10,
            autocommit=True,
        )
    return pymysql.connect(
        host=target.host,
        port=target.port,
        user=target.user,
        password=target.password,
        database=name or None,
        charset="utf8mb4",
        autocommit=True,
    )


__all__ = [
    "MSSQL_URL_VARIABLE",
    "MYSQL_URL_VARIABLE",
    "Backend",
    "mysql_backend",
    "postgres_backend",
    "sqlserver_backend",
]
