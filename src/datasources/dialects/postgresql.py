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

from src.datasources.dialects.base import DatabaseProbeError, Introspection, ProbeFailure, ViewInfo
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
        try:
            with self.connect(dsn, trusted_hosts=trusted_hosts) as connection:
                columns = _view_columns(connection)
                tables = tuple(
                    _display_name(schema, name)
                    for schema, name in connection.execute(_BASE_TABLES, (list(_SYSTEM_SCHEMAS),))
                )
                views = _counted(connection, columns)
        except psycopg.Error as exc:
            raise DatabaseProbeError(_failure_of(exc)) from None
        return Introspection(views=views, base_tables=tables)

    @contextmanager
    def connect(self, dsn: str, *, trusted_hosts: frozenset[str]) -> Iterator[psycopg.Connection[Any]]:
        """A short-lived, autocommit, read-only connection, dialled through `_dial_conninfo`'s checks.

        Not on the `Dialect` interface yet — `introspect` is its only caller
        until `data_query` (DS-04) runs a query through it.
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
        return str(socket.getaddrinfo(target, None)[0][4][0])
    except OSError:
        raise DatabaseProbeError(ProbeFailure.UNREACHABLE) from None


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
    if isinstance(exc, psycopg.OperationalError):
        return ProbeFailure.UNREACHABLE
    return ProbeFailure.SERVER_ERROR
