"""MySQL — the third dialect (DS-07), over PyMySQL: pure Python, nothing to install beside it.

**Read-only is the session's own setting.** Every connection runs
`SET SESSION TRANSACTION READ ONLY` before anything else, so every statement
on it — autocommit or not, `INSERT` or `CREATE TABLE` — is refused by MySQL
itself (1792), and a structured query runs inside `START TRANSACTION READ
ONLY, WITH CONSISTENT SNAPSHOT` on top of that. `MAX_EXECUTION_TIME` stops a
`SELECT` that outruns its ceiling, and leaves the connection usable.

**No session attributes — the one defence MySQL does not get** (ADR-0059 §8).
MySQL has no session context a customer's row-level security could read, so
`QueryRequest.session_attributes` is not sent: the asker's scope is the
`WHERE` the code builds, and nothing else. The Hệ CSDL screen says so beside
every MySQL source.

**One database per Hệ CSDL.** A MySQL "schema" is a database, so the views a
probe lists are the DSN's database's, by bare name, and a query reads them
unqualified — the DSN must name a database.

**The address is pinned the way Postgres's is**: the host goes through
`pin_address` and PyMySQL dials the address it returns, over TCP only.
Failures are read off MySQL's error numbers and reported as a `ProbeFailure`,
never as the server's sentence — which names the user and the host.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pymysql
from sqlalchemy import Unicode, cast, func, or_, select
from sqlalchemy.dialects import mysql
from sqlalchemy.sql import Select

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
    pin_address,
)
from src.datasources.dialects.sql import select_for, source_table

#: DS-02's "lỗi dễ hiểu trong ≤ 5 s". The socket timeouts are a backstop
#: behind `MAX_EXECUTION_TIME`, which only ever stops a `SELECT`.
_CONNECT_TIMEOUT_SECONDS = 5
_STATEMENT_TIMEOUT_MS = 5000
_SOCKET_TIMEOUT_SECONDS = 10

#: Counting rows grows with the database: a ceiling per view and one for all.
_COUNT_TIMEOUT_MS = 2000
_COUNT_BUDGET_SECONDS = 10

#: How many distinct values the spec form is offered — positions in a company.
_DISTINCT_LIMIT = 200

_DEFAULT_PORT = 3306

#: PyMySQL formats `%(name)s` itself; SQLAlchemy's `mysql+pymysql` writes `%s` unless told otherwise.
_SQL_DIALECT = mysql.pymysql.dialect(paramstyle="pyformat")

#: `ssl-mode`, as MySQL's own client spells it. `VERIFY_CA`/`VERIFY_IDENTITY`
#: need a CA file on this server and a host name where we dial a pinned
#: address, so they are refused rather than quietly downgraded.
_SSL_MODES = frozenset({"disabled", "preferred", "required"})

#: The query parameters a creator may set. Everything else PyMySQL or the
#: MySQL client knows reaches past the DSN into this server — `unix_socket`,
#: `read_default_file`, `local_infile`, `init_command` — and is refused.
_PARAMETERS = {"ssl-mode": "ssl_mode", "ssl_mode": "ssl_mode", "sslmode": "ssl_mode", "connect_timeout": "timeout"}

#: MySQL's error numbers, by what they mean to a creator.
_AUTH_REJECTED = frozenset({1045, 1698, 2059, 1251})
_DATABASE_NOT_FOUND = frozenset({1049})
_PERMISSION_DENIED = frozenset({1044, 1142, 1143, 1227, 1356})
_TIMED_OUT = frozenset({3024, 1969, 1317})
_UNREACHABLE = frozenset({2003, 2005, 2006, 2013, 2026})

_VIEW_COLUMNS = """
SELECT c.TABLE_NAME, c.COLUMN_NAME
FROM information_schema.COLUMNS c
JOIN information_schema.VIEWS v ON v.TABLE_SCHEMA = c.TABLE_SCHEMA AND v.TABLE_NAME = c.TABLE_NAME
WHERE c.TABLE_SCHEMA = DATABASE()
ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
"""

_BASE_TABLES = """
SELECT TABLE_NAME FROM information_schema.TABLES
WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_NAME
"""


@dataclass(frozen=True, slots=True)
class MySqlTarget:
    """A MySQL DSN, parsed: what to dial and how to sign in. Never logged, never echoed."""

    host: str
    port: int
    database: str
    user: str
    password: str
    ssl_mode: str


class MySqlDialect:
    """`Dialect` for MySQL, over PyMySQL — one connection per call, never pooled."""

    def introspect(self, dsn: str, *, trusted_hosts: frozenset[str]) -> Introspection:
        """Every view of the DSN's database with its columns and row count, plus the base tables."""
        with self._reading(dsn, trusted_hosts) as cursor:
            cursor.execute(_VIEW_COLUMNS)
            columns: dict[str, list[str]] = {}
            for view, column_name in cursor.fetchall():
                columns.setdefault(view, []).append(column_name)
            cursor.execute(_BASE_TABLES)
            tables = tuple(row[0] for row in cursor.fetchall())
            counts = _counted(cursor, list(columns))
        views = tuple(
            ViewInfo(name=name, columns=tuple(names), row_count=counts.get(name)) for name, names in columns.items()
        )
        return Introspection(views=views, base_tables=tables)

    def identity_lookup(self, dsn: str, *, trusted_hosts: frozenset[str]) -> IdentityLookup:
        """The asker's row by one equality, as a parameter — `None` for nobody and for an ambiguous match.

        Compared as text under the column's collation, usually
        case-insensitive: `AN@cty.vn` is `an@cty.vn`, and two rows that differ
        only by case are two rows, so nobody.
        """

        def lookup(match: ColumnRef, value: Any, columns: tuple[str, ...]) -> dict[str, Any] | None:
            source = _table(match.view, (match.column, *columns))
            statement = (
                select(*(source.c[name] for name in columns))
                .where(cast(source.c[match.column], Unicode) == str(value))
                .limit(2)
            )
            with self._reading(dsn, trusted_hosts) as cursor:
                rows = _execute(cursor, statement).fetchall()
            return dict(zip(columns, rows[0], strict=True)) if len(rows) == 1 else None

        return lookup

    def distinct_values(self, dsn: str, column: ColumnRef, *, trusted_hosts: frozenset[str]) -> list[Any]:
        """`SELECT DISTINCT` of one column, non-null, sorted, at most `_DISTINCT_LIMIT` values."""
        target = _table(column.view, (column.column,)).c[column.column]
        statement = select(target).where(target.is_not(None)).distinct().order_by(target).limit(_DISTINCT_LIMIT)
        with self._reading(dsn, trusted_hosts) as cursor:
            return [row[0] for row in _execute(cursor, statement).fetchall()]

    def identity_candidates(
        self, dsn: str, search: IdentitySearch, *, trusted_hosts: frozenset[str]
    ) -> list[tuple[Any, Any]]:
        """Distinct `(match, label)` pairs whose text contains `search.text` — bound, wildcards escaped."""
        source = _table(search.match.view, (search.match.column, search.label_column))
        matched = cast(source.c[search.match.column], Unicode).label("match_value")
        label = cast(source.c[search.label_column], Unicode).label("label_value")
        escaped = search.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        statement = (
            select(matched, label)
            .distinct()
            .where(or_(matched.ilike(pattern, escape="\\"), label.ilike(pattern, escape="\\")))
            .order_by(matched)
            .limit(search.limit)
        )
        with self._reading(dsn, trusted_hosts) as cursor:
            return [(row[0], row[1]) for row in _execute(cursor, statement).fetchall()]

    def run_query(self, dsn: str, request: QueryRequest, *, trusted_hosts: frozenset[str]) -> QueryRows:
        """`request` in one read-only, consistent-snapshot transaction — rolled back, never committed.

        `request.session_attributes` is deliberately not sent: MySQL has
        nowhere a customer's own security could read it (ADR-0059 §8).
        """
        statement = select_for(request, default_schema=None)
        with self._reading(dsn, trusted_hosts, failure=ProbeFailure.QUERY_FAILED) as cursor:
            cursor.execute("START TRANSACTION READ ONLY, WITH CONSISTENT SNAPSHOT")
            try:
                _execute(cursor, statement)
                columns = tuple(description[0] for description in cursor.description or ())
                return QueryRows(columns=columns, rows=tuple(tuple(row) for row in cursor.fetchall()))
            finally:
                cursor.execute("ROLLBACK")

    @contextmanager
    def _reading(
        self, dsn: str, trusted_hosts: frozenset[str], *, failure: ProbeFailure = ProbeFailure.SERVER_ERROR
    ) -> Iterator[Any]:
        """A cursor on `connect`, with every driver error inside the block reported by reason — never its text."""
        try:
            with self.connect(dsn, trusted_hosts=trusted_hosts) as connection:
                yield connection.cursor()
        except pymysql.Error as exc:
            reason = _failure_of(exc)
            raise DatabaseProbeError(failure if reason is ProbeFailure.SERVER_ERROR else reason) from None

    @contextmanager
    def connect(self, dsn: str, *, trusted_hosts: frozenset[str]) -> Iterator[Any]:
        """A short-lived, autocommit connection whose every transaction is read-only. Raises `DatabaseProbeError`.

        Driver errors raised *inside* the block come out as themselves; the
        dialect's own methods read through `_reading`, which reports them by reason.
        """
        connection = _dial(parse_dsn(dsn), trusted_hosts)
        try:
            yield connection
        finally:
            connection.close()


