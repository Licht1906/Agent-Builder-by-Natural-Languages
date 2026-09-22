"""interview sessions point at the agent they generated

Revision ID: a4d70b19c8e2
Revises: f3b8c26d5a91
Create Date: 2026-08-02 22:40:00.000000

One nullable column, and it exists to answer a question generation (#17) asks
every time it runs: *is there already an agent for this interview?*

Null until the creator generates for the first time — an interview runs before
there is an agent to point at (#16, ADR-0014 §5). Set once afterwards, so
generating again adds a new **draft version** to the same agent instead of
standing up a near-duplicate agent beside it. That is also what makes refining by
chat (#18) a continuation of the same lifecycle rather than a second one.

`ON DELETE SET NULL` rather than a cascade: an interview outlives the agent it
produced. A creator who deletes an agent has thrown away the result, not the
description they wrote, and generating again is how they get another one.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4d70b19c8e2"
down_revision: str | Sequence[str] | None = "f3b8c26d5a91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `interview_sessions.agent_id`."""
    op.add_column("interview_sessions", sa.Column("agent_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_interview_sessions_agent_id",
        "interview_sessions",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Drop `interview_sessions.agent_id`."""
    op.drop_constraint("fk_interview_sessions_agent_id", "interview_sessions", type_="foreignkey")
    op.drop_column("interview_sessions", "agent_id")
