"""The MySQL dialect — DS-07 / ADR-0059 §8.

Two halves, as for SQL Server. The DSN half needs no server: which URLs are
dialled at all, and which are refused before anything leaves the process.
The rest runs against the MySQL `TEST_MYSQL_URL` names (CI starts one) and
is skipped, by name, without it — the probe, failure and identity checks
every dialect is held to, plus MySQL's own read-only session and TLS modes.
"""

import time
from collections.abc import Iterator
from dataclasses import replace
from urllib.parse import quote

import pymysql
import pytest

from src.datasources.dialects import ColumnRef, DatabaseProbeError, IdentitySearch, ProbeFailure, schema_problems
from src.datasources.dialects.mysql import MySqlDialect, MySqlTarget, parse_dsn
from tests.test_datasources.backends import MYSQL_URL_VARIABLE, Backend, mysql_backend

DIALECT = MySqlDialect()

# --- The DSN, before anything is dialled ------------------------------------


@pytest.mark.parametrize(
    "dsn",
    [
        "mysql://ro:p%40ss@erp.example.com:3307/erp?ssl-mode=REQUIRED",
        "mysql+pymysql://ro:p%40ss@erp.example.com:3307/erp?ssl_mode=required",
        "jdbc:mysql://ro:p%40ss@erp.example.com:3307/erp?sslMode=REQUIRED",
    ],
)
def test_every_spelling_of_the_url_names_the_same_target(dsn: str) -> None:
    assert parse_dsn(dsn) == MySqlTarget(
        host="erp.example.com", port=3307, database="erp", user="ro", password="p@ss", ssl_mode="required"
    )


def test_tls_is_preferred_unless_the_dsn_says_otherwise() -> None:
    target = parse_dsn("mysql://ro:x@erp.example.com/erp")

    assert (target.port, target.ssl_mode) == (3306, "preferred")


@pytest.mark.parametrize(
    "option",
    ["unix_socket=/var/run/mysqld/mysqld.sock", "local_infile=1", "read_default_file=/etc/my.cnf",
     "init_command=SET GLOBAL x=1", "ssl-mode=VERIFY_IDENTITY"],
)  # fmt: skip
def test_an_option_that_reaches_past_the_dsn_is_refused(option: str) -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(f"mysql://ro:x@erp.example.com/erp?{option}", trusted_hosts=frozenset())

    assert caught.value.reason is ProbeFailure.DSN_OPTION_REFUSED


@pytest.mark.parametrize(
    "dsn",
    [
        "mysql://erp.example.com/erp",  # no user: PyMySQL would sign in as whoever runs this process
        "mysql://ro:x@erp.example.com",  # no database: no views to list
        "postgresql://ro:x@erp.example.com/erp",
        "mysql://ro:bi-mat-4471@erp.example.com:99999/erp",
    ],
)
def test_a_dsn_this_dialect_cannot_dial_is_refused_without_repeating_it(dsn: str) -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(dsn, trusted_hosts=frozenset({"erp.example.com"}))

    assert caught.value.reason is ProbeFailure.INVALID_DSN
    assert "bi-mat-4471" not in str(caught.value)


def test_an_internal_host_nobody_trusted_is_never_dialled() -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect("mysql://ro:x@127.0.0.1:3306/erp", trusted_hosts=frozenset())

    assert caught.value.reason is ProbeFailure.BLOCKED_ADDRESS


# --- Against a real MySQL ---------------------------------------------------


@pytest.fixture
def server() -> Backend:
    found = mysql_backend()
    if found is None:
        pytest.skip(f"{MYSQL_URL_VARIABLE} is not set — no MySQL to run against")
    return found


def _dsn(backend: Backend, **overrides: object) -> str:
    """The test server's DSN with any part of the target replaced."""
    target = replace(parse_dsn(backend.dsn), **overrides)  # type: ignore[arg-type]
    credentials = f"{quote(target.user, safe='')}:{quote(target.password, safe='')}"
    return f"mysql://{credentials}@{target.host}:{target.port}/{target.database}?ssl-mode={target.ssl_mode}"


@pytest.fixture
def staff(server: Backend) -> Iterator[str]:
    """An employee view, and a base table beside it — what a creator's staging ERP holds."""
    server.execute("CREATE TABLE IF NOT EXISTS my_staff_table (id int)")
    server.create_values_view(
        "v_my_staff",
        ("email", "emp_id", "dept_code", "position"),
        [
            ("an@cty.vn", "E102", "SALES", "Nhân viên KD"),
            ("binh@cty.vn", "E200", "SALES", "Trưởng nhóm KD"),
            ("chi@cty.vn", "E300", "FIN", "Kế toán trưởng"),
            ("trung@cty.vn", "E401", "SALES", "Nhân viên KD"),
            ("trung@cty.vn", "E402", "SALES", "Nhân viên KD"),
        ],
    )
    yield "v_my_staff"
    server.drop_view("v_my_staff")
    server.execute("DROP TABLE IF EXISTS my_staff_table")


def test_introspection_lists_the_views_with_their_columns_and_row_counts(server: Backend, staff: str) -> None:
    found = DIALECT.introspect(server.dsn, trusted_hosts=server.trusted)

    views = {view.name: view for view in found.views}
    assert views[staff].columns == ("email", "emp_id", "dept_code", "position")
    assert views[staff].row_count == 5


