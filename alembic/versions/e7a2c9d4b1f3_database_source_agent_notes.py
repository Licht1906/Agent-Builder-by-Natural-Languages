"""database source agent notes — the creator's description of a database for the agent

Revision ID: e7a2c9d4b1f3
Revises: d1f6b3a8c5e2
Create Date: 2026-11-11 00:00:00.000000

ADR-0095: free text read by the model that writes each query or write —
which view holds what, how tables relate, business rules. Empty for every
existing row.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a2c9d4b1f3"
down_revision: str | Sequence[str] | None = "d1f6b3a8c5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("database_sources", sa.Column("agent_notes", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("database_sources", "agent_notes")