def parse_dsn(dsn: str) -> MySqlTarget:
    """`mysql://user:password@host:3306/database?ssl-mode=REQUIRED` as a target. Raises `DatabaseProbeError`.

    `mysql+pymysql://` and a leading `jdbc:` are the same URL. Nothing here
    dials or resolves anything.
    """
    text = dsn.strip()
    if text.lower().startswith("jdbc:"):
        text = text[5:]
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise DatabaseProbeError(ProbeFailure.INVALID_DSN) from None
    if parts.scheme.split("+", 1)[0].lower() not in ("mysql", "mariadb"):
        raise DatabaseProbeError(ProbeFailure.INVALID_DSN)
    fields: dict[str, str] = {}
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        canonical = _PARAMETERS.get(key.lower())
        if canonical is None:
            raise DatabaseProbeError(ProbeFailure.DSN_OPTION_REFUSED)
        fields[canonical] = value
    ssl_mode = fields.get("ssl_mode", "preferred").strip().lower()
    if ssl_mode not in _SSL_MODES:
        raise DatabaseProbeError(ProbeFailure.DSN_OPTION_REFUSED)
    database = unquote(parts.path.lstrip("/"))
    user = unquote(parts.username or "")
    # No user is whoever runs this process (PyMySQL's default); no database is no views to list.
    if not parts.hostname or not user or not database or "/" in database:
        raise DatabaseProbeError(ProbeFailure.INVALID_DSN)
    return MySqlTarget(
        host=parts.hostname,
        port=port or _DEFAULT_PORT,
        database=database,
        user=user,
        password=unquote(parts.password or ""),
        ssl_mode=ssl_mode,
    )


