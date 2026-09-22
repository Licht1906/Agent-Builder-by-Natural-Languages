"""seed demo accounts

Revision ID: 1135e617defc
Revises: dcac21ed0150
Create Date: 2026-07-31 02:13:57.246800

One account per role (ticket #4), so a freshly migrated database can be logged
into immediately: the approval flow needs an admin to exist before any creator's
work can be reviewed.

Passwords are hashed here at migration time from the current settings, so an
environment that overrides `SEED_*_PASSWORD` seeds its own credentials rather
than the development defaults written into this file's history.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.auth.seed import seed_user_rows

# revision identifiers, used by Alembic.
revision: str = '1135e617defc'
down_revision: str | Sequence[str] | None = 'dcac21ed0150'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _users_table() -> sa.TableClause:
    return sa.table(
        'users',
        sa.column('email', sa.String()),
        sa.column('password_hash', sa.String()),
        sa.column('role', sa.String()),
    )


def upgrade() -> None:
    """Seed one creator and one admin."""
    # `users.role` is a native enum whose labels are the member *names*, which
    # is what SQLAlchemy writes for a mapped Enum column — so send names here.
    op.bulk_insert(
        _users_table(),
        [{**row, 'role': row['role'].name} for row in seed_user_rows()],
    )


def downgrade() -> None:
    """Remove the seeded accounts, leaving any account created since."""
    seeded_emails = [row['email'] for row in seed_user_rows()]
    users = _users_table()
    op.execute(users.delete().where(users.c.email.in_(seeded_emails)))
