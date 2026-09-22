"""user_connections.token_kind — the pasted-token path beside the sign-in one

Revision ID: f2c9d4e71b83
Revises: e1f7b3c85a92
Create Date: 2026-08-21 09:45:00.000000

Two ways to hand this platform a Google credential, and they last different
lengths of time: a refresh token (sign in once, minted forever) or a pasted
access token (dies in an hour, paste again). The column records which, because
the two are indistinguishable strings and guessing wrong produces either a
refresh call against an access token or a dead connection nobody can explain.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2c9d4e71b83"
down_revision: str | Sequence[str] | None = "e1f7b3c85a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `refresh_token` for every existing row: they were all created by the
    # OAuth flow, which is the only path that existed until now.
    op.add_column(
        "user_connections",
        sa.Column("token_kind", sa.String(), nullable=False, server_default="refresh_token"),
    )


def downgrade() -> None:
    op.drop_column("user_connections", "token_kind")
