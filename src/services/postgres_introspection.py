"""The one real `ViewLister` (`src.agentspec.views`) — a live `information_schema.views` query.

Separate from `src.db.base`'s engine on purpose: that engine opens *this
platform's own* database. A UC-1 connection's DSN names somebody else's
Postgres — the customer's ERP — reached only here and only ever read-only, a
`SELECT` against `information_schema`, never the tables `allowed_objects`
itself names.

`psycopg` (v3), not `psycopg2`: this module has nothing to do with SQLAlchemy's
own engine, and psycopg3's `connect(..., connect_timeout=...)` is the same
short-timeout dial-out shape `src.services.connection_probe` already asks of a
BYODB vector store — a probe against a wrong host must fail fast, not hang the
request.
"""

from collections.abc import Sequence

import psycopg

from src.agentspec.views import ViewLister

#: Short on purpose (§4.3's own "timeout ngắn"). A dry-run probe is a person
#: watching a spinner, not a batch job — five seconds of "is anything even
#: listening" is already generous.
_CONNECT_TIMEOUT_SECONDS = 5


def live_view_lister(dsn: str) -> ViewLister:
    """A `ViewLister` bound to `dsn` — one short-lived connection per call, never pooled.

    Not pooled because a probe/compile check happens once per action a
    creator takes (wiring a connection, compiling a version), not per request
    in a hot path — the cost of `psycopg.connect` is not the risk here, a
    lingering connection to a customer's ERP with credentials this process
    should hold as briefly as possible is.
    """

    def _list(schema: str) -> Sequence[str]:
        with (
            psycopg.connect(dsn, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT table_name FROM information_schema.views WHERE table_schema = %s",
                (schema,),
            )
            return [row[0] for row in cur.fetchall()]

    return _list


def count_rows(dsn: str, object_name: str, *, schema: str = "public") -> int:
    """`SELECT count(*)` against `schema.object_name` — FR-23's "đếm số dòng".

    `object_name` is never interpolated from anything but a value this
    caller already checked against `information_schema.views`
    (`assert_allowed_objects_are_views`) — a name arriving here unchecked
    would make this a SQL-injection surface, so every caller is required to
    check first, not this function's job to check again.
    """
    quoted = f'"{schema}"."{object_name}"'
    with (
        psycopg.connect(dsn, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(f"SELECT count(*) FROM {quoted}")  # noqa: S608 — object_name pre-validated by caller
        (n,) = cur.fetchone()  # type: ignore[misc]
        return int(n)
