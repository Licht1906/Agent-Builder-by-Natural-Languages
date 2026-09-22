"""`pm` role + the PM control room's own account (ADR-0051)

Revision ID: b3c8e47a9d15
Revises: a7e2c91f4d06
Create Date: 2026-08-21 16:30:00.000000

The PM control room shipped behind `require_admin`, which made the platform
admin and the PM the same login. They are different people: the admin
activates accounts, edits platform config and signs approval dossiers; the PM
reads the board and pulls its levers. So: one new `userrole` label, and one
seeded account holding it, `active` from the start like `d13a1d1e5b78`'s
agent_admin and `3f9c7a2e6b18`'s sales personas.

`ALTER TYPE ... ADD VALUE` runs in its own committed transaction
(`autocommit_block`) for the reason `9602748a105a` spells out: the insert a
few lines below uses the label, and Postgres refuses a label added in the
same transaction. Insert-if-missing, so re-running against a database where
someone already created `pm@example.com` neither raises nor rewrites a
password.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.auth.seed import seed_pm_row
from src.db.models import UserRole, UserStatus

# revision identifiers, used by Alembic.
revision: str = "b3c8e47a9d15"
down_revision: str | Sequence[str] | None = "a7e2c91f4d06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _users_table() -> sa.TableClause:
    return sa.table(
        "users",
        sa.column("email", sa.String()),
        sa.column("password_hash", sa.String()),
        sa.column("role", sa.String()),
        sa.column("status", sa.String()),
        sa.column("employee_id", sa.String()),
        sa.column("job_title", sa.String()),
        sa.column("department", sa.String()),
    )


def _wire_row(row: dict[str, object]) -> dict[str, object]:
    """Native enum columns store the member *name* (`PM`, `ACTIVE`), not its value."""
    role, status = row["role"], row["status"]
    assert isinstance(role, UserRole)
    assert isinstance(status, UserStatus)
    return {**row, "role": role.name, "status": status.name}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'PM'")

    users = _users_table()
    row = seed_pm_row()
    already_there = (
        op.get_bind().execute(sa.select(users.c.email).where(users.c.email == row["email"])).scalar_one_or_none()
    )
    if already_there is None:
        op.bulk_insert(users, [_wire_row(row)])


def downgrade() -> None:
    """Remove the account. The `PM` label stays: Postgres has no `DROP VALUE`, and
    an unused label is inert."""
    users = _users_table()
    op.execute(users.delete().where(users.c.email == seed_pm_row()["email"]))
