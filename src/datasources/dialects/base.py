"""What every dialect promises, and the parts of a probe that do not depend on the engine.

**A failure is a reason, never a driver's sentence.** A driver's error text
can carry the very thing DS-02 says must never surface — libpq echoes pieces
of a malformed connection string back, and a server's `FATAL` names the user.
So a dialect turns every failure into a `ProbeFailure` and `str()` of the
error is this module's fixed sentence for it, the same "keyed by reason, not
by prose" rule `ExternalConnectionResponse.status_reason` follows for BYODB.

**The schema check reads a probe, not a database.** `schema_problems` is pure:
it compares one `Introspection` with what a caller expects to find, so the
spec screen, the `data_query` validator (DS-04) and the exported package's
start-up check (DS-15) ask one question one way.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ProbeFailure(StrEnum):
    """Why a probe of a Hệ CSDL did not get as far as listing its views."""

    INVALID_DSN = "invalid_dsn"
    DSN_OPTION_REFUSED = "dsn_option_refused"
    BLOCKED_ADDRESS = "blocked_address"
    UNREACHABLE = "unreachable"
    AUTH_REJECTED = "auth_rejected"
    DATABASE_NOT_FOUND = "database_not_found"
    PERMISSION_DENIED = "permission_denied"
    SERVER_ERROR = "server_error"
    DSN_MISSING = "dsn_missing"
    DSN_UNREADABLE = "dsn_unreadable"


#: The one sentence each failure is said in — the whole of what a probe ever
#: reports about itself. None of them quotes the DSN, the host or the user.
FAILURE_DETAIL: dict[ProbeFailure, str] = {
    ProbeFailure.INVALID_DSN: "DSN không đúng định dạng — kiểm tra lại chuỗi kết nối",
    ProbeFailure.DSN_OPTION_REFUSED: (
        "DSN có tham số không được hỗ trợ — chỉ dùng host, port, dbname, user, password, sslmode, application_name"
    ),
    ProbeFailure.BLOCKED_ADDRESS: (
        "Địa chỉ này là địa chỉ nội bộ và chưa được quản trị viên cho phép — "
        "nhờ quản trị viên thêm host vào database_source_trusted_hosts"
    ),
    ProbeFailure.UNREACHABLE: "Không kết nối được tới máy chủ CSDL trong 5 giây — kiểm tra host, cổng và tường lửa",
    ProbeFailure.AUTH_REJECTED: "CSDL từ chối tên đăng nhập hoặc mật khẩu trong DSN",
    ProbeFailure.DATABASE_NOT_FOUND: "Máy chủ không có database mà DSN chỉ tới",
    ProbeFailure.PERMISSION_DENIED: "Tài khoản trong DSN không có quyền đọc các view này",
    ProbeFailure.SERVER_ERROR: "CSDL trả về lỗi khi đọc danh sách view — thử lại sau",
    ProbeFailure.DSN_MISSING: "Chưa có DSN cho Hệ CSDL này — nhập lại DSN",
    ProbeFailure.DSN_UNREADABLE: "Không giải mã được DSN đã lưu — nhập lại DSN",
}


class DatabaseProbeError(Exception):
    """A probe that failed, by reason. `str()` is `FAILURE_DETAIL[reason]` and nothing else."""

    def __init__(self, reason: ProbeFailure) -> None:
        self.reason = reason
        super().__init__(FAILURE_DETAIL[reason])


@dataclass(frozen=True, slots=True)
class ViewInfo:
    """One view a probe found: what it is called, what it has, how many rows it held then.

    `row_count` is `None` for a view the probe could not count in time or was
    not allowed to read — the view is still listed, because it is still there.
    """

    name: str
    columns: tuple[str, ...]
    row_count: int | None


@dataclass(frozen=True, slots=True)
class Introspection:
    """What one probe found. `base_tables` is there so a table can be refused *as* a table."""

    views: tuple[ViewInfo, ...]
    base_tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """One column of one view, as a creator names them (`view` schema-qualified outside `public`)."""

    view: str
    column: str


#: `(match, value, columns) -> {column: value}` for the one row of `match.view`
#: whose `match.column` equals `value`, or `None` for no row or several — the
#: access engine's single read (`src.datasources.access.resolve_principal`).
IdentityLookup = Callable[[ColumnRef, Any, tuple[str, ...]], Mapping[str, Any] | None]


class Dialect(Protocol):
    """One database engine, as far as a Hệ CSDL needs it today.

    Compiling a structured query and setting the asker's session attributes
    join this interface with `data_query` (DS-04), the first caller that
    needs either — defining them before there is a query type to compile
    would be guessing at its shape.

    Every method takes the DSN and the trusted hosts and dials through the
    same checks; none keeps a connection past its own call.
    """

    def introspect(self, dsn: str, *, trusted_hosts: frozenset[str]) -> Introspection:
        """Every view with its columns and row count, plus the base tables. Raises `DatabaseProbeError`."""
        ...

    def identity_lookup(self, dsn: str, *, trusted_hosts: frozenset[str]) -> IdentityLookup:
        """An `IdentityLookup` against this database — each call one short read-only connection."""
        ...

    def distinct_values(self, dsn: str, column: ColumnRef, *, trusted_hosts: frozenset[str]) -> list[Any]:
        """The distinct non-null values of `column`, sorted — the spec form's list of positions. Capped."""
        ...


def schema_problems(found: Introspection, expected: Mapping[str, Sequence[str]]) -> list[str]:
    """One sentence per view or column in `expected` that `found` does not have, in `expected`'s order.

    A base table named where a view belongs is its own problem rather than
    "no such view": the creator did name something real, it is just the one
    kind of object ADR-0059 never lets an agent read (UC1-09's rule).
    """
    views = {view.name: view for view in found.views}
    tables = set(found.base_tables)
    problems: list[str] = []
    for name, columns in expected.items():
        view = views.get(name)
        if view is None:
            problems.append(f"{name} là bảng gốc, không phải view" if name in tables else f"không có view {name}")
            continue
        problems.extend(f"view {name} thiếu cột {column}" for column in columns if column not in view.columns)
    return problems
