"""database source write tables — what a data_write node may change

Revision ID: c9e4a7b2d6f8
Revises: f5c3a8e2b6d9
Create Date: 2026-11-10 00:00:00.000000

ADR-0094: a Hệ CSDL now carries the base tables a creator ticked for writing,
apart from the views ticked for reading, and the last probe's columns of every
base table. Both start empty: nothing becomes writable by migrating.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e4a7b2d6f8"
down_revision: str | Sequence[str] | None = "f5c3a8e2b6d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "database_sources",
        sa.Column("allowed_write_tables", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "database_sources",
        sa.Column("table_columns", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("database_sources", "table_columns")
    op.drop_column("database_sources", "allowed_write_tables")
