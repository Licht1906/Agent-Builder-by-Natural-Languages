"""uc1 connections

Revision ID: cadb5f6f09f9
Revises: d13a1d1e5b78
Create Date: 2026-08-14 21:00:00.000000

UC1-08 (#122), spec.md §4.3/§6.2/§19: the registry `POST /connections` writes
into and `POST /connections/{id}/probe` reads back from — never the credential
itself, which lives only in Vault (`src.services.vault_secret_store`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cadb5f6f09f9"
down_revision: str | Sequence[str] | None = "d13a1d1e5b78"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `create_type=False`: the type is created explicitly below via `.create()`,
#: once. Without this, `op.create_table` compiles its own `CREATE TYPE` for
#: any inline `postgresql.ENUM` column it has not seen before and collides
#: with the explicit one (`DuplicateObject`) — the same two-step
#: `9602748a105a` uses for `userstatus`, just also needed here because
#: `create_table` (unlike `add_column`) always compiles the type inline.
_CONNECTION_TYPE = postgresql.ENUM(
    "postgres", "mysql", "mssql", "rest", "smtp", "s3", "filesystem", name="connectiontype", create_type=False
)
_PROBE_STATUS = postgresql.ENUM(
    "untested", "connected", "failed", name="uc1connectionprobestatus", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        "postgres", "mysql", "mssql", "rest", "smtp", "s3", "filesystem", name="connectiontype"
    ).create(bind, checkfirst=True)
    postgresql.ENUM("untested", "connected", "failed", name="uc1connectionprobestatus").create(
        bind, checkfirst=True
    )

    op.create_table(
        "uc1_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("type", _CONNECTION_TYPE, nullable=False),
        sa.Column("allowed_objects", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("allowed_hosts", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("vault_path", sa.String(), nullable=False),
        sa.Column("credential_ref", sa.String(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_probe_status", _PROBE_STATUS, nullable=False, server_default="untested"),
        sa.Column("last_probe_detail", sa.String(), nullable=True),
        sa.Column("last_probe_objects", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("last_probed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("uc1_connections")
    bind = op.get_bind()
    _PROBE_STATUS.drop(bind, checkfirst=True)
    _CONNECTION_TYPE.drop(bind, checkfirst=True)
