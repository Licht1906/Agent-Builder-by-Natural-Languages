"""UC1-32 (#144) — the real `AuditSink` (`src.policy.pep.AuditSink`): what
`execute_tool_call` (`src.compiler.tools`) calls immediately after every PEP
decision, allow/deny/require_approval alike — "sau mỗi lời gọi PEP, insert
một dòng audit_log ... không có code path nào bỏ qua ghi log" (tickets.md,
UC1-32). `schema.sql` §6's own hash-chain trigger (`audit_chain`) computes
`prev_hash`/`entry_hash` from whatever this module inserts, so this function
never supplies either column itself — a caller-given value would just be
overwritten by the trigger before the row lands.

`record_decision` takes its `psycopg.Connection` the same way
`src.agentspec.roles.provision_agent_role` takes one — audit_log is a
schema.sql-owned object, not one of `src.db.models`'s ORM tables, so it is
read and written the same explicit-connection way every other schema.sql
object in this codebase already is. `audit_sink()` is the production
`AuditSink` built on top of it: a fresh connection off `settings.database_url`
per call, the same choice `src.compiler.tools._query_view` already makes for
the ERP DSN — audit writes are not on any hot loop this platform has today,
and a fresh connection means a broken write can never be blamed on a stale one
some earlier call left half-open.
"""

import psycopg
from psycopg.types.json import Json

from src.config import get_settings
from src.policy.pep import AuditSink, AuthzRequest, Decision

_INSERT = """
INSERT INTO audit_log (agent_id, principal_id, tool, effect, reasons, action_hash, payload)
VALUES (%(agent_id)s, %(principal_id)s, %(tool)s, %(effect)s, %(reasons)s, %(action_hash)s, %(payload)s)
"""


def record_decision(conn: psycopg.Connection, request_id: str, request: AuthzRequest, decision: Decision) -> None:
    """One `audit_log` row for one PEP decision.

    `payload` carries `request_id` — `audit_log`'s own DDL has no such column
    (UC1-32's "DDL đã có sẵn trong schema.sql" rules out adding one), so this
    is what UC1-31's structured log lines join against `audit_log` by, per
    that ticket's own text — alongside `action.args`, the payload a human
    would need to see to make sense of `action_hash` on its own.
    """
    with conn.cursor() as cur:
        cur.execute(
            _INSERT,
            {
                "agent_id": request.spec.metadata.id,
                "principal_id": request.principal.employee_id,
                "tool": request.action.tool,
                "effect": decision.effect.value,
                "reasons": Json(list(decision.reasons)),
                "action_hash": request.action.hash,
                "payload": Json({"request_id": request_id, "args": dict(request.action.args)}),
            },
        )


def audit_sink() -> AuditSink:
    """The `AuditSink` every real call site wires into `ToolCallContext`/
    `CompileDependencies`/`GoldenSetRunDeps` — `get_pep_client`'s own shape:
    settings read fresh inside the returned closure, not at import time, so a
    test that repoints `DATABASE_URL` mid-run is never fighting a cached URL.
    """

    def _sink(request_id: str, request: AuthzRequest, decision: Decision) -> None:
        with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
            record_decision(conn, request_id, request, decision)

    return _sink
