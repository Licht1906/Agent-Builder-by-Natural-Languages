"""The SQL Server dialect — DS-06 / ADR-0059 §8.

Two halves. The DSN half needs no server: which connection strings are
dialled at all, and which are refused before anything leaves the process.
The rest runs against the SQL Server `TEST_MSSQL_URL` names (CI starts one)
and is skipped, by name, without it — the same probe, failure and identity
checks `test_postgresql_dialect` holds Postgres to, plus what is SQL Server's
own: snapshot reads, `SESSION_CONTEXT`, named instances.
"""

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import replace

import pytest

from src.datasources.dialects import (
    ColumnRef,
    DatabaseProbeError,
    IdentitySearch,
    ProbeFailure,
    QueryRequest,
    schema_problems,
)
from src.datasources.dialects import sqlserver as sqlserver_module
from src.datasources.dialects.sqlserver import SqlServerDialect, SqlServerTarget, parse_dsn
from src.datasources.query import StructuredQuery
from tests.test_datasources.backends import MSSQL_URL_VARIABLE, Backend, sqlserver_backend

DIALECT = SqlServerDialect()

# --- The DSN, before anything is dialled ------------------------------------


def test_a_url_and_an_ado_string_name_the_same_target() -> None:
    url = parse_dsn("mssql://ro:p%40ss@erp.example.com:1444/erp?encrypt=yes")
    ado = parse_dsn("Server=tcp:erp.example.com,1444;Initial Catalog=erp;User ID=ro;Password=p@ss;Encrypt=True")

    assert (
        url
        == ado
        == SqlServerTarget(
            host="erp.example.com",
            port=1444,
            instance=None,
            database="erp",
            user="ro",
            password="p@ss",
            encryption="require",
            appname=None,
        )
    )


def test_a_braced_password_may_carry_a_semicolon() -> None:
    target = parse_dsn("Server=erp.example.com;Database=erp;UID=ro;PWD={a;b}}c};Encrypt=no")

    assert target.password == "a;b}c"
    assert target.encryption == "request"  # "no" never asks for plaintext


def test_a_named_instance_is_kept_for_sql_browser() -> None:
    target = parse_dsn(r"Data Source=erp.example.com\SQLEXPRESS;Database=erp;User Id=ro;Password=x")

    assert (target.host, target.port, target.instance) == ("erp.example.com", None, "SQLEXPRESS")


@pytest.mark.parametrize(
    "option",
    ["Integrated Security=SSPI", "Trusted_Connection=yes", "Failover Partner=other.example.com", "Encrypt=strict"],
)
def test_an_option_that_signs_in_as_this_server_or_dials_elsewhere_is_refused(option: str) -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(f"Server=erp.example.com;User Id=ro;Password=x;{option}", trusted_hosts=frozenset())

    assert caught.value.reason is ProbeFailure.DSN_OPTION_REFUSED


def test_no_user_is_refused_rather_than_signed_in_as_this_servers_windows_account() -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect("Server=erp.example.com;Database=erp", trusted_hosts=frozenset({"erp.example.com"}))

    assert caught.value.reason is ProbeFailure.INVALID_DSN


@pytest.mark.parametrize("server", [".", "(local)", r"(localdb)\MSSQLLocalDB", r"np:\\.\pipe\sql\query", "lpc:erp"])
def test_a_server_name_that_means_this_machine_is_never_dialled(server: str) -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(f"Server={server};User Id=ro;Password=x", trusted_hosts=frozenset())

    assert caught.value.reason is ProbeFailure.BLOCKED_ADDRESS


@pytest.mark.parametrize(
    "dsn", ["Server=erp;User Id=ro;Password={bi-mat-4471", "Server=erp,99999;User Id=ro;Password=bi-mat-4471"]
)
def test_a_malformed_dsn_is_refused_without_repeating_it(dsn: str) -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(dsn, trusted_hosts=frozenset({"erp"}))

    assert caught.value.reason is ProbeFailure.INVALID_DSN
    assert "bi-mat-4471" not in str(caught.value)


def test_an_internal_host_nobody_trusted_is_never_dialled() -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect("Server=127.0.0.1,1433;User Id=ro;Password=x", trusted_hosts=frozenset())

    assert caught.value.reason is ProbeFailure.BLOCKED_ADDRESS


# --- Against a real SQL Server ----------------------------------------------


@pytest.fixture
def mssql() -> Backend:
    found = sqlserver_backend()
    if found is None:
        pytest.skip(f"{MSSQL_URL_VARIABLE} is not set — no SQL Server to run against")
    return found


def _dsn(backend: Backend, **overrides: object) -> str:
    """The test server's DSN as an ADO.NET string, with any part of the target replaced."""
    target = replace(parse_dsn(backend.dsn), **overrides)  # type: ignore[arg-type]
    server = (
        target.host + (f",{target.port}" if target.port else "") + (f"\\{target.instance}" if target.instance else "")
    )
    return f"Server={server};Database={target.database};User Id={target.user};Password={{{target.password}}}"


