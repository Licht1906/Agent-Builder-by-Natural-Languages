"""`user_connections.expires_at` — a pasted access token knows when it dies

Revision ID: 9d4f1c2b7e38
Revises: 66bab42a57ec
Create Date: 2026-08-22 13:40:00.000000

The paste path stored a Google access token and nothing about its lifetime.
Google had said `expires_in` at paste time (`tokeninfo`), the cache deadline
used it, and then the row forgot it. So a paste stayed `CONNECTED` for good:
the Connections screen said "đang nối", the MCP panel on a brand-new agent
said the same and hid the paste box behind that sentence, and the run found
out an hour later with a 401 it could only describe as "tài khoản có thể vừa
bị thu hồi".

Nullable: the sign-in path mints access tokens on demand from a refresh token
and has no expiry worth recording. No backfill — a row pasted before this
column existed has already outlived any token it was pasted with, and the
run-time check treats a pasted row without `expires_at` the way it always did
(hand the token over, let Google answer).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d4f1c2b7e38"
down_revision: str | Sequence[str] | None = "66bab42a57ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_connections", sa.Column("expires_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_connections", "expires_at")
