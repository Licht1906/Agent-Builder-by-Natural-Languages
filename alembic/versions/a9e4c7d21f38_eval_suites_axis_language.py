"""eval_suites.axis_language — the grid's vocabulary overlay

Revision ID: a9e4c7d21f38
Revises: f4a1c8e0d372
Create Date: 2026-08-25 12:00:00.000000

ADR-0055 §2. One nullable JSON column on `eval_suites`, holding a
`GridLanguage` payload: the creator's names for the grid's machine axes and
values, plus any domain input situations added to the input axis. On the suite
rather than on `agent_versions` because the vocabulary names the agent's
domain, which survives every refine the way the suite itself does (ADR-0054
§1); on a column rather than a table because it is one document read whole,
written whole, with no row anything else joins against.

`NULL` is a meaningful state — "nobody has proposed or written words yet" —
and the screen falls back to the machine ids, so no backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9e4c7d21f38"
down_revision: str | Sequence[str] | None = "f4a1c8e0d372"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("eval_suites", sa.Column("axis_language", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("eval_suites", "axis_language")
