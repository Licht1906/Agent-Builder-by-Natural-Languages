"""UC1-18 (#134) — Postgres role lifecycle: deploy generates, suspend revokes (FR-34/FR-35).

`Metadata.id`'s own docstring already commits to the naming: an agent's id
"cũng là tên PostgreSQL role" (`src.agentspec.spec.Metadata`). This module is
what makes that true — one NOLOGIN role per agent, granted `SELECT` on exactly
the views its own spec's `connections[]` names, and nothing else. Two agents
with disjoint `allowed_objects` get two roles with disjoint grants by
construction: each role's grants come only from that spec's own connections,
never from a shared list either could drift into.

This is deliberately a *separate* mechanism from schema.sql §5's
`agt_ar_assistant_ro` — that role is pre-seeded by migration so the demo
agent's sandbox chat (FR-36, which runs before any version is ever approved,
let alone deployed) has something to query before this module has provisioned
anything. `src.compiler.tools`'s real ERP tool implementations query through
that fixed, always-present role; this module's dynamically-provisioned
`<metadata.id>` role is the FR-34/FR-35 lifecycle itself — the thing AC-18
tests by deploying two differently-`metadata.id`'d specs and reading back two
distinct grant sets from `information_schema`.

`NOLOGIN`, like every role this platform creates for this purpose: nobody
authenticates as `<metadata.id>` directly. The one DSN this module ever dials
is the platform's own superuser connection (`get_settings().database_url`,
the same one `scripts/verify_uc1_data_surface.py` uses to prove `SET LOCAL
ROLE` behaves correctly even from a superuser session) — provisioning is an
administrative act, not something a request-scoped credential does.
"""

import psycopg
from psycopg import sql

from src.agentspec.spec import AgentSpec, ConnectionType

#: `information_schema.role_table_grants`'s own value for a granted `SELECT` —
#: what `tests` reads back to prove a role holds exactly what this module gave it.
GRANT_SELECT = "SELECT"

#: UC1-36 (#148)'s own finding, closed here rather than filed: `v_ar_open`/
#: `v_customer_safe` are `security_invoker = true` (`schema.sql` §4), so
#: Postgres checks the *querying* role's own privileges on the tables each
#: view reads, never the view owner's — granting `SELECT` on the view alone
#: (this module's whole behavior before this fix) left every dynamically
#: provisioned `<metadata.id>` role unable to actually read a single row
#: through it, since nothing ever granted the required column-level access
#: schema.sql's own pre-seeded `agt_ar_assistant_ro` role gets in §5. Nothing
#: caught this before UC1-36's production chat door existed — sandbox chat,
#: the golden-set runner, and action-approval's second call all query
#: through that one fixed role, never through a `metadata.id` role. `None`
#: as a table's columns means the whole table (`crm_sales_assignment`, whose
#: own RLS policy is schema.sql §3's actual gate, so no column list narrows
#: it further); a tuple means exactly those columns, matching schema.sql §5's
#: grants column for column. Deliberately a closed mapping for the two views
#: §16.1 scopes this build to, not a live introspection of an arbitrary
#: view's dependencies — the same "known, narrow, honest" scope
#: `scripts/verify_uc1_data_surface.py`'s own `_EXPECTED_COLUMN_GRANTS`
#: already commits to for the same two views.
_VIEW_BASE_TABLE_GRANTS: dict[str, tuple[tuple[str, tuple[str, ...] | None], ...]] = {
    "v_ar_open": (
        ("fin_invoice", ("order_id", "customer_id", "amount_due", "amount_paid", "due_date", "settled_at")),
        ("crm_sales_assignment", None),
        ("orders", ("order_id", "order_status")),
    ),
    "v_customer_safe": (
        ("customers", ("customer_id", "customer_city", "customer_state")),
        ("crm_sales_assignment", None),
    ),
}


#: The capability-name prefix whose implementations read a view
#: (`src.compiler.tools`' `erp.query_receivables`/`erp.query_customer`; the
#: other three touch no relation). Kept as a prefix rather than a tool list
#: because the mapping this guards is domain-level — the same convention
#: `src.compiler.graph._connection_id_for` already reads a connection's id by.
_VIEW_QUERYING_TOOL_PREFIX = "erp."


class AgentRoleGrantError(RuntimeError):
    """`spec` cannot be served by the role its own `metadata.id` names.

    Either the spec never declared the connection its view-reading
    capabilities need (so `provision_agent_role` had nothing to grant), or the
    grants it did make do not in fact let the role read. Both end the same
    way if allowed through — a `deployed` agent that answers every question
    with a permission error — so both are refused here rather than discovered
    by a user.
    """


def granted_views(spec: AgentSpec) -> tuple[str, ...]:
    """Every view `provision_agent_role` will grant `spec`'s role `SELECT` on.

    The single source of truth for "what this spec's role is supposed to be
    able to read", read by the provisioning loop below and by the two checks
    beside it, so a change to which connections count cannot leave the grant
    and its verification disagreeing.
    """
    return tuple(
        view
        for connection in spec.connections
        if connection.type is ConnectionType.POSTGRES and connection.read_only
        for view in connection.allowed_objects
    )