def test_introspection_names_base_tables_apart_from_views(server: Backend, staff: str) -> None:
    found = DIALECT.introspect(server.dsn, trusted_hosts=server.trusted)

    assert "my_staff_table" in found.base_tables
    assert "my_staff_table" not in {view.name for view in found.views}


def test_a_wrong_password_is_named_as_such_and_the_password_is_never_echoed(server: Backend) -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(_dsn(server, password="sai-mat-khau-9137"), trusted_hosts=server.trusted)

    assert caught.value.reason is ProbeFailure.AUTH_REJECTED
    assert "sai-mat-khau-9137" not in str(caught.value)


def test_a_database_that_does_not_exist_is_named_as_such(server: Backend) -> None:
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(_dsn(server, database="khong_co_db_nay"), trusted_hosts=server.trusted)

    assert caught.value.reason is ProbeFailure.DATABASE_NOT_FOUND


def test_nothing_listening_fails_within_five_seconds(server: Backend) -> None:
    started = time.monotonic()
    with pytest.raises(DatabaseProbeError) as caught:
        DIALECT.introspect(_dsn(server, port=1), trusted_hosts=server.trusted)

    assert caught.value.reason is ProbeFailure.UNREACHABLE
    assert time.monotonic() - started <= 5.5


@pytest.fixture
def slow_view(server: Backend) -> Iterator[str]:
    """A view whose `count(*)` takes longer than a probe waits for one view — named to be counted first."""
    server.execute("CREATE OR REPLACE VIEW v_my_a_slow AS SELECT 1 AS x FROM DUAL WHERE SLEEP(3) = 0")
    yield "v_my_a_slow"
    server.drop_view("v_my_a_slow")


def test_a_view_too_slow_to_count_is_listed_without_a_count_and_the_rest_still_are(
    server: Backend, staff: str, slow_view: str
) -> None:
    found = DIALECT.introspect(server.dsn, trusted_hosts=server.trusted)

    views = {view.name: view for view in found.views}
    assert views[slow_view].row_count is None
    assert views[staff].row_count == 5


def test_the_identity_lookup_reads_one_askers_row(server: Backend, staff: str) -> None:
    lookup = DIALECT.identity_lookup(server.dsn, trusted_hosts=server.trusted)

    row = lookup(ColumnRef(view=staff, column="email"), "an@cty.vn", ("emp_id", "dept_code", "position"))

    assert row == {"emp_id": "E102", "dept_code": "SALES", "position": "Nhân viên KD"}


def test_the_identity_lookup_finds_nobody_for_an_unknown_or_ambiguous_value(server: Backend, staff: str) -> None:
    lookup = DIALECT.identity_lookup(server.dsn, trusted_hosts=server.trusted)
    email = ColumnRef(view=staff, column="email")

    assert lookup(email, "la@cty.vn", ("emp_id",)) is None
    assert lookup(email, "trung@cty.vn", ("emp_id",)) is None
    assert lookup(email, "x' OR '1'='1", ("emp_id",)) is None


def test_distinct_values_lists_every_position_once(server: Backend, staff: str) -> None:
    values = DIALECT.distinct_values(server.dsn, ColumnRef(view=staff, column="position"), trusted_hosts=server.trusted)

    assert values == ["Kế toán trưởng", "Nhân viên KD", "Trưởng nhóm KD"]


@pytest.mark.parametrize(("text", "expected"), [("AN@", [("an@cty.vn", "Nhân viên KD")]), ("%", []), ("_", [])])
def test_identity_candidates_match_text_only_as_text(
    server: Backend, staff: str, text: str, expected: list[tuple[str, str]]
) -> None:
    """Case-insensitive, and `%` or `_` are characters to find — not patterns that match everyone."""
    search = IdentitySearch(match=ColumnRef(view=staff, column="email"), label_column="position", text=text, limit=10)

    assert DIALECT.identity_candidates(server.dsn, search, trusted_hosts=server.trusted) == expected


@pytest.mark.parametrize("statement", ["INSERT INTO my_staff_table VALUES (1)", "CREATE TABLE my_never (id int)"])
def test_every_session_is_read_only(server: Backend, staff: str, statement: str) -> None:
    with (
        DIALECT.connect(server.dsn, trusted_hosts=server.trusted) as connection,
        pytest.raises(pymysql.Error) as caught,
    ):
        connection.cursor().execute(statement)

    assert caught.value.args[0] == 1792  # "Cannot execute statement in a READ ONLY transaction"
    assert server.scalar("SELECT COUNT(*) FROM my_staff_table") == 0


@pytest.mark.parametrize(("mode", "encrypted"), [("required", True), ("disabled", False)])
def test_the_ssl_mode_is_what_the_session_gets(server: Backend, mode: str, encrypted: bool) -> None:
    with DIALECT.connect(_dsn(server, ssl_mode=mode), trusted_hosts=server.trusted) as connection:
        cursor = connection.cursor()
        cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        assert bool(cursor.fetchone()[1]) is encrypted


def test_schema_problems_names_every_missing_view_and_column(server: Backend, staff: str) -> None:
    found = DIALECT.introspect(server.dsn, trusted_hosts=server.trusted)

    problems = schema_problems(
        found, {staff: ["email", "khong_co_cot"], "v_khong_ton_tai": ["id"], "my_staff_table": []}
    )

    assert problems == [
        f"view {staff} thiếu cột khong_co_cot",
        "không có view v_khong_ton_tai",
        "my_staff_table là bảng gốc, không phải view",
    ]
