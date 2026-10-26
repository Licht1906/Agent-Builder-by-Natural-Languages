"""PostgreSQL — the first dialect (DS-02), absorbing UC-1's `postgres_introspection` and `uc1_connection_probe`.

**Every connection is read-only, short and checked before it is dialled.**
`default_transaction_read_only` and a statement timeout ride in the
connection's own `options` — set by us, never taken from the DSN — so no
statement this process sends can write or hang: a property of the session,
not of the SQL we happen to send. The host
goes through `src.services.ssrf_guard` unless an operator listed it in
`database_source_trusted_hosts` (a staging ERP usually lives on a private
network, which is exactly what the guard refuses), and the address that was
checked is pinned as `hostaddr` so DNS is not asked twice.

**Failures are read off libpq's message.** psycopg reports no `sqlstate` for
a connection that never opened, so the reason is matched on the text — and
only ever reported as a `ProbeFailure`, never as that text.
"""

import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from sqlalchemy.dialects import postgresql

from src.datasources.dialects.base import (
    ColumnRef,
    DatabaseProbeError,
    IdentityLookup,
    IdentitySearch,
    Introspection,
    ProbeFailure,
    QueryRequest,
    QueryRows,
    ViewInfo,
)
from src.datasources.dialects.sql import select_for
from src.datasources.query import PLAIN_NAME
from src.services.ssrf_guard import SSRFBlockedError, pin_safe_host

#: A person is watching a spinner — UC-1's own "timeout ngắn", and DS-02's
#: "lỗi dễ hiểu trong ≤ 5 s". One attempt only: the address is always pinned,
#: so libpq never walks a list of them, each with its own timeout.
_CONNECT_TIMEOUT_SECONDS = 5
_STATEMENT_TIMEOUT_MS = 5000

#: Counting rows is the one part of a probe whose cost grows with the
#: database, so it gets a ceiling per view and a ceiling for all of them; a
#: view past either is listed with no count rather than failing the probe.
_COUNT_TIMEOUT_MS = 2000
_COUNT_BUDGET_SECONDS = 10

#: How many distinct values the spec form is offered — positions in a
#: company, not a column scan.
_DISTINCT_LIMIT = 200

#: What a structured query is compiled with. The `psycopg2` flavour on
#: purpose, though psycopg 3 executes it: both write `%(name)s` placeholders,
#: but SQLAlchemy's `psycopg` flavour also renders a cast on every bind
#: (`%(a_1)s::VARCHAR`), which turns `"45"` against an integer column into a
#: type error. Uncast, psycopg 3 sends a `str` as `unknown`, and the value
#: takes the column's type the way a literal does — a model writes `"45"` for
#: a number as often as `45`.
_SQL_DIALECT = postgresql.psycopg2.dialect()


#: The connection-string keywords a creator may set. Everything else libpq
#: knows reaches past the DSN into this server — `service`/`passfile` read
#: files here, `sslcert`/`sslkey` hand our files to whoever answers — so it is
#: refused, not silently dropped.
_ALLOWED_KEYWORDS = frozenset(
    {"host", "hostaddr", "port", "dbname", "user", "password", "sslmode", "application_name", "connect_timeout"}
)

_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")