@pytest.fixture
def staff(mssql: Backend) -> Iterator[str]:
    """An employee view, and the base table it pretends to read — what a creator's staging ERP holds."""
    mssql.execute("IF OBJECT_ID('dbo.ms_staff_table') IS NULL CREATE TABLE dbo.ms_staff_table (id int)")
    mssql.create_values_view(
        "v_ms_staff",
        ("email", "emp_id", "dept_code", "position"),
        [
            ("an@cty.vn", "E102", "SALES", "Nhân viên KD"),
            ("binh@cty.vn", "E200", "SALES", "Trưởng nhóm KD"),
            ("chi@cty.vn", "E300", "FIN", "Kế toán trưởng"),
            ("trung@cty.vn", "E401", "SALES", "Nhân viên KD"),
            ("trung@cty.vn", "E402", "SALES", "Nhân viên KD"),
        ],
    )
    yield "v_ms_staff"
    mssql.drop_view("v_ms_staff")
    mssql.execute("DROP TABLE IF EXISTS dbo.ms_staff_table")


def test_introspection_lists_the_views_with_their_columns_and_row_counts(mssql: Backend, staff: str) -> None:
    found = DIALECT.introspect(mssql.dsn, trusted_hosts=mssql.trusted)

    views = {view.name: view for view in found.views}
    assert views[staff].columns == ("email", "emp_id", "dept_code", "position")
    assert views[staff].row_count == 5


def test_introspection_names_base_tables_apart_from_views(mssql: Backend, staff: str) -> None:
    found = DIALECT.introspect(mssql.dsn, trusted_hosts=mssql.trusted)

    assert "ms_staff_table" in found.base_tables
    assert "ms_staff_table" not in {view.name for view in found.views}
    assert not [view.name for view in found.views if view.name.startswith(("sys.", "INFORMATION_SCHEMA."))]


def test_the_ado_form_of_the_same_dsn_probes_the_same(mssql: Backend, staff: str) -> None:
    found = DIALECT.introspect(_dsn(mssql), trusted_hosts=mssql.trusted)

    assert staff in {view.name for view in found.views}


def test_a_wrong_password_is_named_as_such_and_the_password_is_never_echoed(mssql: Backend) -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(_dsn(mssql, password="sai-mat-khau-9137"), trusted_hosts=mssql.trusted)

    assert caught.value.reason is ProbeFailure.AUTH_REJECTED
    assert "sai-mat-khau-9137" not in str(caught.value)


def test_a_database_that_does_not_exist_is_named_as_such(mssql: Backend) -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(_dsn(mssql, database="khong_co_db_nay"), trusted_hosts=mssql.trusted)

    assert caught.value.reason is ProbeFailure.DATABASE_NOT_FOUND


def test_nothing_listening_fails_within_five_seconds(mssql: Backend) -> None:
    started = time.monotonic()
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(_dsn(mssql, port=1), trusted_hosts=mssql.trusted)

    assert caught.value.reason is ProbeFailure.UNREACHABLE
    assert time.monotonic() - started <= 5.5


def test_an_instance_sql_browser_does_not_know_fails_fast(mssql: Backend) -> None:
    """FreeTDS's own lookup waits 16 s for an instance that is not there; ours asks once."""
    started = time.monotonic()
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(_dsn(mssql, port=None, instance="KHONGCO"), trusted_hosts=mssql.trusted)

    assert caught.value.reason is ProbeFailure.UNREACHABLE
    assert time.monotonic() - started <= 3


