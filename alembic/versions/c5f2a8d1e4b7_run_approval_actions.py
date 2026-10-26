"""run approval actions — an approval is of a payload

Revision ID: c5f2a8d1e4b7
Revises: b4e8f1a6c3d9
Create Date: 2026-10-26 00:00:00.000000

DS-08 / ADR-0059 §7: `run_approvals.actions` holds the calls a pause stands
in front of — `{node_id, tool, args, hash}` each — so the card shows the
payload verbatim and the action node can refuse a call whose hash differs
from the one approved. Rows from before this revision guard no payload and
get an empty list.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5f2a8d1e4b7"
down_revision: str | Sequence[str] | None = "b4e8f1a6c3d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_approvals", sa.Column("actions", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("run_approvals", "actions")