_VIEW_COLUMNS = """
SELECT c.table_schema, c.table_name, c.column_name
FROM information_schema.columns c
JOIN information_schema.views v ON v.table_schema = c.table_schema AND v.table_name = c.table_name
WHERE c.table_schema <> ALL(%s)
ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

_BASE_TABLES = """
SELECT table_schema, table_name FROM information_schema.tables
WHERE table_type = 'BASE TABLE' AND table_schema <> ALL(%s)
ORDER BY table_schema, table_name
"""


class PostgresDialect:
    """`Dialect` for PostgreSQL, over psycopg 3 — one connection per call, never pooled."""

    def introspect(self, dsn: str, *, trusted_hosts: frozenset[str]) -> Introspection:
        """Every view with its columns and row count, plus the base tables — one connection, read-only."""
        with self._reading(dsn, trusted_hosts) as connection:
            columns = _view_columns(connection)
            tables = tuple(
                _display_name(schema, name)
                for schema, name in connection.execute(_BASE_TABLES, (list(_SYSTEM_SCHEMAS),))
            )
            views = _counted(connection, columns)
        return Introspection(views=views, base_tables=tables)

    def identity_lookup(self, dsn: str, *, trusted_hosts: frozenset[str]) -> IdentityLookup:
        """The asker's row by one equality, as a parameter — `None` for nobody and for an ambiguous match.

        Two rows for one email is not an asker this lookup may choose between:
        picking the first would hand whoever sorts first the other one's
        scope. The column is compared as text, so an `int` employee code
        matches the string an identity provider hands over.
        """

        def lookup(match: ColumnRef, value: Any, columns: tuple[str, ...]) -> dict[str, Any] | None:
            statement = sql.SQL("SELECT {columns} FROM {view} WHERE {match}::text = %s LIMIT 2").format(
                columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
                view=_qualified(match.view),
                match=sql.Identifier(match.column),
            )
            with self._reading(dsn, trusted_hosts) as connection:
                rows = connection.execute(statement, (str(value),)).fetchall()
            return dict(zip(columns, rows[0], strict=True)) if len(rows) == 1 else None

        return lookup

    def distinct_values(self, dsn: str, column: ColumnRef, *, trusted_hosts: frozenset[str]) -> list[Any]:
        """`SELECT DISTINCT` of one column, non-null, sorted, at most `_DISTINCT_LIMIT` values."""
        statement = sql.SQL(
            "SELECT DISTINCT {column} FROM {view} WHERE {column} IS NOT NULL ORDER BY 1 LIMIT {limit}"
        ).format(column=sql.Identifier(column.column), view=_qualified(column.view), limit=sql.Literal(_DISTINCT_LIMIT))
        with self._reading(dsn, trusted_hosts) as connection:
            return [row[0] for row in connection.execute(statement)]

    def identity_candidates(
        self, dsn: str, search: IdentitySearch, *, trusted_hosts: frozenset[str]
    ) -> list[tuple[Any, Any]]:
        """Distinct `(match, label)` pairs whose text contains `search.text` — bound as a parameter, wildcards escaped."""
        escaped = search.text.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        pattern = f"%{escaped}%"
        statement = sql.SQL(
            "SELECT DISTINCT {match}::text, {label}::text FROM {view} "
            "WHERE {match}::text ILIKE %s OR {label}::text ILIKE %s ORDER BY 1 LIMIT {limit}"
        ).format(
            match=sql.Identifier(search.match.column),
            label=sql.Identifier(search.label_column),
            view=_qualified(search.match.view),
            limit=sql.Literal(search.limit),
        )
        with self._reading(dsn, trusted_hosts) as connection:
            return [(row[0], row[1]) for row in connection.execute(statement, (pattern, pattern))]

    def run_query(self, dsn: str, request: QueryRequest, *, trusted_hosts: frozenset[str]) -> QueryRows:
        """`request` as one read-only transaction: the asker's attributes set locally, then the statement.

        Read-only and time-boxed *in the transaction itself*, not only by the
        session options `_dial_conninfo` sends — a connection pooler in front of
        a customer's database may drop startup options, and the guarantee must
        not ride on that.

        `set_config(name, value, true)` is `SET LOCAL` with both halves bound
        as parameters. Attribute names are lower-cased — Postgres setting names
        are case-insensitive — and one that is still not a plain name is left
        off the session rather than quoted into it. Local, so nothing outlives
        the transaction even on a pooled server.
        """
        compiled = select_for(request).compile(dialect=_SQL_DIALECT, compile_kwargs={"render_postcompile": True})
        with (
            self._reading(dsn, trusted_hosts, failure=ProbeFailure.QUERY_FAILED) as connection,
            connection.transaction(),
        ):
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(_STATEMENT_TIMEOUT_MS)))
            for name, value in request.session_attributes.items():
                if PLAIN_NAME.match(name.lower()) and value is not None:
                    connection.execute("SELECT set_config(%s, %s, true)", (f"app.{name.lower()}", str(value)))
            cursor = connection.execute(str(compiled), compiled.params)
            columns = tuple(description.name for description in cursor.description or ())
            return QueryRows(columns=columns, rows=tuple(tuple(row) for row in cursor.fetchall()))

    @contextmanager
    def _reading(
        self, dsn: str, trusted_hosts: frozenset[str], *, failure: ProbeFailure = ProbeFailure.SERVER_ERROR
    ) -> Iterator[psycopg.Connection[Any]]:
        """`connect`, with every driver error inside the block reported as a `DatabaseProbeError` — never its text.

        `failure` is what an error that is not about connecting is called —
        "reading the view list failed" for a probe, "this query failed" for a
        query — so the sentence a creator reads is about what they just did.
        """
        try:
            with self.connect(dsn, trusted_hosts=trusted_hosts) as connection:
                yield connection
        except psycopg.Error as exc:
            reason = _failure_of(exc)
            raise DatabaseProbeError(failure if reason is ProbeFailure.SERVER_ERROR else reason) from None

    @contextmanager
    def connect(self, dsn: str, *, trusted_hosts: frozenset[str]) -> Iterator[psycopg.Connection[Any]]:
        """A short-lived, autocommit, read-only connection, dialled through `_dial_conninfo`'s checks.

        Driver errors raised *inside* the block come out as themselves; the
        dialect's own methods read through `_reading`, which reports them by
        reason. Not on the `Dialect` interface until `data_query` (DS-04) runs
        a query through it.
        """
        conninfo = _dial_conninfo(dsn, trusted_hosts)
        try:
            connection = psycopg.connect(conninfo, autocommit=True)
        except psycopg.Error as exc:
            raise DatabaseProbeError(_failure_of(exc)) from None
        with connection:
            yield connection


def _dial_conninfo(dsn: str, trusted_hosts: frozenset[str]) -> str:
    """`dsn` made safe to dial: address checked and pinned, read-only, short timeouts. Raises `DatabaseProbeError`.

    `hostaddr`, when the DSN sets one, is what libpq actually dials — so it is
    the thing checked, or `host=trusted hostaddr=169.254.169.254` would walk
    straight past the guard. The address is pinned on the trusted path too,
    so nothing outside the DSN (`PGHOSTADDR`) supplies one.

    A DSN with no password gets an empty one and no password file: libpq
    would otherwise reach for `PGPASSWORD` or `~/.pgpass` — this platform's
    own credentials — and offer them to whatever host the creator named.
    """
    try:
        params: dict[str, Any] = conninfo_to_dict(dsn)
    except psycopg.Error:
        raise DatabaseProbeError(ProbeFailure.INVALID_DSN) from None
    if not set(params) <= _ALLOWED_KEYWORDS:
        raise DatabaseProbeError(ProbeFailure.DSN_OPTION_REFUSED)
    params["hostaddr"] = _pinned_address(str(params.get("hostaddr") or params.get("host") or ""), trusted_hosts)
    params.setdefault("password", "")
    params["passfile"] = os.devnull
    params["options"] = f"-c default_transaction_read_only=on -c statement_timeout={_STATEMENT_TIMEOUT_MS}"
    params["connect_timeout"] = _CONNECT_TIMEOUT_SECONDS
    try:
        return make_conninfo(**params)
    except psycopg.Error:
        raise DatabaseProbeError(ProbeFailure.INVALID_DSN) from None


def _pinned_address(target: str, trusted_hosts: frozenset[str]) -> str:
    """The one IP address a connection to `target` dials. Raises `DatabaseProbeError`.

    An empty target is the local socket, as is a path, and several hosts are
    several dial-outs to reason about one by one — all refused. A trusted host
    skips the private-address check but is still resolved once, here.
    """
    if not target or "," in target or target.startswith("/"):
        raise DatabaseProbeError(ProbeFailure.BLOCKED_ADDRESS)
    if target not in trusted_hosts:
        try:
            return pin_safe_host(target)
        except SSRFBlockedError:
            raise DatabaseProbeError(ProbeFailure.BLOCKED_ADDRESS) from None
    try:
        resolved = socket.getaddrinfo(target, None)
    except OSError:
        raise DatabaseProbeError(ProbeFailure.UNREACHABLE) from None
    # IPv4 first when there is one: `localhost` resolves to `::1` first on many
    # hosts, and a database that only listens on IPv4 would then look down to a
    # probe that, unlike libpq, tries exactly one address.
    ipv4 = [info for info in resolved if info[0] == socket.AF_INET]
    return str((ipv4 or resolved)[0][4][0])


def _view_columns(connection: psycopg.Connection[Any]) -> dict[tuple[str, str], list[str]]:
    """`{(schema, view): [column, …]}` in column order, system schemas left out."""
    found: dict[tuple[str, str], list[str]] = {}
    for schema, view, column in connection.execute(_VIEW_COLUMNS, (list(_SYSTEM_SCHEMAS),)):
        found.setdefault((schema, view), []).append(column)
    return found


def _counted(connection: psycopg.Connection[Any], columns: dict[tuple[str, str], list[str]]) -> tuple[ViewInfo, ...]:
    """Every view in `columns` with its row count — `None` once the budget is spent, or for one that would not count."""
    connection.execute(sql.SQL("SET statement_timeout = {}").format(sql.Literal(_COUNT_TIMEOUT_MS)))
    deadline = time.monotonic() + _COUNT_BUDGET_SECONDS
    return tuple(
        ViewInfo(
            name=_display_name(*key),
            columns=tuple(cols),
            row_count=_row_count(connection, *key) if time.monotonic() < deadline else None,
        )
        for key, cols in columns.items()
    )


def _row_count(connection: psycopg.Connection[Any], schema: str, view: str) -> int | None:
    """`count(*)` of one view, or `None` if it timed out or may not be read.

    Quoted as an identifier — the name came from the catalogue, not from a
    person. Autocommit, so a cancelled count leaves no aborted transaction
    behind for the next one.
    """
    statement = sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(view))
    try:
        row = connection.execute(statement).fetchone()
    except (psycopg.errors.QueryCanceled, psycopg.errors.InsufficientPrivilege):
        return None
    return int(row[0]) if row else 0


def _display_name(schema: str, name: str) -> str:
    """`name` as a creator writes it: bare in `public`, schema-qualified anywhere else."""
    return name if schema == "public" else f"{schema}.{name}"


def _qualified(display_name: str) -> sql.Composed:
    """`_display_name`'s inverse, quoted: `v_x` → `"public"."v_x"`, `sales.v_x` → `"sales"."v_x"`."""
    schema, _, name = display_name.rpartition(".")
    return sql.SQL("{}.{}").format(sql.Identifier(schema or "public"), sql.Identifier(name))


def _failure_of(exc: psycopg.Error) -> ProbeFailure:
    """Which `ProbeFailure` a psycopg error is, read off libpq's (English) message."""
    if isinstance(exc, psycopg.errors.ConnectionTimeout):
        return ProbeFailure.UNREACHABLE
    message = str(exc).lower()
    if (
        "authentication failed" in message
        or "no password supplied" in message
        or ('role "' in message and "does not exist" in message)
    ):
        return ProbeFailure.AUTH_REJECTED
    if 'database "' in message and "does not exist" in message:
        return ProbeFailure.DATABASE_NOT_FOUND
    if isinstance(exc, psycopg.errors.InsufficientPrivilege) or "permission denied" in message:
        return ProbeFailure.PERMISSION_DENIED
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return ProbeFailure.QUERY_TIMEOUT
    if isinstance(exc, psycopg.OperationalError):
        return ProbeFailure.UNREACHABLE
    return ProbeFailure.SERVER_ERROR
