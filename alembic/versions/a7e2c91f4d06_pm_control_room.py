"""PM control room — `trigger`/`outcome` on run_costs, quality_scores, meta_agent_gate_runs

Revision ID: a7e2c91f4d06
Revises: f2c9d4e71b83
Create Date: 2026-08-21 15:00:00.000000

PM playbook §1 (trace attributes a PM groups by), §4 (scores from the three
evaluator tiers) and step 3 (the gate's answer, remembered). Additive only.

The two new `run_costs` columns are backfilled from what the row already
knows: a sandbox run was a `chat_test` — and so was a run on a `trial` thread,
whose tools were real but whose caller was the creator testing; everything else
was `manual`. A row with an `error` ended in `error`, the rest in `success`.
That is exactly the derivation
`src.agents.spend.run_cost_row` applies to new rows, so old and new rows
read the same.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7e2c91f4d06"
down_revision: str | Sequence[str] | None = "f2c9d4e71b83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE = sa.Enum("CODE", "JUDGE", "HUMAN", name="scoresource")


def upgrade() -> None:
    op.add_column("run_costs", sa.Column("trigger", sa.String(), nullable=False, server_default="manual"))
    op.add_column("run_costs", sa.Column("outcome", sa.String(), nullable=False, server_default="success"))
    op.execute("UPDATE run_costs SET trigger = 'chat_test' WHERE sandbox = true OR namespace = 'TRIAL'")
    op.execute("UPDATE run_costs SET outcome = 'error' WHERE error IS NOT NULL")

    op.create_table(
        "quality_scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_cost_id", sa.Integer(), sa.ForeignKey("run_costs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("source", _SOURCE, nullable=False),
        sa.Column("given_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_quality_scores_agent_id", "quality_scores", ["agent_id"])
    op.create_index("ix_quality_scores_version_id", "quality_scores", ["version_id"])
    op.create_index("ix_quality_scores_run_cost_id", "quality_scores", ["run_cost_id"])
    op.create_index("ix_quality_scores_name", "quality_scores", ["name"])

    op.create_table(
        "meta_agent_gate_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("thresholds", sa.JSON(), nullable=False),
        sa.Column("per_family", sa.JSON(), nullable=False),
        sa.Column("verdicts", sa.JSON(), nullable=False),
        sa.Column("triggered_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("meta_agent_gate_runs")
    op.drop_index("ix_quality_scores_name", table_name="quality_scores")
    op.drop_index("ix_quality_scores_run_cost_id", table_name="quality_scores")
    op.drop_index("ix_quality_scores_version_id", table_name="quality_scores")
    op.drop_index("ix_quality_scores_agent_id", table_name="quality_scores")
    op.drop_table("quality_scores")
    _SOURCE.drop(op.get_bind(), checkfirst=True)
    op.drop_column("run_costs", "outcome")
    op.drop_column("run_costs", "trigger")
