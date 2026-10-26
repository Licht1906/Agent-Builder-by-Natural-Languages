"""SQL Server — the second dialect (DS-06), over pymssql: FreeTDS ships inside the wheel, so no ODBC driver to install.

**Read-only is made of three parts, because SQL Server has no read-only
transaction.** Only a statement this module compiled ever reaches the server
— a `SELECT` over a view, or `sp_set_session_context` — and neither a view
nor a function can write. Every connection runs inside a transaction that is
rolled back, never committed, on the way out, so not even a statement sent
through `connect` by hand outlives it. And the DSN guide asks for a
`db_datareader` login. Under snapshot isolation when the database allows it,
so a read neither blocks a writer nor waits for one.

**The address is pinned the way Postgres's is.** The host goes through
`pin_address`, and the address it returns is what FreeTDS dials. A named
instance (`erp\\SQLEXPRESS`) is resolved to its port by asking SQL Browser on
that same address ourselves — one datagram, a short wait — because FreeTDS's
own lookup waits 16 s for an instance that is not there.

**No user is refused, not handed to Windows.** pymssql with no user signs in
as this process's own Windows account (SSPI): the platform's identity,
offered to whatever host a creator named. `Integrated Security` and
`Trusted_Connection` are the same thing asked for by name, and refused as
options this module does not take.

**FreeTDS's timeouts are process-wide.** `dbsettime` is global, so a timeout
set on one connection is every connection's. Every connection here sets the
same two values, so none shortens another's; that is why a row count waits
as long as a query does rather than less. A statement that times out kills
its connection, so the probe dials a fresh one for the views still to count.

**Failures are read off DB-Lib's text**, and only ever reported as a
`ProbeFailure`. pymssql's error number is the last message FreeTDS saw in
this process, not necessarily this error's, and a login refused for a
missing database reads exactly like a wrong password — which is why one
failed login is retried against `master` to tell the two apart.
"""

import re
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pymssql
from sqlalchemy import Unicode, cast, func, literal_column, or_, select, table
from sqlalchemy.dialects import mssql
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
from src.datasources.query import PLAIN_NAME

#: DS-02's "lỗi dễ hiểu trong ≤ 5 s" — for the login, and for every statement.
_LOGIN_TIMEOUT_SECONDS = 5
_QUERY_TIMEOUT_SECONDS = 5
_LOCK_TIMEOUT_MS = 5000

#: The probe's ceiling for counting rows across every view; a view past it is
#: listed with no count rather than failing the probe.
_COUNT_BUDGET_SECONDS = 10

#: `COUNT_BIG`, not `count`: a view past 2³¹ rows overflows `int` and would
#: fail the whole probe rather than report its size.
_COUNT = func.count_big(literal_column("*"))

#: How many distinct values the spec form is offered — positions in a company.
_DISTINCT_LIMIT = 200

_DEFAULT_PORT = 1433
_DEFAULT_SCHEMA = "dbo"
_SYSTEM_SCHEMAS = ("sys", "INFORMATION_SCHEMA")

#: SQL Browser: one `CLNT_UCAST_INST` datagram, answered with the instance's
#: `tcp;<port>` — or not at all, for an instance that is not there.
_BROWSER_PORT = 1434
_BROWSER_TIMEOUT_SECONDS = 2
_INSTANCE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,15}$")

#: pymssql substitutes `%(name)s` itself; SQLAlchemy's `mssql+pymssql` flavour
#: writes `:name` unless told otherwise.
_SQL_DIALECT = mssql.pymssql.dialect(paramstyle="pyformat")

#: Every keyword a creator may write, under each spelling ADO.NET and ODBC
#: use for it (lower-cased, spaces and underscores dropped). Anything else is
#: refused, not dropped: `Integrated Security` signs in as this server, and
#: `Failover Partner` is a second host nobody checked.
_KEYWORDS = {
    "server": "server",
    "datasource": "server",
    "address": "server",
    "addr": "server",
    "networkaddress": "server",
    "host": "server",
    "port": "port",
    "database": "database",
    "initialcatalog": "database",
    "user": "user",
    "userid": "user",
    "uid": "user",
    "username": "user",
    "password": "password",
    "pwd": "password",
    "encrypt": "encrypt",
    "trustservercertificate": "trust_server_certificate",
    "applicationname": "appname",
    "app": "appname",
    "connecttimeout": "timeout",
    "connectiontimeout": "timeout",
    "timeout": "timeout",
}

