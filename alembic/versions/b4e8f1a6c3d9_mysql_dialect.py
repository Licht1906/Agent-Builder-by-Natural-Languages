"""MySQL dialect — a Hệ CSDL can run on MySQL

Revision ID: b4e8f1a6c3d9
Revises: a7d3e9c2b5f1
Create Date: 2026-10-26 00:00:00.000000

DS-07 / ADR-0059 §8: `databasedialect` gains `MYSQL`, in its own committed
transaction the way `a7d3e9c2b5f1` adds `SQLSERVER`.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e8f1a6c3d9"
down_revision: str | Sequence[str] | None = "a7d3e9c2b5f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE databasedialect ADD VALUE IF NOT EXISTS 'MYSQL'")


def downgrade() -> None:
    """Delete the MySQL rows, which the code before this revision cannot read; the label stays.

    Postgres has no `DROP VALUE`. The rows' DSNs are left in the secret store
    under their `dsn_secret_ref`: a downgrade is not the place to destroy
    credentials.
    """
    op.execute("DELETE FROM database_sources WHERE dialect = 'MYSQL'")
