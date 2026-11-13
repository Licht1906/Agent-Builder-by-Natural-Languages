"""database source write dsn — an optional second account for writes

Revision ID: d1f6b3a8c5e2
Revises: c9e4a7b2d6f8
Create Date: 2026-11-11 00:00:00.000000

ADR-0094: a Hệ CSDL may name a second DSN used only by `data_write`, so the
main one can be a read-only login. Empty for every existing row: writes keep
going through the main DSN until a creator sets one.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f6b3a8c5e2"
down_revision: str | Sequence[str] | None = "c9e4a7b2d6f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("database_sources", sa.Column("write_dsn_secret_ref", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("database_sources", "write_dsn_secret_ref")
