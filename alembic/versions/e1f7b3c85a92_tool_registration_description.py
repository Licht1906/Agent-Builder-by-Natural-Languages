"""tool_registrations.description — Agent Builder's `server_description`

Revision ID: e1f7b3c85a92
Revises: d5a83c2b17e0
Create Date: 2026-08-20 23:55:00.000000

One optional line describing what a connection is for. It matters most in
`full` action mode, where a model is handed several connections' actions at
once and has to tell the ERP from the mailbox.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1f7b3c85a92"
down_revision: str | Sequence[str] | None = "d5a83c2b17e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tool_registrations", sa.Column("description", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("tool_registrations", "description")
