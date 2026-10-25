"""UC1-08 (#122) dry-run probe — FR-23: "test kết nối, liệt kê chính xác các
object agent sẽ chạm, đếm số dòng."

Postgres only. UC-1's own connection (`conn_erp`, `agentspec.yaml`) is the
only one this build needs to probe for real; `rest`/`smtp`
(`conn_mail`/`conn_task`) have no dry-run story here yet — the same honest gap
`src.services.connection_probe.ProbeFailureReason.PROVIDER_UNSUPPORTED`
documents for BYODB providers beyond Qdrant. `src.services.uc1_connections`
is what turns "unsupported" into a 4xx rather than a crash.

FR-24 is enforced *inside* this probe, not beside it:
`assert_allowed_objects_are_views` (UC1-09 / #123) runs before a single row is
counted, so a spec that later tries to declare a base table was already
refused the moment a creator tried to wire the connection up, not just at
compile time.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

import psycopg

from src.agentspec.spec import ConnectionType
from src.agentspec.views import DisallowedBaseTableError, ViewLister, assert_allowed_objects_are_views
from src.services.postgres_introspection import count_rows, live_view_lister

#: What this module knows how to probe today. `src.services.uc1_connections`
#: refuses everything else by name before calling in here at all.
SUPPORTED_PROBE_TYPES = frozenset({ConnectionType.POSTGRES})

#: `(dsn, object_name) -> row count`. The second test seam, alongside
#: `ViewLister` — a test scripts both without a live Postgres; production
#: always counts against the real database via `src.services.postgres_introspection.count_rows`.
RowCounter = Callable[[str, str], int]


class ProbeStatus(StrEnum):
    CONNECTED = "connected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ObjectCount:
    """One `allowed_objects` entry and how many rows it held at probe time."""

    object: str
    row_count: int


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: ProbeStatus
    detail: str
    objects: tuple[ObjectCount, ...] = ()


def probe_postgres_connection(
    *,
    dsn: str,
    allowed_objects: Sequence[str],
    list_views: ViewLister | None = None,
    row_counter: RowCounter | None = None,
) -> ProbeResult:
    """Test `dsn` for real: verify `allowed_objects` are views, then count each one's rows.

    Never raises — a bad DSN, an unreachable host, a base table where a view
    should be: every one of those comes back as `ProbeResult(status=FAILED,
    ...)`, the same "always answer, never crash" contract
    `src.services.connection_probe.probe_connection` already keeps for BYODB.
    `list_views`/`row_counter` are the test seams; production always probes
    the real database via `live_view_lister(dsn)`/`count_rows`.
    """
    lister = list_views if list_views is not None else live_view_lister(dsn)
    counter = row_counter if row_counter is not None else count_rows
    try:
        assert_allowed_objects_are_views(allowed_objects, list_views=lister)
    except DisallowedBaseTableError as exc:
        return ProbeResult(status=ProbeStatus.FAILED, detail=str(exc))
    except (psycopg.Error, OSError) as exc:
        return ProbeResult(status=ProbeStatus.FAILED, detail=f"Không kết nối được: {exc}")

    try:
        counts = tuple(ObjectCount(object=name, row_count=counter(dsn, name)) for name in allowed_objects)
    except (psycopg.Error, OSError) as exc:
        return ProbeResult(status=ProbeStatus.FAILED, detail=f"Kết nối được nhưng đếm dòng thất bại: {exc}")

    total_rows = sum(count.row_count for count in counts)
    return ProbeResult(
        status=ProbeStatus.CONNECTED,
        detail=f"Kết nối thành công — {len(counts)} view, {total_rows} dòng",
        objects=counts,
    )
