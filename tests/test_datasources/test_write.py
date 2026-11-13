"""A `data_write`'s pieces below the runtime — ADR-0094.

The structured write the model fills in, the rules' `tables` grants and
`scope_for_write`, the statement a write compiles to, and `run_write` against
every engine (`backend`): what lands, what the asker's scope keeps out, and
the row ceiling that rolls a write back.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from src.datasources.access import (
    WRITE_DENIED,
    AccessRules,
    All,
    Deny,
    DenyCause,
    Principal,
    Where,
    access_problems,
    scope_for_write,
)
from src.datasources.dialects import DatabaseProbeError, Introspection, ProbeFailure, WriteRequest
from src.datasources.dialects.sql import write_statement
from src.datasources.write import StructuredWrite, WriteOp, describe_write, write_problems
from src.db.models import DatabaseDialect
from tests.test_datasources.backends import Backend

RULES = AccessRules.model_validate(
    {
        "identity": {
            "view": "v_staff",
            "match_by": "email",
            "attributes": {"employee_code": "emp_id", "position": "position"},
        },
        "rules": [
            {
                "position": "Nhân viên KD",
                "tables": {
                    "ar_notes": {"operations": ["insert", "update"], "where": {"sales_rep_id": "employee_code"}}
                },
            },
            {"position": "Kế toán trưởng", "tables": {"ar_notes": {"operations": ["delete"], "all_rows": True}}},
            {"position": "Trưởng nhóm KD", "views": {"v_ar": "all"}},
        ],
    }
)

REP = Principal(match_value="an@cty.vn", attributes={"employee_code": "E102", "position": "Nhân viên KD"})


def _write(**fields: Any) -> StructuredWrite:
    return StructuredWrite.model_validate(fields)


# --------------------------------------------------------------------------- #
# The structured write
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fields", "complaint"),
    [
        ({"table": "t", "operation": "insert"}, "insert cần"),
        (
            {"table": "t", "operation": "insert", "values": {"a": 1}, "where": [{"column": "id", "value": 1}]},
            "không nhận where",
        ),
        ({"table": "t", "operation": "update", "values": {"a": 1}}, "không sửa toàn bộ bảng"),
        ({"table": "t", "operation": "delete"}, "không xoá toàn bộ bảng"),
        (
            {"table": "t", "operation": "delete", "values": {"a": 1}, "where": [{"column": "id", "value": 1}]},
            "không nhận values",
        ),
        ({"table": "t", "operation": "drop"}, "operation"),
        (
            {"table": "t", "operation": "update", "values": {"a": 1}, "where": [{"column": "id", "value": [1, 2]}]},
            "value",
        ),
    ],
)
def test_a_write_holds_each_operation_to_its_own_parts(fields: dict[str, Any], complaint: str) -> None:
    with pytest.raises(ValidationError, match=complaint):
        StructuredWrite.model_validate(fields)


def test_a_write_names_only_the_nodes_tables_and_their_real_columns() -> None:
    columns = {"ar_notes": ["id", "customer_id", "sales_rep_id", "note"]}
    ok = _write(
        table="ar_notes", operation="update", values={"note": "hẹn 20/11"}, where=[{"column": "id", "value": 3}]
    )
    assert write_problems(ok, tables=["ar_notes"], columns_by_table=columns) == []

    elsewhere = _write(table="fin_invoice", operation="delete", where=[{"column": "id", "value": 3}])
    assert write_problems(elsewhere, tables=["ar_notes"], columns_by_table=columns) == [
        "bảng fin_invoice không nằm trong các bảng node này được ghi"
    ]
    guessed = _write(table="ar_notes", operation="update", values={"amount": 0}, where=[{"column": "code", "value": 3}])
    assert write_problems(guessed, tables=["ar_notes"], columns_by_table=columns) == [
        "bảng ar_notes không có cột amount",
        "bảng ar_notes không có cột code",
    ]


def test_a_write_is_described_the_way_an_approval_card_reads() -> None:
    assert describe_write(_write(table="ar_notes", operation="insert", values={"customer_id": "C1", "note": None})) == (
        "thêm một dòng vào bảng ar_notes (customer_id = C1, note = NULL)"
    )
    assert describe_write(_write(table="ar_notes", operation="delete", where=[{"column": "id", "value": 3}])) == (
        "xoá khỏi bảng ar_notes các dòng id = 3"
    )


# --------------------------------------------------------------------------- #
# The rules' `tables` grants
# --------------------------------------------------------------------------- #


def test_a_table_grant_names_its_rows_one_way_exactly() -> None:
    with pytest.raises(ValidationError, match="đúng một trong hai"):
        AccessRules.model_validate(
            {**RULES.model_dump(), "rules": [{"position": "X", "tables": {"t": {"operations": ["insert"]}}}]}
        )
    with pytest.raises(ValidationError, match="đúng một trong hai"):
        AccessRules.model_validate(
            {
                **RULES.model_dump(),
                "rules": [
                    {
                        "position": "X",
                        "tables": {"t": {"operations": ["insert"], "all_rows": True, "where": {"a": "employee_code"}}},
                    }
                ],
            }
        )


def test_a_table_grant_filters_on_a_declared_attribute_only() -> None:
    with pytest.raises(ValidationError, match="chưa khai trong identity: department"):
        AccessRules.model_validate(
            {
                **RULES.model_dump(),
                "rules": [
                    {"position": "X", "tables": {"t": {"operations": ["update"], "where": {"dept": "department"}}}}
                ],
            }
        )


def test_a_rule_must_grant_something() -> None:
    with pytest.raises(ValidationError, match="ít nhất một view hoặc một bảng"):
        AccessRules.model_validate({**RULES.model_dump(), "rules": [{"position": "X"}]})


@pytest.mark.parametrize(
    ("principal", "table", "operation", "expected"),
    [
        (REP, "ar_notes", WriteOp.INSERT, Where(pairs=(("sales_rep_id", "E102"),))),
        (REP, "ar_notes", WriteOp.UPDATE, Where(pairs=(("sales_rep_id", "E102"),))),
        (REP, "ar_notes", WriteOp.DELETE, Deny(cause=DenyCause.OPERATION_NOT_GRANTED, reason=WRITE_DENIED)),
        (REP, "fin_invoice", WriteOp.INSERT, Deny(cause=DenyCause.TABLE_NOT_GRANTED, reason=WRITE_DENIED)),
        (None, "ar_notes", WriteOp.INSERT, Deny(cause=DenyCause.UNKNOWN_ASKER, reason=WRITE_DENIED)),
        (
            Principal(match_value="x", attributes={"position": "Kế toán trưởng"}),
            "ar_notes",
            WriteOp.DELETE,
            All(),
        ),
        (
            Principal(match_value="x", attributes={"position": "Trưởng nhóm KD"}),
            "ar_notes",
            WriteOp.UPDATE,
            Deny(cause=DenyCause.TABLE_NOT_GRANTED, reason=WRITE_DENIED),
        ),
        (
            Principal(match_value="x", attributes={"position": "Nhân viên KD"}),
            "ar_notes",
            WriteOp.INSERT,
            Deny(cause=DenyCause.MISSING_ATTRIBUTE, reason=WRITE_DENIED),
        ),
        (
            Principal(match_value="x", attributes={"position": "Giám đốc"}),
            "ar_notes",
            WriteOp.INSERT,
            Deny(cause=DenyCause.NO_RULE, reason=WRITE_DENIED),
        ),
    ],
)
def test_write_scope_is_denied_unless_a_rule_grants_the_table_and_the_operation(
    principal: Principal | None, table: str, operation: WriteOp, expected: object
) -> None:
    assert scope_for_write(RULES, principal, table, operation) == expected


def test_reading_a_view_never_grants_writing() -> None:
    lead = Principal(match_value="x", attributes={"position": "Trưởng nhóm KD"})
    assert isinstance(scope_for_write(RULES, lead, "v_ar", WriteOp.UPDATE), Deny)


def test_a_table_grant_must_be_ticked_for_writing_and_have_its_columns() -> None:
    snapshot = Introspection(
        views=(),
        base_tables=("ar_notes",),
        table_columns={"ar_notes": ("id", "customer_id", "note")},
    )
    problems = access_problems(
        AccessRules.model_validate(
            {
                **RULES.model_dump(),
                "rules": [
                    {
                        "position": "Nhân viên KD",
                        "tables": {"ar_notes": {"operations": ["insert"], "where": {"sales_rep_id": "employee_code"}}},
                    }
                ],
            }
        ),
        allowed_views=[],
        snapshot=snapshot,
        allowed_tables=["ar_notes"],
    )
    assert "bảng ar_notes không có cột sales_rep_id (quy tắc của chức vụ Nhân viên KD)" in problems
    unticked = access_problems(RULES, allowed_views=[], snapshot=snapshot, allowed_tables=[])
    assert "bảng ar_notes chưa được tick cho phép ghi (quy tắc của chức vụ Nhân viên KD)" in unticked


# --------------------------------------------------------------------------- #
# The statement
# --------------------------------------------------------------------------- #


def _sql(write: StructuredWrite, scope: tuple[tuple[str, Any], ...]) -> tuple[str, dict[str, Any]]:
    request = WriteRequest(
        write=write,
        table_columns=("id", "customer_id", "sales_rep_id", "note"),
        scope=scope,
        session_attributes={},
        max_rows=5,
    )
    compiled = write_statement(request, default_schema="public").compile(dialect=postgresql.psycopg2.dialect())
    return str(compiled), compiled.params


def test_an_update_carries_the_askers_scope_beside_its_own_rows_and_binds_every_value() -> None:
    sql, params = _sql(
        _write(
            table="ar_notes",
            operation="update",
            values={"note": "'; DROP TABLE ar_notes; --"},
            where=[{"column": "id", "value": 3}],
        ),
        (("sales_rep_id", "E102"),),
    )
    assert sql.startswith("UPDATE public.ar_notes SET note=")
    assert "WHERE public.ar_notes.id = " in sql and "AND public.ar_notes.sales_rep_id = " in sql
    assert "DROP TABLE" not in sql
    assert set(params.values()) == {"'; DROP TABLE ar_notes; --", 3, "E102"}


def test_an_insert_writes_the_askers_scope_into_the_new_row() -> None:
    sql, params = _sql(
        _write(table="ar_notes", operation="insert", values={"customer_id": "C1"}), (("sales_rep_id", "E102"),)
    )
    assert sql.startswith("INSERT INTO public.ar_notes (customer_id, sales_rep_id)")
    assert set(params.values()) == {"C1", "E102"}


# --------------------------------------------------------------------------- #
# `run_write` for real — once per engine
# --------------------------------------------------------------------------- #

TABLE_COLUMNS = ("customer_id", "sales_rep_id", "note")


@pytest.fixture
def notes(backend: Backend) -> Iterator[None]:
    backend.execute("DROP TABLE IF EXISTS t_write_notes")
    backend.execute(
        "CREATE TABLE t_write_notes (customer_id varchar(20) NOT NULL, sales_rep_id varchar(20), note varchar(200))"
    )
    backend.execute(
        "INSERT INTO t_write_notes (customer_id, sales_rep_id, note) VALUES "
        "('C1', 'E102', 'a'), ('C2', 'E102', 'b'), ('C3', 'E200', 'c')"
    )
    yield
    backend.execute("DROP TABLE IF EXISTS t_write_notes")


def _run(
    backend: Backend, write: StructuredWrite, *, scope: tuple[tuple[str, Any], ...] = (), max_rows: int = 5
) -> int:
    return backend.dialect.run_write(
        backend.dsn,
        WriteRequest(
            write=write,
            table_columns=TABLE_COLUMNS,
            scope=scope,
            session_attributes={"employee_code": "E102"},
            max_rows=max_rows,
        ),
        trusted_hosts=backend.trusted,
    )


def _count(backend: Backend, where: str = "1 = 1") -> int:
    return int(backend.scalar(f"SELECT count(*) FROM t_write_notes WHERE {where}"))


@pytest.mark.usefixtures("notes")
def test_an_insert_lands_with_the_askers_scope_written_in(backend: Backend) -> None:
    write = _write(table="t_write_notes", operation="insert", values={"customer_id": "C9", "note": "moi"})
    assert _run(backend, write, scope=(("sales_rep_id", "E102"),)) == 1
    assert _count(backend, "customer_id = 'C9' AND sales_rep_id = 'E102' AND note = 'moi'") == 1


@pytest.mark.usefixtures("notes")
def test_an_update_changes_the_askers_row_and_never_one_outside_their_scope(backend: Backend) -> None:
    def update(customer: str) -> StructuredWrite:
        return _write(
            table="t_write_notes",
            operation="update",
            values={"note": "da goi"},
            where=[{"column": "customer_id", "value": customer}],
        )

    assert _run(backend, update("C1"), scope=(("sales_rep_id", "E102"),)) == 1
    assert _run(backend, update("C3"), scope=(("sales_rep_id", "E102"),)) == 0
    assert _count(backend, "note = 'da goi'") == 1
    assert _count(backend, "customer_id = 'C3' AND note = 'c'") == 1


@pytest.mark.usefixtures("notes")
def test_a_delete_removes_exactly_the_named_rows(backend: Backend) -> None:
    write = _write(table="t_write_notes", operation="delete", where=[{"column": "customer_id", "value": "C2"}])
    assert _run(backend, write, scope=(("sales_rep_id", "E102"),)) == 1
    assert _count(backend) == 2


@pytest.mark.usefixtures("notes")
def test_a_write_over_the_row_ceiling_is_rolled_back_whole(backend: Backend) -> None:
    write = _write(table="t_write_notes", operation="delete", where=[{"column": "sales_rep_id", "value": "E102"}])
    with pytest.raises(DatabaseProbeError) as caught:
        _run(backend, write, max_rows=1)
    assert caught.value.reason is ProbeFailure.TOO_MANY_ROWS
    assert _count(backend) == 3


@pytest.mark.usefixtures("notes")
def test_a_write_the_database_refuses_changes_nothing_and_says_so_in_its_own_words(backend: Backend) -> None:
    write = _write(table="t_write_notes", operation="insert", values={"customer_id": None, "note": "thieu khach"})
    with pytest.raises(DatabaseProbeError) as caught:
        _run(backend, write)
    assert caught.value.reason is ProbeFailure.WRITE_FAILED
    assert _count(backend) == 3


@pytest.mark.usefixtures("notes")
def test_the_probe_lists_each_base_tables_columns(backend: Backend) -> None:
    found = backend.dialect.introspect(backend.dsn, trusted_hosts=backend.trusted)
    assert found.table_columns["t_write_notes"] == TABLE_COLUMNS


@pytest.mark.usefixtures("notes")
def test_a_read_connection_still_cannot_write_after_a_write_ran(backend: Backend) -> None:
    """The write path lifts read-only for its own transaction only — every other door stays shut."""
    insert = _write(table="t_write_notes", operation="insert", values={"customer_id": "C9"})
    _run(backend, insert)
    with pytest.raises(Exception):  # noqa: B017, PT011 — each driver has its own error for a refused write
        with backend.dialect.connect(backend.dsn, trusted_hosts=backend.trusted) as connection:
            cursor = connection.cursor()
            cursor.execute("DELETE FROM t_write_notes")
            if backend.kind is DatabaseDialect.SQLSERVER:
                # No read-only transaction on SQL Server: the door is the rollback `connect` always ends in.
                raise RuntimeError("rolled back on exit")
    assert _count(backend) == 4
