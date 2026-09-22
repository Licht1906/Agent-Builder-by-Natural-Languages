"""seed agent_admin

Revision ID: d13a1d1e5b78
Revises: a4c2c2363959
Create Date: 2026-08-14 10:00:00.000000

UC1-01 (#117), FR-03: "tài khoản đầu tiên được seed làm agent_admin khi cài
đặt" — seeded here by migration, the same way `1135e617defc` (issue #4)
already seeds the V2 `creator`/`admin` pair, rather than decided at runtime
by whichever request happens to hit `POST /auth/register` first. See
`src.auth.registration`'s module docstring for why a runtime "am I the first
account" check doesn't work: this repo's own migration chain already seeds
two V2 accounts before this one runs, so an empty-table check could never
truthfully fire against a real deployment.

Runs after `9602748a105a` (adds `status`/`employee_id`/`job_title`/`department`)
so this row can carry all four from its very first insert, unlike the V2 pair.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.auth.seed import seed_agent_admin_row
from src.db.models import UserRole, UserStatus

# revision identifiers, used by Alembic.
revision: str = "d13a1d1e5b78"
down_revision: str | Sequence[str] | None = "a4c2c2363959"
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


def upgrade() -> None:
    row = seed_agent_admin_row()
    role, status = row["role"], row["status"]
    assert isinstance(role, UserRole)
    assert isinstance(status, UserStatus)
    # Native enum columns store the member *name* (`AGENT_ADMIN`, `ACTIVE`),
    # not its value — the same reason `1135e617defc` sends `row['role'].name`.
    op.bulk_insert(_users_table(), [{**row, "role": role.name, "status": status.name}])


def downgrade() -> None:
    row = seed_agent_admin_row()
    users = _users_table()
    op.execute(users.delete().where(users.c.email == row["email"]))
