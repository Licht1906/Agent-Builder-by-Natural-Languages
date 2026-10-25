"""database sources — a creator's Hệ CSDL

Revision ID: d5a2e8c41f07
Revises: b6f1e9a37c04
Create Date: 2026-10-25 00:00:00.000000

DS-02 / ADR-0059 §2: `database_sources` absorbs UC-1's `uc1_connections` as
the V2 data source of kind `database`. The DSN is never a column — it lives in
`secret_store_entries` under `dsn_secret_ref`.

Every `postgres` row of `uc1_connections` is **copied** across as `NEEDS_DSN`
with an empty `dsn_secret_ref`: its credential sat in a dev-mode Vault that
keeps nothing across a restart, so there is nothing to carry, and a creator
re-enters the DSN on the Hệ CSDL tab. `uc1_connections` itself is left alone
— removing the UC-1 pipeline is DS-21's job, not a side effect of this one.
Other connection types (`rest`, `smtp`, …) are not databases and stay behind.

`downgrade` drops the table; the rows it copied are still in `uc1_connections`.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5a2e8c41f07"
down_revision: str | Sequence[str] | None = "b6f1e9a37c04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIALECT = sa.Enum("POSTGRESQL", name="databasedialect")
_STATUS = sa.Enum("UNTESTED", "CONNECTED", "FAILED", "NEEDS_DSN", name="databasesourcestatus")


def upgrade() -> None:
    op.create_table(
        "database_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("dialect", _DIALECT, nullable=False),
        sa.Column("dsn_secret_ref", sa.String(), nullable=False),
        sa.Column("allowed_views", sa.JSON(), nullable=False),
        sa.Column("view_columns", sa.JSON(), nullable=False),
        sa.Column("view_row_counts", sa.JSON(), nullable=False),
        sa.Column("base_tables", sa.JSON(), nullable=False),
        sa.Column("last_probe_status", _STATUS, nullable=False),
        sa.Column("last_probe_reason", sa.String(), nullable=True),
        sa.Column("last_probe_detail", sa.String(), nullable=True),
        sa.Column("last_probed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_database_sources_owner_id", "database_sources", ["owner_id"])

    op.execute(
        """
        INSERT INTO database_sources (
            owner_id, name, dialect, dsn_secret_ref, allowed_views, view_columns, view_row_counts, base_tables,
            last_probe_status, last_probe_reason, last_probe_detail, created_at
        )
        SELECT created_by_id, name, 'POSTGRESQL', '', allowed_objects, '{}', '{}', '[]',
               'NEEDS_DSN', 'dsn_missing',
               'Chuyển từ kết nối UC-1 — DSN cũ nằm ở Vault dev-mode nên không chuyển được; nhập lại DSN',
               created_at
        FROM uc1_connections
        WHERE type = 'postgres'
        ORDER BY id
        """
    )


def downgrade() -> None:
    op.drop_index("ix_database_sources_owner_id", table_name="database_sources")
    op.drop_table("database_sources")
    _STATUS.drop(op.get_bind(), checkfirst=True)
    _DIALECT.drop(op.get_bind(), checkfirst=True)
