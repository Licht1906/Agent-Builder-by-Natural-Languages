"""v2 agent solution specification

Revision ID: d9c14b7f2a63
Revises: a4d70b19c8e2
Create Date: 2026-08-08 20:10:00.000000

V2's first structure lands (ticket #42, ADR-0020): the AI Solution Designer
interview, and the seven-field spec it produces.

Three changes, and they are one change seen from three sides:

1. `versionstatus` gains the two stages that come *before* everything V1 had —
   a version now begins life holding a business problem nobody has confirmed
   yet, and only `spec_confirmed` opens the Workflow Generator's door.
2. `agent_versions` widens to carry both documents (ADR-0020 §5). `solution_spec`
   is the new one; `spec` becomes nullable because a version at
   `spec_pending_confirmation` has no executable pipeline yet, and writing an
   empty `AgentSpec` to keep the column full would be a spec claiming an agent
   that cannot run.
3. `solution_interview_sessions` holds everything that is still *unsettled* —
   a half-filled draft, the contradictions the model flagged, the question
   currently open — precisely so `agent_versions.solution_spec` never has to.
   That split is what lets every structure downstream assume the spec it
   receives is complete (ADR-0020 §3).

Its own table rather than columns on V1's `interview_sessions`. The two
interviews stop on different conditions — V1's is arithmetic over five empty
slots (ADR-0014), V2's is the model's judgement plus seven fields — and one row
holding both would be one row with two contradictory answers to "is this
finished".
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9c14b7f2a63"
down_revision: str | Sequence[str] | None = "a4d70b19c8e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What goes into the `versionstatus` type: SQLAlchemy stores an enum's *member
#: names*, not its values, so these are `SPEC_CONFIRMED` rather than
#: `spec_confirmed` — matching how the first migration wrote the other five.
_NEW_STATUSES = ("SPEC_PENDING_CONFIRMATION", "SPEC_CONFIRMED")


def upgrade() -> None:
    """Widen the version status and row, and create the V2 interview table."""
    # `ADD VALUE` is permitted inside a transaction from PostgreSQL 12 as long as
    # the new value is not *used* in the same one — nothing below writes a status.
    for name in _NEW_STATUSES:
        op.execute(f"ALTER TYPE versionstatus ADD VALUE IF NOT EXISTS '{name}'")

    op.alter_column("agent_versions", "spec", existing_type=sa.JSON(), nullable=True)
    op.add_column("agent_versions", sa.Column("solution_spec", sa.JSON(), nullable=True))

    op.create_table(
        "solution_interview_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        # Only the fields that hold something. A field missing from this document
        # is a field the interview still has to ask about — presence *is*
        # answered-ness, so there is no separate flag to fall out of step with it.
        sa.Column("draft", sa.JSON(), nullable=False),
        # Stored rather than derived, unlike V1's slots: "these two things you
        # told me cannot both be true" is a judgement somebody formed, and no
        # amount of looking at which fields are empty reproduces it.
        sa.Column("conflicts", sa.JSON(), nullable=False),
        sa.Column("open_question", sa.JSON(), nullable=True),
        sa.Column("turns", sa.JSON(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        # Cascade like V1's: a half-finished description of somebody's business is
        # unreadable — and unresumable — once that account is gone.
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        # `SET NULL`: an agent the creator deleted must not take their interview
        # with it — the reading is still theirs, and proposing again is how they
        # get a new agent out of it.
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Indexed because the only listing query is "this creator's interviews" — the
    # path a creator takes to find the session they walked away from.
    op.create_index(op.f("ix_solution_interview_sessions_owner_id"), "solution_interview_sessions", ["owner_id"])


def downgrade() -> None:
    """Drop the V2 table and column, and put `spec` back the way V1 had it.

    **Destructive, by necessity.** Restoring `spec NOT NULL` means there can be no
    row without one, and every V2 version at the spec stage is exactly such a row
    — so they are deleted. There is nothing to preserve them as: their whole
    content is the `solution_spec` column this same downgrade drops. Anyone
    running it should expect to lose every agent that has not reached a workflow.

    The two enum values are deliberately left behind. PostgreSQL cannot remove a
    value from an enum type, and rebuilding `versionstatus` to drop them would
    mean rewriting a column that every other table's foreign keys hang off — a
    far larger risk than two unused labels.
    """
    op.drop_index(op.f("ix_solution_interview_sessions_owner_id"), table_name="solution_interview_sessions")
    op.drop_table("solution_interview_sessions")
    op.drop_column("agent_versions", "solution_spec")
    op.execute("DELETE FROM agent_versions WHERE spec IS NULL")
    op.alter_column("agent_versions", "spec", existing_type=sa.JSON(), nullable=False)
