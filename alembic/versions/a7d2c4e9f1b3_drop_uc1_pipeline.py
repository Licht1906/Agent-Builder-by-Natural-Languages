"""drop the parallel UC-1 pipeline's tables, suites and audience roles (DS-21)

Revision ID: a7d2c4e9f1b3
Revises: e7b4c1d8a2f6
Create Date: 2026-10-28 18:00:00.000000

ADR-0059 §11: UC-1's capabilities now live in V2 — a Hệ CSDL with quy tắc
truy cập (DS-02/03), `data_query` (DS-04), `action_hash` (DS-08), the audit
chain (DS-10) — so the parallel pipeline goes, and with it what only it wrote:

- the `uc1_*` tables (`uc1_connections` was carried into `database_sources`
  by `d5a2e8c41f07`, DS-02) and their enum types;
- the eval suites keyed by a UC-1 agent id (`eval_suites.uc1_agent_id`,
  ADR-0054 §6) — their cases and runs go with them (`ON DELETE CASCADE`);
- the `sales_manager`/`sales_rep` roles: every such account becomes a
  `creator`, and the labels leave the `userrole` type. Who may see which rows
  is a Hệ CSDL's rules now, not a platform role.

`agent_runs` rows written by UC-1 (`pipeline='uc1'`) stay: they are the PM's
history, and `RunPipeline.UC1` still reads them.

**One way.** What is dropped here is data, not only shape; going back means a
backup taken before this revision, not a downgrade.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d2c4e9f1b3"
down_revision: str | Sequence[str] | None = "e7b4c1d8a2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Children before parents: `uc1_pending_actions` and the run/approval tables
#: name a spec version by `(agent_id, version)` only, but drop order is kept
#: safe for whichever foreign keys a deployment added by hand.
_UC1_TABLES = (
    "uc1_pending_actions",
    "uc1_version_approvals",
    "uc1_eval_runs",
    "uc1_sandbox_runs",
    "uc1_agent_spec_versions",
    "uc1_connections",
)
_UC1_TYPES = ("uc1pendingactionstatus", "uc1sandboxrunstatus", "uc1connectionprobestatus")

#: `userrole` without the two UC-1 audience labels — native enum labels are member *names*.
_ROLES_KEPT = ("CREATOR", "ADMIN", "AGENT_ADMIN", "PM")


def upgrade() -> None:
    for table in _UC1_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for type_name in _UC1_TYPES:
        op.execute(f"DROP TYPE IF EXISTS {type_name}")

    op.execute("DELETE FROM eval_suites WHERE uc1_agent_id IS NOT NULL")
    op.drop_constraint("ck_eval_suites_exactly_one_agent", "eval_suites", type_="check")
    op.drop_constraint("uq_eval_suites_uc1_agent_id", "eval_suites", type_="unique")
    op.drop_column("eval_suites", "uc1_agent_id")
    op.alter_column("eval_suites", "agent_id", nullable=False)

    op.execute("UPDATE users SET role = 'CREATOR' WHERE role::text IN ('SALES_MANAGER', 'SALES_REP')")
    op.execute("ALTER TYPE userrole RENAME TO userrole_with_uc1")
    labels = ", ".join(f"'{label}'" for label in _ROLES_KEPT)
    op.execute(f"CREATE TYPE userrole AS ENUM ({labels})")
    op.execute("ALTER TABLE users ALTER COLUMN role TYPE userrole USING role::text::userrole")
    op.execute("DROP TYPE userrole_with_uc1")


def downgrade() -> None:
    raise RuntimeError(
        "a7d2c4e9f1b3 xoá dữ liệu của pipeline UC-1 — không downgrade được; khôi phục từ bản sao lưu trước revision này"
    )
