"""uc1-01 auth foundation

Revision ID: 9602748a105a
Revises: 9a1e7b53c40d
Create Date: 2026-08-14 08:00:00.000000

UC1-01 (#117), spec.md §3/§4.1/FR-01..04: three new `userrole` labels for the
UC-1 build target (`agent_admin`, `sales_manager`, `sales_rep`, alongside the
V2 platform's existing `creator`/`admin`), plus the columns the new
registration flow needs — `status` (first account active, everyone after it
pending until an admin activates them), `employee_id` (what RLS and the JWT
key off of), `job_title`, `department`.

`ALTER TYPE ... ADD VALUE` cannot be *used* — written, compared, or inserted
— inside the transaction that adds it. Alembic runs every migration in one
`upgrade head` invocation inside a single outer transaction (see `alembic/env.py`),
not one transaction per revision, and migration `d13a1d1e5b78` (a few
revisions later in this same chain) inserts a row with `role = 'AGENT_ADMIN'`
— so without `autocommit_block()` below, that insert would hit Postgres's
"unsafe use of new value" error even though it is a later, separate migration
file. `autocommit_block()` runs the `ALTER TYPE` statements in their own,
immediately-committed transaction, so the label exists for real by the time
any later migration — in this run or a future one — needs to use it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9602748a105a"
down_revision: str | Sequence[str] | None = "9a1e7b53c40d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_ROLE_LABELS = ("AGENT_ADMIN", "SALES_MANAGER", "SALES_REP")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label in _NEW_ROLE_LABELS:
            op.execute(f"ALTER TYPE userrole ADD VALUE IF NOT EXISTS '{label}'")

    userstatus = postgresql.ENUM("ACTIVE", "PENDING", name="userstatus")
    userstatus.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "users",
        sa.Column("status", userstatus, nullable=False, server_default="ACTIVE"),
    )
    op.add_column("users", sa.Column("employee_id", sa.String(), nullable=True))
    op.add_column("users", sa.Column("job_title", sa.String(), nullable=True))
    op.add_column("users", sa.Column("department", sa.String(), nullable=True))
    op.create_unique_constraint("uq_users_employee_id", "users", ["employee_id"])


def downgrade() -> None:
    """Drop the new columns and the `userstatus` type.

    The three new `userrole` labels stay — Postgres has no `ALTER TYPE ... DROP
    VALUE`, and an unused label on an enum type is inert, not a constraint
    violation, so leaving it behind costs nothing.
    """
    op.drop_constraint("uq_users_employee_id", "users", type_="unique")
    op.drop_column("users", "department")
    op.drop_column("users", "job_title")
    op.drop_column("users", "employee_id")
    op.drop_column("users", "status")
    sa.Enum(name="userstatus").drop(op.get_bind(), checkfirst=True)