@pytest.fixture
def sql_browser(mssql: Backend, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A stand-in SQL Browser answering for instance `ERP` with the test server's port.

    On a port of its own — a real one may already hold 1434 on a developer's
    machine — and pointed at by the dialect's `_BROWSER_PORT`.
    """
    port = parse_dsn(mssql.dsn).port or 1433
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(5)
    monkeypatch.setattr(sqlserver_module, "_BROWSER_PORT", listener.getsockname()[1])

    def answer() -> None:
        # Every dial asks again — the probe's row counts come over a connection of their own.
        while True:
            try:
                request, sender = listener.recvfrom(64)
            except OSError:
                return
            if request == b"ERP":
                reply = f"ServerName;ERPHOST;InstanceName;ERP;IsClustered;No;Version;16.0;tcp;{port};;".encode("ascii")
                listener.sendto(b"" + len(reply).to_bytes(2, "little") + reply, sender)

    responder = threading.Thread(target=answer, daemon=True)
    responder.start()
    yield "ERP"
    listener.close()
    responder.join(timeout=5)


def test_a_named_instance_is_dialled_on_the_port_sql_browser_names(
    mssql: Backend, staff: str, sql_browser: str
) -> None:
    found = DIALECT.introspect(_dsn(mssql, port=None, instance=sql_browser), trusted_hosts=mssql.trusted)

    assert staff in {view.name for view in found.views}


@pytest.fixture
def slow_view(mssql: Backend) -> Iterator[str]:
    """A view whose `count(*)` outlasts the query timeout — named to be counted first."""
    mssql.execute(
        "CREATE OR ALTER VIEW v_ms_a_slow AS SELECT a.object_id AS x "
        "FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c "
        # A per-row filter, or the optimiser multiplies three counts instead of reading 10⁹ rows.
        "WHERE CHECKSUM(a.object_id, b.object_id, c.object_id) % 7 = 3"
    )
    yield "v_ms_a_slow"
    mssql.drop_view("v_ms_a_slow")


def test_a_view_too_slow_to_count_is_listed_without_a_count_and_the_rest_still_are(
    mssql: Backend, staff: str, slow_view: str
) -> None:
    found = DIALECT.introspect(mssql.dsn, trusted_hosts=mssql.trusted)

    views = {view.name: view for view in found.views}
    assert views[slow_view].row_count is None
    assert views[staff].row_count == 5


def test_the_identity_lookup_reads_one_askers_row(mssql: Backend, staff: str) -> None:
    lookup = DIALECT.identity_lookup(mssql.dsn, trusted_hosts=mssql.trusted)

    row = lookup(ColumnRef(view=staff, column="email"), "an@cty.vn", ("emp_id", "dept_code", "position"))

    assert row == {"emp_id": "E102", "dept_code": "SALES", "position": "Nhân viên KD"}


def test_the_identity_lookup_finds_nobody_for_an_unknown_or_ambiguous_value(mssql: Backend, staff: str) -> None:
    lookup = DIALECT.identity_lookup(mssql.dsn, trusted_hosts=mssql.trusted)
    email = ColumnRef(view=staff, column="email")

    assert lookup(email, "la@cty.vn", ("emp_id",)) is None
    assert lookup(email, "trung@cty.vn", ("emp_id",)) is None
    assert lookup(email, "x' OR '1'='1", ("emp_id",)) is None


def test_distinct_values_lists_every_position_once(mssql: Backend, staff: str) -> None:
    values = DIALECT.distinct_values(mssql.dsn, ColumnRef(view=staff, column="position"), trusted_hosts=mssql.trusted)

    assert values == ["Kế toán trưởng", "Nhân viên KD", "Trưởng nhóm KD"]


@pytest.mark.parametrize(("text", "expected"), [("AN@", [("an@cty.vn", "Nhân viên KD")]), ("%", []), ("[a-z]", [])])
def test_identity_candidates_match_text_only_as_text(
    mssql: Backend, staff: str, text: str, expected: list[tuple[str, str]]
) -> None:
    """Case-insensitive, and `%` or `[a-z]` are characters to find — not patterns that match everyone."""
    search = IdentitySearch(match=ColumnRef(view=staff, column="email"), label_column="position", text=text, limit=10)

    assert DIALECT.identity_candidates(mssql.dsn, search, trusted_hosts=mssql.trusted) == expected


def test_nothing_written_in_a_session_outlives_it(mssql: Backend) -> None:
    with DIALECT.connect(mssql.dsn, trusted_hosts=mssql.trusted) as connection:
        connection.cursor().execute("CREATE TABLE ms_should_never_exist (id int)")

    assert mssql.scalar("SELECT OBJECT_ID('dbo.ms_should_never_exist')") is None


@pytest.fixture
def isolation_view(mssql: Backend) -> Iterator[str]:
    """The isolation level of whichever session reads it — 5 is snapshot, 2 read committed."""
    mssql.execute(
        "CREATE OR ALTER VIEW v_ms_isolation AS "
        "SELECT CAST(transaction_isolation_level AS int) AS level FROM sys.dm_exec_sessions WHERE session_id = @@SPID"
    )
    yield "v_ms_isolation"
    mssql.drop_view("v_ms_isolation")
    mssql.execute(f"ALTER DATABASE [{parse_dsn(mssql.dsn).database}] SET ALLOW_SNAPSHOT_ISOLATION OFF")


def test_a_query_reads_a_snapshot_when_the_database_allows_one(mssql: Backend, isolation_view: str) -> None:
    def level() -> int:
        request = QueryRequest(
            query=StructuredQuery.model_validate({"view": isolation_view}),
            view_columns=("level",),
            scope=(),
            session_attributes={},
            limit=1,
        )
        return int(DIALECT.run_query(mssql.dsn, request, trusted_hosts=mssql.trusted).rows[0][0])

    database = parse_dsn(mssql.dsn).database
    mssql.execute(f"ALTER DATABASE [{database}] SET ALLOW_SNAPSHOT_ISOLATION OFF")
    assert level() == 2
    mssql.execute(f"ALTER DATABASE [{database}] SET ALLOW_SNAPSHOT_ISOLATION ON")
    assert level() == 5


def test_schema_problems_names_every_missing_view_and_column(mssql: Backend, staff: str) -> None:
    found = DIALECT.introspect(mssql.dsn, trusted_hosts=mssql.trusted)

    problems = schema_problems(
        found, {staff: ["email", "khong_co_cot"], "v_khong_ton_tai": ["id"], "ms_staff_table": []}
    )

    assert problems == [
        f"view {staff} thiếu cột khong_co_cot",
        "không có view v_khong_ton_tai",
        "ms_staff_table là bảng gốc, không phải view",
    ]