#: `Encrypt=` as FreeTDS's `encryption`. "No" still encrypts when the server
#: offers it: a creator turning off a requirement is not asking for plaintext.
#: `strict` (TDS 8) is not something FreeTDS here can promise, so it is refused.
_ENCRYPTION = {
    "true": "require",
    "yes": "require",
    "mandatory": "require",
    "false": "request",
    "no": "request",
    "optional": "request",
}

#: Names that mean "this machine" in a SQL Server connection string, and the
#: protocol prefixes that never leave it (named pipes, shared memory).
_LOCAL_SERVERS = frozenset({".", "(local)"})
_LOCAL_PROTOCOLS = ("np:", "lpc:", "admin:", "(localdb)")

_VIEW_COLUMNS = """
SELECT c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME
FROM INFORMATION_SCHEMA.COLUMNS c
JOIN INFORMATION_SCHEMA.VIEWS v ON v.TABLE_SCHEMA = c.TABLE_SCHEMA AND v.TABLE_NAME = c.TABLE_NAME
WHERE c.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
"""

_BASE_TABLES = """
SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')
ORDER BY TABLE_SCHEMA, TABLE_NAME
"""

#: Run on every connection before anything else. `SET` inside `IF` still
#: lasts the session; snapshot only where the database has it switched on,
#: since asking for it elsewhere fails the first read.
_BEGIN = f"""
IF EXISTS (SELECT 1 FROM sys.databases WHERE database_id = DB_ID() AND snapshot_isolation_state = 1)
    SET TRANSACTION ISOLATION LEVEL SNAPSHOT;
SET LOCK_TIMEOUT {_LOCK_TIMEOUT_MS};
BEGIN TRANSACTION;
"""

_ROLLBACK = "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION"


@dataclass(frozen=True, slots=True)
class SqlServerTarget:
    """A SQL Server DSN, parsed: what to dial and how to sign in. Never logged, never echoed."""

    host: str
    port: int | None
    instance: str | None
    database: str
    user: str
    password: str
    encryption: str
    appname: str | None


