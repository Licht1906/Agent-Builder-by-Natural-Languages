"""SQL Server dialect — a Hệ CSDL can run on SQL Server

Revision ID: a7d3e9c2b5f1
Revises: f2a9c6d1e8b3
Create Date: 2026-10-26 00:00:00.000000

DS-06 / ADR-0059 §8: `databasedialect` gains `SQLSERVER`. `ALTER TYPE ... ADD
VALUE` runs in its own committed transaction (`autocommit_block`), the way
`b3c8e47a9d15` adds `PM`, so a later migration in the same run can use the
label.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d3e9c2b5f1"
down_revision: str | Sequence[str] | None = "f2a9c6d1e8b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE databasedialect ADD VALUE IF NOT EXISTS 'SQLSERVER'")


def downgrade() -> None:
    """Delete the SQL Server rows, which the code before this revision cannot read; the label stays.

    Postgres has no `DROP VALUE`. The rows' DSNs are left in the secret store
    under their `dsn_secret_ref`: a downgrade is not the place to destroy
    credentials.
    """
    op.execute("DELETE FROM database_sources WHERE dialect = 'SQLSERVER'")