def missing_connection_reason(spec: AgentSpec) -> str | None:
    """Why `spec` could never read anything once deployed, or `None` if it could.

    A pure check on the document, so a caller runs it *before* touching any
    state — see `src.api.uc1_approval_routes.deploy_my_agents_version`, which
    has already suspended the outgoing version by the time provisioning runs.

    Exists because a spec whose `connections` is empty is not a
    hypothetical: the UC-1 meta-agent drafted exactly that while granting
    `erp.query_receivables`, and every downstream check waved it through.
    `compile_spec` accepted it (FR-24's view allow-list is verified per
    declared connection, so zero connections means zero objections),
    `provision_agent_role`'s own grant loop iterates `spec.connections` and so
    granted nothing, and the version reached `deployed` able to do nothing but
    answer `permission denied for view v_ar_open`.
    """
    view_readers = sorted(
        capability.tool for capability in spec.capabilities if capability.tool.startswith(_VIEW_QUERYING_TOOL_PREFIX)
    )
    if not view_readers or granted_views(spec):
        return None
    return (
        f"spec cấp quyền {', '.join(view_readers)} nhưng `connections` không khai kết nối `postgres` "
        "`read_only` nào — role của agent sẽ không được GRANT SELECT trên view nào cả, và bản deploy "
        "chỉ có thể trả về `permission denied`. Thêm connection kèm `allowed_objects` trước khi deploy."
    )


def _role(name: str) -> sql.Identifier:
    """`name` as a safely-quoted SQL identifier. Every caller here passes a
    string already shaped by `Metadata.id`'s pattern or a connection's own
    `allowed_objects` entry — `sql.Identifier` quotes it regardless, so this
    module is never the place an unexpected character becomes a SQL injection.
    """
    return sql.Identifier(name)


def provision_agent_role(conn: psycopg.Connection, spec: AgentSpec) -> None:
    """`approved -> deployed` (FR-34): create `spec.metadata.id` as a role, or reset
    an existing one's grants to match `spec` exactly.

    Idempotent on purpose — `CREATE ROLE` first, `REVOKE ALL` unconditionally
    before granting again, so redeploying the same agent (a new version of an
    already-deployed one) never leaves a stale grant from an older spec
    behind. Only `postgres`-typed, `read_only` connections grant anything:
    UC-1 has no write capability at the database layer (writes go through
    `mail.send_reminder`/`task.create`'s own approval queue, never SQL), so a
    `read_only: false` connection here has nothing this module knows how to
    grant.
    """
    role = spec.metadata.id
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_name}) THEN
                        CREATE ROLE {role} NOLOGIN;
                    END IF;
                END
                $$;
                """
            ).format(role=_role(role), role_name=sql.Literal(role))
        )
        cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}").format(role=_role(role)))
        cur.execute(sql.SQL("REVOKE ALL ON SCHEMA public FROM {role}").format(role=_role(role)))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {role}").format(role=_role(role)))
        for view in granted_views(spec):
            cur.execute(sql.SQL("GRANT SELECT ON {view} TO {role}").format(view=_role(view), role=_role(role)))
            for table, columns in _VIEW_BASE_TABLE_GRANTS.get(view, ()):
                if columns is None:
                    cur.execute(
                        sql.SQL("GRANT SELECT ON {table} TO {role}").format(table=_role(table), role=_role(role))
                    )
                else:
                    cur.execute(
                        sql.SQL("GRANT SELECT ({columns}) ON {table} TO {role}").format(
                            columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
                            table=_role(table),
                            role=_role(role),
                        )
                    )
    conn.commit()
    assert_agent_role_can_read(conn, spec)


def assert_agent_role_can_read(conn: psycopg.Connection, spec: AgentSpec) -> None:
    """Prove the grants just made actually work, by reading as the role itself.

    `SET LOCAL ROLE <metadata.id>` then one `SELECT ... LIMIT 1` per granted
    view, each in its own transaction that is always rolled back — nothing
    here is allowed to outlive the check.

    Row count is deliberately not asserted, only the absence of a privilege
    error: with none of §9.2's three GUCs set, `app_scope()` coalesces to
    `'none'` and RLS correctly returns zero rows. Zero rows is the *expected*
    answer here, so treating it as failure would reject a healthy deploy.

    This exists because `_VIEW_BASE_TABLE_GRANTS` above documents this repo
    getting caught by this once already — `security_invoker` views need
    base-table grants, nothing had them, and the note records that "nothing
    caught this before UC1-36's production chat door existed", because every
    pre-deploy caller queries through the shared `agt_ar_assistant_ro` role
    instead. That is still true: sandbox chat and the golden-set runner both
    exercise a role that is *not* the one production uses, so no amount of
    rehearsal can tell an admin whether the deployed role can read. This can,
    and it runs on the one transition that creates the role.
    """
    role = spec.metadata.id
    for view in granted_views(spec):
        try:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SET LOCAL ROLE {role}").format(role=_role(role)))
                cur.execute(sql.SQL("SELECT 1 FROM {view} LIMIT 1").format(view=_role(view)))
                cur.fetchall()
        except psycopg.errors.InsufficientPrivilege as exc:
            raise AgentRoleGrantError(
                f"role {role!r} vẫn không đọc được view {view!r} sau khi provision: {exc}"
            ) from None
        finally:
            # Always, including the success path: `SET LOCAL ROLE` must not
            # survive into whatever the caller does with this connection next.
            conn.rollback()


def suspend_agent_role(conn: psycopg.Connection, agent_id: str) -> None:
    """`deployed -> suspended` (FR-35): revoke every privilege `agent_id`'s role holds.

    Two `REVOKE`s, one round trip each, then `commit` — the whole point being
    that the next request to use this role (`SET LOCAL ROLE <agent_id>`) fails
    with a permission error the instant this transaction lands, which is what
    lets AC-18's "< 1 giây đo bằng đồng hồ thực" hold: there is no cache to
    invalidate and nothing else to wait for. The role itself is left in place
    (not dropped) — a suspended agent may still be inspected/audited by id,
    and dropping a role a running transaction still references would error.
    """
    role = agent_id
    with conn.cursor() as cur:
        cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}").format(role=_role(role)))
        cur.execute(sql.SQL("REVOKE ALL ON SCHEMA public FROM {role}").format(role=_role(role)))
    conn.commit()
