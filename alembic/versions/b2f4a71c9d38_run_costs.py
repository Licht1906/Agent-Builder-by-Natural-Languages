"""run costs

Revision ID: b2f4a71c9d38
Revises: 94fc5d6353f7
Create Date: 2026-08-01 23:40:00.000000

The table behind the measured cost figure (ticket #11, ADR-0009) — one row per
run, trial and production alike, which is why it is not the columns on
`sandbox_runs` that the previous migration left room for. "Which agent is
costing us money" is a question about the runs nobody watches, and rehearsals
alone cannot answer it.

Only the measurement is stored. The estimate and the worst-case ceiling are pure
functions of an immutable spec, so persisting them would cache a computation and
let it drift against a price change.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2f4a71c9d38"
down_revision: str | Sequence[str] | None = "94fc5d6353f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `run_costs`."""
    op.create_table(
        "run_costs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        # Whether tools were simulated, not which thread namespace was used: a
        # production chat may run on a `trial` thread and still call real tools.
        sa.Column("sandbox", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_seconds", sa.Float(), nullable=False),
        sa.Column("usage_by_model", sa.JSON(), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        # A bill is about an agent and a version; once either is gone there is
        # nobody left to send it to.
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["agent_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_run_costs_agent_id"), "run_costs", ["agent_id"])
    op.create_index(op.f("ix_run_costs_version_id"), "run_costs", ["version_id"])


def downgrade() -> None:
    """Drop `run_costs`."""
    op.drop_index(op.f("ix_run_costs_version_id"), table_name="run_costs")
    op.drop_index(op.f("ix_run_costs_agent_id"), table_name="run_costs")
    op.drop_table("run_costs")
