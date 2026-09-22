"""interview sessions

Revision ID: f3b8c26d5a91
Revises: e51c8a97b3d4
Create Date: 2026-08-02 21:05:00.000000

The one table behind the slot-filling interview (ticket #16, ADR-0014).

It exists for a single criterion: *"đóng trang giữa lúc trả lời rồi quay lại thì
tiếp tục được"*. Every turn of the interview commits a row here, so what a creator
comes back to is what they last said rather than what the last completed request
happened to leave behind.

Deliberately three columns and no more. `slots` holds **only filled slots**, which
is what lets the next question, the progress count and whether the interview is
finished all be derived from which slots are absent (`src.interview.slots`). A
`status` or a `current_question` column here would be a second opinion about
whether the interview is over, and the ticket asks for exactly one — a stop that
is arithmetic, not a stored judgement.

Attached to `users` rather than to `agents` because an interview runs before there
is an agent: #17 is what turns a finished sheet into a draft version. The cascade
follows the same reasoning `sandbox_runs` uses — a half-finished interview belongs
to one person and is unresumable once that account is gone.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3b8c26d5a91"
down_revision: str | Sequence[str] | None = "e51c8a97b3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `interview_sessions`."""
    op.create_table(
        "interview_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        # Only the slots that hold something. A slot missing from this document is
        # a slot the interview still has to ask about — presence *is* filled-ness,
        # so there is no separate flag to fall out of step with the value.
        sa.Column("slots", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Indexed because the only listing query is "this creator's interviews" — the
    # path a creator takes to find the session they walked away from.
    op.create_index(op.f("ix_interview_sessions_owner_id"), "interview_sessions", ["owner_id"])


def downgrade() -> None:
    """Drop `interview_sessions`."""
    op.drop_index(op.f("ix_interview_sessions_owner_id"), table_name="interview_sessions")
    op.drop_table("interview_sessions")
