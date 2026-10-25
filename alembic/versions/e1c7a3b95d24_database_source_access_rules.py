"""database source access rules — a Hệ CSDL's `access.yaml`

Revision ID: e1c7a3b95d24
Revises: d5a2e8c41f07
Create Date: 2026-10-25 12:00:00.000000

DS-03 / ADR-0059 §6: `identity` + `rules`, stored as the model's own JSON on
the Hệ CSDL it governs. Nullable, and null means no rules — which the engine
reads as "deny everyone", so every existing row starts closed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1c7a3b95d24"
down_revision: str | Sequence[str] | None = "d5a2e8c41f07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("database_sources", sa.Column("access_rules", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("database_sources", "access_rules")
