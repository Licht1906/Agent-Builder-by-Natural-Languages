"""user connections — per-user OAuth, the half ADR-0023 §8 deferred

Revision ID: b8e4f0a2c9d1
Revises: a7f31c9e60b4
Create Date: 2026-08-20 20:30:00.000000

One table: `(user_id, provider)` → where the refresh token lives in the secret
store. The built-in Gmail/Drive connections stop refusing with "cần một
UserConnection" the moment a row exists for the agent's owner — this is that
row. Nothing here holds a token: `secret_ref` is a key into `secrets_kv`,
exactly the discipline `external_connections.secret_ref` established.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e4f0a2c9d1"
down_revision: str | Sequence[str] | None = "a7f31c9e60b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `UserConnectionStatus`, spelled as SQLAlchemy spells a `StrEnum` member — by
#: name, which is what the ORM column stores.
_STATUS = sa.Enum("CONNECTED", "NEEDS_RECONNECT", name="userconnectionstatus")


def upgrade() -> None:
    op.create_table(
        "user_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("account_email", sa.String(), nullable=False, server_default=""),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("secret_ref", sa.String(), nullable=False),
        sa.Column("status", _STATUS, nullable=False, server_default="CONNECTED"),
        sa.Column("connected_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "provider", name="uq_user_connections_owner_provider"),
    )
    op.create_index("ix_user_connections_user_id", "user_connections", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_connections_user_id", table_name="user_connections")
    op.drop_table("user_connections")
    _STATUS.drop(op.get_bind(), checkfirst=True)
