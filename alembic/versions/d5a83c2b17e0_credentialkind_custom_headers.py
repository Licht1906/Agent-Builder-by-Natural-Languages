"""credentialkind gains CUSTOM_HEADERS — Agent Builder's third auth choice

Revision ID: d5a83c2b17e0
Revises: c4d7e1f9a2b5
Create Date: 2026-08-20 23:35:00.000000

The add-server dialog this platform is modelled on offers exactly three ways to
authenticate: none, a bearer token, custom headers. The first two were already
`credentialkind` members; this is the third. Values live in the secret store
like every other credential — the enum only records the *shape*.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5a83c2b17e0"
down_revision: str | Sequence[str] | None = "c4d7e1f9a2b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `IF NOT EXISTS` so a database that already gained the value (a developer
    # who ran this by hand while the feature was in flight) upgrades cleanly
    # rather than failing on a duplicate.
    op.execute("ALTER TYPE credentialkind ADD VALUE IF NOT EXISTS 'CUSTOM_HEADERS'")


def downgrade() -> None:
    """Deliberately a no-op.

    Postgres cannot drop a value from an enum, and the only way to fake it —
    rebuild the type without the value — fails on any row still using it, which
    is precisely the row an operator would care about. Leaving the value in
    place costs nothing: no code writes it once the feature is reverted, and an
    unused enum member is not a defect.
    """