class SqlServerDialect:
    """`Dialect` for SQL Server, over pymssql — one connection per call, never pooled."""

    def introspect(self, dsn: str, *, trusted_hosts: frozenset[str]) -> Introspection:
        """Every view with its columns and row count, plus the base tables. Raises `DatabaseProbeError`."""
        with self._reading(dsn, trusted_hosts) as cursor:
            cursor.execute(_VIEW_COLUMNS)
            columns: dict[tuple[str, str], list[str]] = {}
            for schema, view, column_name in cursor.fetchall():
                columns.setdefault((schema, view), []).append(column_name)
            cursor.execute(_BASE_TABLES)
            tables = tuple(_display_name(schema, name) for schema, name in cursor.fetchall())
        counts = self._counted(dsn, trusted_hosts, list(columns))
        views = tuple(
            ViewInfo(name=_display_name(*key), columns=tuple(names), row_count=counts.get(key))
            for key, names in columns.items()
        )
        return Introspection(views=views, base_tables=tables)

    def identity_lookup(self, dsn: str, *, trusted_hosts: frozenset[str]) -> IdentityLookup:
        """The asker's row by one equality, as a parameter — `None` for nobody and for an ambiguous match.

        Compared as text, so an `int` employee code matches the string an
        identity provider hands over — under the database's collation, which
        is usually case-insensitive: `AN@cty.vn` is `an@cty.vn`, and two rows
        that differ only by case are two rows, so nobody.
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
        """Distinct `(match, label)` pairs whose text contains `search.text` — bound, wildcards escaped.

        `[` is a wildcard too in SQL Server's `LIKE`, so it is escaped with the rest.
        """
        source = _table(search.match.view, (search.match.column, search.label_column))
        matched = cast(source.c[search.match.column], Unicode).label("match_value")
        label = cast(source.c[search.label_column], Unicode).label("label_value")
        escaped = re.sub(r"([\\%_\[])", r"\\\1", search.text)
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
        """`request` inside the rolled-back transaction every connection runs in, the asker's attributes set first.

        `sp_set_session_context` with key and value both bound, `@read_only`
        so nothing later in the session can change them — a customer's
        row-level security reads `SESSION_CONTEXT(N'employee_code')`. Names are
        lower-cased to match Postgres's `app.<name>` (session-context keys are
        case-insensitive anyway), so two spellings of one name are one key,
        set once; one that is still not a plain name is left off.
        """
        statement = select_for(request, default_schema=_DEFAULT_SCHEMA)
        attributes = {
            name.lower(): str(value)
            for name, value in request.session_attributes.items()
            if PLAIN_NAME.match(name.lower()) and value is not None
        }
        with self._reading(dsn, trusted_hosts, failure=ProbeFailure.QUERY_FAILED) as cursor:
            for name, value in attributes.items():
                cursor.execute("EXEC sp_set_session_context @key = %s, @value = %s, @read_only = 1", (name, value))
            _execute(cursor, statement)
            columns = tuple(description[0] for description in cursor.description or ())
            return QueryRows(columns=columns, rows=tuple(tuple(row) for row in cursor.fetchall()))

    def _counted(
        self, dsn: str, trusted_hosts: frozenset[str], views: list[tuple[str, str]]
    ) -> dict[tuple[str, str], int | None]:
        """`count(*)` of each view until the budget is spent — a fresh connection after each one that timed out."""
        deadline = time.monotonic() + _COUNT_BUDGET_SECONDS
        counts: dict[tuple[str, str], int | None] = {}
        pending = list(views)
        while pending and time.monotonic() < deadline:
            with self._reading(dsn, trusted_hosts) as cursor:
                while pending and time.monotonic() < deadline:
                    schema, view = pending.pop(0)
                    try:
                        _execute(cursor, select(_COUNT).select_from(table(view, schema=schema)))
                        row = cursor.fetchone()
                    except pymssql.Error as exc:
                        reason = _failure_of(exc)
                        if reason not in (ProbeFailure.QUERY_TIMEOUT, ProbeFailure.PERMISSION_DENIED):
                            raise
                        counts[schema, view] = None
                        if reason is ProbeFailure.QUERY_TIMEOUT:
                            break  # FreeTDS has closed this connection; dial a fresh one.
                        continue
                    counts[schema, view] = int(row[0]) if row else 0
        return counts

    @contextmanager
    def _reading(
        self, dsn: str, trusted_hosts: frozenset[str], *, failure: ProbeFailure = ProbeFailure.SERVER_ERROR
    ) -> Iterator[Any]:
        """A cursor on `connect`, with every driver error inside the block reported by reason — never its text.

        `failure` is what an error that is not about connecting is called, so
        the sentence a creator reads is about what they just did.
        """
        try:
            with self.connect(dsn, trusted_hosts=trusted_hosts) as connection:
                yield connection.cursor()
        except pymssql.Error as exc:
            reason = _failure_of(exc)
            raise DatabaseProbeError(failure if reason is ProbeFailure.SERVER_ERROR else reason) from None

    @contextmanager
    def connect(self, dsn: str, *, trusted_hosts: frozenset[str]) -> Iterator[Any]:
        """A short-lived connection inside a transaction that is always rolled back, dialled through `_dial`'s checks.

        Driver errors raised *inside* the block come out as themselves; the
        dialect's own methods read through `_reading`, which reports them by
        reason.
        """
        connection = _dial(parse_dsn(dsn), trusted_hosts)
        try:
            connection.cursor().execute(_BEGIN)
            yield connection
        finally:
            # A connection a timeout killed has nothing left to roll back.
            with suppress(pymssql.Error):
                connection.cursor().execute(_ROLLBACK)
            with suppress(pymssql.Error):
                connection.close()


def parse_dsn(dsn: str) -> SqlServerTarget:
    """`dsn` — a `mssql://`/`sqlserver://` URL or an ADO.NET `Key=Value;` string — as a target. Raises `DatabaseProbeError`.

    Nothing here dials or resolves anything; it only decides whether the DSN
    is one this dialect will dial at all.
    """
    text = dsn.strip()
    fields = _url_fields(text) if "://" in text.split(";", 1)[0] else _ado_fields(text)
    server = fields.get("server", "").strip()
    if server.lower().startswith("tcp:"):
        server = server[4:].strip()
    if server.lower() in _LOCAL_SERVERS or server.lower().startswith(_LOCAL_PROTOCOLS):
        raise DatabaseProbeError(ProbeFailure.BLOCKED_ADDRESS)
    host, port, instance = _split_server(server, fields.get("port"))
    user = fields.get("user", "")
    if not host or not user:
        # No user is no login of the creator's — FreeTDS would sign in as this server.
        raise DatabaseProbeError(ProbeFailure.INVALID_DSN)
    encrypt = fields.get("encrypt", "").strip().lower()
    if encrypt and encrypt not in _ENCRYPTION:
        raise DatabaseProbeError(ProbeFailure.DSN_OPTION_REFUSED)
    return SqlServerTarget(
        host=host,
        port=port,
        instance=instance,
        database=fields.get("database", ""),
        user=user,
        password=fields.get("password", ""),
        encryption=_ENCRYPTION.get(encrypt, "request"),
        appname=fields.get("appname") or None,
    )


def _url_fields(dsn: str) -> dict[str, str]:
    """`mssql://user:password@host:port/database?encrypt=yes` as keyword fields."""
    try:
        parts = urlsplit(dsn)
        port = parts.port
    except ValueError:
        raise DatabaseProbeError(ProbeFailure.INVALID_DSN) from None
    if parts.scheme.split("+", 1)[0].lower() not in ("mssql", "sqlserver"):
        raise DatabaseProbeError(ProbeFailure.INVALID_DSN)
    fields = _keywords(parse_qsl(parts.query, keep_blank_values=True))
    netloc = {
        "server": parts.hostname or "",
        "port": "" if port is None else str(port),
        "database": unquote(parts.path.lstrip("/")),
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
    }
    fields.update({key: value for key, value in netloc.items() if value})
    return fields


def _ado_fields(dsn: str) -> dict[str, str]:
    """`Server=erp,1433;Database=erp;User Id=ro;Password={a;b}` as keyword fields.

    Values may be wrapped in `{}` (ODBC, `}}` for a literal brace) or quotes
    (ADO.NET, doubled for a literal quote), which is how a password carries a
    `;`. A later keyword wins over an earlier one, as in ADO.NET.
    """
    pairs: list[tuple[str, str]] = []
    position, length = 0, len(dsn)
    while position < length:
        equals = dsn.find("=", position)
        if equals < 0:
            if dsn[position:].strip():
                raise DatabaseProbeError(ProbeFailure.INVALID_DSN)
            break
        key = dsn[position:equals].strip()
        position = equals + 1
        while position < length and dsn[position] == " ":
            position += 1
        if position < length and dsn[position] in "{\"'":
            value, position = _quoted(dsn, position)
            while position < length and dsn[position] == " ":
                position += 1
            if position < length and dsn[position] != ";":
                raise DatabaseProbeError(ProbeFailure.INVALID_DSN)
        else:
            end = dsn.find(";", position)
            end = length if end < 0 else end
            value, position = dsn[position:end].strip(), end
        position += 1
        if not key:
            raise DatabaseProbeError(ProbeFailure.INVALID_DSN)
        pairs.append((key, value))
    return _keywords(pairs)


def _quoted(dsn: str, start: int) -> tuple[str, int]:
    """The value opened at `start` by `{`, `"` or `'`, and the index just past its closing mark."""
    closing = "}" if dsn[start] == "{" else dsn[start]
    value: list[str] = []
    position = start + 1
    while position < len(dsn):
        char = dsn[position]
        if char == closing:
            if dsn[position + 1 : position + 2] == closing:
                value.append(char)
                position += 2
                continue
            return "".join(value), position + 1
        value.append(char)
        position += 1
    raise DatabaseProbeError(ProbeFailure.INVALID_DSN)


def _keywords(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Each pair under its canonical keyword. Raises `DatabaseProbeError` for one this dialect does not take."""
    fields: dict[str, str] = {}
    for key, value in pairs:
        canonical = _KEYWORDS.get(key.lower().replace(" ", "").replace("_", ""))
        if canonical is None:
            raise DatabaseProbeError(ProbeFailure.DSN_OPTION_REFUSED)
        fields[canonical] = value
    return fields


