"""eval case origin `library` — cases filled from the shared template library

Revision ID: d9a6e3b1f4c7
Revises: c4f2a9d7e1b5
Create Date: 2026-10-30 00:00:00.000000

EV-03 / ADR-0069 §1. Downgrade leaves the value in place: Postgres cannot drop
one value from an enum, and a row carrying it must still read.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9a6e3b1f4c7"
down_revision: str | Sequence[str] | None = "c4f2a9d7e1b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE evalcaseorigin ADD VALUE IF NOT EXISTS 'LIBRARY'")


def downgrade() -> None:
    pass
