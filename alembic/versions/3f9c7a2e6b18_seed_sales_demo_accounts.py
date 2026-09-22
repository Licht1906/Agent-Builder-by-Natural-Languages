"""seed sales_rep / sales_manager demo accounts

Revision ID: 3f9c7a2e6b18
Revises: 8b1e2f7c9a4d
Create Date: 2026-08-17 18:40:00.000000

The counterpart to `d13a1d1e5b78`, which seeds the account that *builds* a UC-1
agent. Nothing seeded the accounts that *use* one, so a fresh install could
follow the whole flow — draft, sandbox, golden set, approve, deploy — and then
have no account the deployed agent would answer: `audience.roles` names
`sales_rep`/`sales_manager`, and `authz.rego` refuses everyone else, `agent_admin`
included.

Until now those two accounts existed only where the test suite had been pointed
at a development database and left its `test_uc1_demo_rehearsal` fixtures
behind — which is not a thing a fresh install has, and not a thing anyone should
be relying on.

Insert-if-missing rather than a bare `bulk_insert`: unlike `d13a1d1e5b78`, these
emails may already exist on exactly the databases that motivated this migration.
Re-running against one of those must not raise on the unique index, and must not
silently rewrite a password somebody has already changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.auth.seed import seed_sales_demo_rows
from src.db.models import UserRole, UserStatus

# revision identifiers, used by Alembic.
revision: str = "3f9c7a2e6b18"
down_revision: str | Sequence[str] | None = "8b1e2f7c9a4d"
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
    """Native enum columns store the member *name* (`SALES_REP`, `ACTIVE`), not its
    value — the same conversion `d13a1d1e5b78` and `1135e617defc` both make.
    """
    role, status = row["role"], row["status"]
    assert isinstance(role, UserRole)
    assert isinstance(status, UserStatus)
    return {**row, "role": role.name, "status": status.name}


def upgrade() -> None:
    users = _users_table()
    connection = op.get_bind()
    for row in seed_sales_demo_rows():
        already_there = connection.execute(
            sa.select(users.c.email).where(users.c.email == row["email"])
        ).scalar_one_or_none()
        if already_there is None:
            op.bulk_insert(users, [_wire_row(row)])


def downgrade() -> None:
    users = _users_table()
    for row in seed_sales_demo_rows():
        op.execute(users.delete().where(users.c.email == row["email"]))