def _split_server(server: str, port_field: str | None) -> tuple[str, int | None, str | None]:
    """`host`, `host,1433` or `host\\INSTANCE` as `(host, port, instance)`. Raises `DatabaseProbeError`."""
    host, _, port_text = server.partition(",")
    host, _, instance = host.partition("\\")
    port_text = port_text.strip() or (port_field or "").strip()
    port: int | None = None
    if port_text:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise DatabaseProbeError(ProbeFailure.INVALID_DSN)
        port = int(port_text)
    instance = instance.strip()
    if instance and not _INSTANCE_NAME.match(instance):
        raise DatabaseProbeError(ProbeFailure.INVALID_DSN)
    return host.strip(), port, instance or None


def _dial(target: SqlServerTarget, trusted_hosts: frozenset[str]) -> Any:
    """An autocommit pymssql connection to `target`'s one checked address. Raises `DatabaseProbeError`.

    A login refused while naming a database is tried once more against
    `master`: pymssql reports "no such database" and "wrong password" in the
    same words, and only a second login tells them apart.
    """
    address = pin_address(target.host, trusted_hosts)
    port = target.port or (_instance_port(address, target.instance) if target.instance else _DEFAULT_PORT)
    try:
        return _login(target, address, port)
    except pymssql.Error as exc:
        reason = _failure_of(exc)
    if reason is ProbeFailure.AUTH_REJECTED and target.database and target.database.lower() != "master":
        try:
            _login(replace(target, database="master"), address, port).close()
        except pymssql.Error:
            pass
        else:
            reason = ProbeFailure.DATABASE_NOT_FOUND
    # Anything else that stops a login — refused, timed out, TLS — is a server not reached.
    if reason not in (ProbeFailure.AUTH_REJECTED, ProbeFailure.DATABASE_NOT_FOUND):
        reason = ProbeFailure.UNREACHABLE
    raise DatabaseProbeError(reason)


