"""sandbox runs

Revision ID: 94fc5d6353f7
Revises: 807eecbb4f5a
Create Date: 2026-08-01 16:06:01.296641

The table behind the sandbox transcript (ticket #9, ADR-0007) — the mandatory
evidence an approval request is built on. One row per trial run, holding the
ordered transcript of what the agent did and what its side-effecting tools
*would* have done.

The measured token/cost/latency figures the approval dossier also shows are
not here: they belong to budget enforcement (#10) and the cost trace (#11),
which add their own columns in their own migrations.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "94fc5d6353f7"
down_revision: str | Sequence[str] | None = "807eecbb4f5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `sandbox_runs`."""
    op.create_table(
        "sandbox_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("input_message", sa.String(), nullable=False),
        sa.Column("transcript", sa.JSON(), nullable=False),
        sa.Column("status", sa.Enum("SUCCEEDED", "FAILED", name="sandboxrunstatus"), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        # A transcript is evidence *about* a version and says nothing once that
        # version is gone, so it goes with it rather than blocking its deletion.
        sa.ForeignKeyConstraint(["version_id"], ["agent_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sandbox_runs_version_id"), "sandbox_runs", ["version_id"])


def downgrade() -> None:
    """Drop `sandbox_runs` and the enum type it owns."""
    op.drop_index(op.f("ix_sandbox_runs_version_id"), table_name="sandbox_runs")
    op.drop_table("sandbox_runs")
    # Postgres ENUM types outlive their column — drop_table doesn't drop them.
    sa.Enum(name="sandboxrunstatus").drop(op.get_bind(), checkfirst=True)