def _dial(target: MySqlTarget, trusted_hosts: frozenset[str]) -> Any:
    """A connection to `target`'s one checked address, read-only and time-boxed. Raises `DatabaseProbeError`.

    `required` is checked after the handshake rather than trusted to it:
    PyMySQL quietly carries on in plaintext when a server offers no TLS.
    """
    address = pin_address(target.host, trusted_hosts)
    try:
        connection = pymysql.connect(
            host=address,
            port=target.port,
            user=target.user,
            password=target.password,
            database=target.database,
            charset="utf8mb4",
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            read_timeout=_SOCKET_TIMEOUT_SECONDS,
            write_timeout=_SOCKET_TIMEOUT_SECONDS,
            autocommit=True,
            local_infile=False,
            ssl_disabled=target.ssl_mode == "disabled",
        )
    except pymysql.Error as exc:
        reason = _failure_of(exc)
        if reason not in (ProbeFailure.AUTH_REJECTED, ProbeFailure.DATABASE_NOT_FOUND, ProbeFailure.PERMISSION_DENIED):
            reason = ProbeFailure.UNREACHABLE
        raise DatabaseProbeError(reason) from None
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            _set_statement_timeout(cursor, _STATEMENT_TIMEOUT_MS)
            if target.ssl_mode == "required":
                cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
                row = cursor.fetchone()
                if not row or not row[1]:
                    raise DatabaseProbeError(ProbeFailure.UNREACHABLE)
    except BaseException:
        connection.close()
        raise
    return connection


def _set_statement_timeout(cursor: Any, milliseconds: int) -> None:
    """The ceiling on every `SELECT` this session runs — MySQL's `MAX_EXECUTION_TIME`, MariaDB's own name for it."""
    try:
        cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (milliseconds,))
    except pymysql.err.OperationalError as exc:
        if exc.args[0] != 1193:  # unknown system variable: MariaDB
            raise
        cursor.execute("SET SESSION max_statement_time = %s", (milliseconds / 1000,))


def _counted(cursor: Any, views: list[str]) -> dict[str, int | None]:
    """`count(*)` of each view — `None` once the budget is spent, or for one that would not count in time or may not be read."""
    _set_statement_timeout(cursor, _COUNT_TIMEOUT_MS)
    deadline = time.monotonic() + _COUNT_BUDGET_SECONDS
    counts: dict[str, int | None] = {}
    for view in views:
        if time.monotonic() >= deadline:
            break
        try:
            row = _execute(cursor, select(func.count()).select_from(_table(view, ()))).fetchone()
        except pymysql.Error as exc:
            if _failure_of(exc) not in (ProbeFailure.QUERY_TIMEOUT, ProbeFailure.PERMISSION_DENIED):
                raise
            counts[view] = None
            continue
        counts[view] = int(row[0]) if row else 0
    _set_statement_timeout(cursor, _STATEMENT_TIMEOUT_MS)
    return counts


def _execute(cursor: Any, statement: Select[Any]) -> Any:
    """`statement` compiled for MySQL and run on `cursor` — every value a PyMySQL parameter."""
    compiled = statement.compile(dialect=_SQL_DIALECT, compile_kwargs={"render_postcompile": True})
    cursor.execute(str(compiled), compiled.params)
    return cursor


def _table(view: str, columns: tuple[str, ...]) -> Any:
    """`view` of the connection's own database, unqualified, as a table SQLAlchemy quotes."""
    return source_table(view, tuple(dict.fromkeys(columns)), default_schema=None)


def _failure_of(exc: pymysql.Error) -> ProbeFailure:
    """Which `ProbeFailure` a PyMySQL error is, by MySQL's error number."""
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if code in _AUTH_REJECTED:
        return ProbeFailure.AUTH_REJECTED
    if code in _DATABASE_NOT_FOUND:
        return ProbeFailure.DATABASE_NOT_FOUND
    if code in _PERMISSION_DENIED:
        return ProbeFailure.PERMISSION_DENIED
    if code in _TIMED_OUT:
        return ProbeFailure.QUERY_TIMEOUT
    if code in _UNREACHABLE:
        return ProbeFailure.UNREACHABLE
    return ProbeFailure.SERVER_ERROR