def _login(target: SqlServerTarget, address: str, port: int) -> Any:
    """`pymssql.connect` with every option set here — nothing about how to dial is the DSN's to choose."""
    return pymssql.connect(
        server=address,
        port=str(port),
        user=target.user,
        password=target.password,
        database=target.database,
        login_timeout=_LOGIN_TIMEOUT_SECONDS,
        timeout=_QUERY_TIMEOUT_SECONDS,
        appname=target.appname or "agent-builder",
        encryption=target.encryption,
        autocommit=True,
    )


def _instance_port(address: str, instance: str) -> int:
    """The TCP port SQL Browser on `address` says `instance` listens on. Raises `DatabaseProbeError`.

    The socket is connected to `address`, so only a reply from the host that
    was checked is read; no reply, or one without a TCP port, is unreachable.
    """
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as browser:
        browser.settimeout(_BROWSER_TIMEOUT_SECONDS)
        try:
            browser.connect((address, _BROWSER_PORT))
            browser.send(b"\x04" + instance.encode("ascii"))
            reply = browser.recv(4096)
        except OSError:
            raise DatabaseProbeError(ProbeFailure.UNREACHABLE) from None
    fields = reply[3:].decode("ascii", "replace").split(";")
    port = dict(zip(fields[::2], fields[1::2], strict=False)).get("tcp", "")
    if not port.isdigit():
        raise DatabaseProbeError(ProbeFailure.UNREACHABLE)
    return int(port)


def _execute(cursor: Any, statement: Select[Any]) -> Any:
    """`statement` compiled for SQL Server and run on `cursor` — every value a pymssql parameter."""
    compiled = statement.compile(dialect=_SQL_DIALECT, compile_kwargs={"render_postcompile": True})
    cursor.execute(str(compiled), compiled.params)
    return cursor


def _table(display_name: str, columns: tuple[str, ...]) -> Any:
    """`_display_name`'s inverse as a table SQLAlchemy quotes: `v_x` → `[dbo].[v_x]`."""
    return source_table(display_name, tuple(dict.fromkeys(columns)), default_schema=_DEFAULT_SCHEMA)


def _display_name(schema: str, name: str) -> str:
    """`name` as a creator writes it: bare in `dbo`, schema-qualified anywhere else."""
    return name if schema == _DEFAULT_SCHEMA else f"{schema}.{name}"


def _failure_of(exc: pymssql.Error) -> ProbeFailure:
    """Which `ProbeFailure` a pymssql error is, read off DB-Lib's (English) text."""
    message = str(exc).lower()
    if "login failed" in message or ("20018" in message and "severity 14" in message):
        return ProbeFailure.AUTH_REJECTED
    if "permission was denied" in message or "permission denied" in message:
        return ProbeFailure.PERMISSION_DENIED
    if "timed out" in message:
        return ProbeFailure.QUERY_TIMEOUT
    if "unable to connect" in message or "connection failed" in message or "dbprocess is dead" in message:
        return ProbeFailure.UNREACHABLE
    return ProbeFailure.SERVER_ERROR
