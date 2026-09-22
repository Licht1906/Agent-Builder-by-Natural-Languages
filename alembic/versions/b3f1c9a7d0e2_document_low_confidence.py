"""document low_confidence

Revision ID: b3f1c9a7d0e2
Revises: 9dbf9accf508
Create Date: 2026-08-13 00:00:00.000000

RAG-12 (#98): `ExtractionResult.flagged_for_review` (RAG-06) already exists at
extraction time but was never carried onto the `Document` row, so the upload
screen had no way to show "some of this document was hard to read" without
re-running extraction. `server_default="false"` backfills every existing row
to the same "not flagged" state a fresh extraction would report absent
evidence otherwise — nothing here re-extracts anything.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f1c9a7d0e2"
down_revision: str | Sequence[str] | None = "9dbf9accf508"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("low_confidence", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("documents", "low_confidence")
