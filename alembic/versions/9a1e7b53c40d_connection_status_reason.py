"""connection status reason

Revision ID: 9a1e7b53c40d
Revises: 7c4f2a91d6e3
Create Date: 2026-08-13 09:00:00.000000

RAG-22 (#108): the BYODB connection form has to tell a creator *why* a
"Kiểm tra kết nối" failed in words they can act on, and `status_detail` is
not that — it quotes URLs, httpx exception text and the first 200 bytes a
server answered with. `status_reason` carries the kind of failure
(`src.services.connection_probe.ProbeFailureReason`) beside that sentence, so
the screen writes the advice instead of pattern-matching prose.

Nullable, with no backfill: an untested connection has no reason, and a row
tested before this column existed has a `status_detail` that is still true.
Re-running `/test` is what fills it in.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a1e7b53c40d"
down_revision: str | Sequence[str] | None = "7c4f2a91d6e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("external_connections", sa.Column("status_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("external_connections", "status_reason")
