"""agent_runs — the shared run ledger with traces; run_labels; quality_scores.run_id

Revision ID: c4d9e8f2a1b7
Revises: 9d4f1c2b7e38
Create Date: 2026-08-22 20:00:00.000000

ADR-0053. Three things, all additive except two columns going nullable:

1. `agent_runs` — one row per leg of a run in *either* pipeline, carrying the
   transcript and the attributes the PM groups by. Backfilled from every
   `run_costs` row that exists (V2's bill), so the board keeps its history —
   without transcripts, because nothing kept them before this revision, and
   `simulated` read off the seed script's `sim-` prefix.
2. `run_labels` — the expert's binary verdict + trace codes on one run.
3. `quality_scores.run_id` → `agent_runs`, filled from the backfill mapping
   so every score that named a `run_costs` row now names its `agent_runs`
   row too; `agent_id` / `version_id` become nullable because a score on a
   UC-1 run has neither.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d9e8f2a1b7"
down_revision: str | Sequence[str] | None = "9d4f1c2b7e38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pipeline", sa.String(), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("agent_key", sa.String(), nullable=False),
        sa.Column("version_key", sa.String(), nullable=False),
        sa.Column("agent_name", sa.String(), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=True),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=True),
        sa.Column("run_cost_id", sa.Integer(), sa.ForeignKey("run_costs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("caller", sa.String(), nullable=True),
        sa.Column("trigger", sa.String(), nullable=False, server_default="manual"),
        sa.Column("outcome", sa.String(), nullable=False, server_default="success"),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("latency_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usage_by_model", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("input_message", sa.String(), nullable=True),
        sa.Column("answer", sa.String(), nullable=True),
        sa.Column("transcript", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("node_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("first_failure", sa.String(), nullable=True),
        sa.Column("failure_node_type", sa.String(), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    for column in (
        "trace_id",
        "agent_key",
        "version_key",
        "agent_id",
        "version_id",
        "run_cost_id",
        "failure_node_type",
        "simulated",
        "created_at",
    ):
        op.create_index(f"ix_agent_runs_{column}", "agent_runs", [column])

    # Backfill: the bill becomes the first page of the ledger. No transcript —
    # nothing kept one — and the seed script's rows keep their mark.
    op.execute(
        """
        INSERT INTO agent_runs (
            pipeline, trace_id, agent_key, version_key, agent_name, agent_id, version_id, run_cost_id,
            conversation_id, caller, trigger, outcome, error, cost_usd, latency_seconds, input_tokens,
            output_tokens, usage_by_model, transcript, actions, simulated, created_at
        )
        SELECT
            'v2',
            COALESCE(rc.trace_id, 'run-cost-' || rc.id),
            CAST(rc.agent_id AS VARCHAR),
            CAST(rc.version_id AS VARCHAR),
            COALESCE(a.name, 'agent ' || rc.agent_id),
            rc.agent_id,
            rc.version_id,
            rc.id,
            rc.conversation_id,
            NULL,
            rc.trigger,
            rc.outcome,
            rc.error,
            rc.cost_usd,
            rc.latency_seconds,
            rc.input_tokens,
            rc.output_tokens,
            rc.usage_by_model,
            '[]',
            '[]',
            rc.conversation_id LIKE 'sim-%',
            rc.created_at
        FROM run_costs rc
        LEFT JOIN agents a ON a.id = rc.agent_id
        ORDER BY rc.id
        """
    )

    op.create_table(
        "run_labels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reviewer_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("codes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_run_labels_run_id", "run_labels", ["run_id"])

    op.add_column(
        "quality_scores",
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=True),
    )
    op.create_index("ix_quality_scores_run_id", "quality_scores", ["run_id"])
    op.execute(
        """
        UPDATE quality_scores qs
        SET run_id = ar.id
        FROM agent_runs ar
        WHERE ar.run_cost_id = qs.run_cost_id AND qs.run_cost_id IS NOT NULL
        """
    )
    op.alter_column("quality_scores", "agent_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column("quality_scores", "version_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM quality_scores WHERE agent_id IS NULL OR version_id IS NULL")
    op.alter_column("quality_scores", "version_id", existing_type=sa.Integer(), nullable=False)
    op.alter_column("quality_scores", "agent_id", existing_type=sa.Integer(), nullable=False)
    op.drop_index("ix_quality_scores_run_id", table_name="quality_scores")
    op.drop_column("quality_scores", "run_id")
    op.drop_index("ix_run_labels_run_id", table_name="run_labels")
    op.drop_table("run_labels")
    for column in (
        "created_at",
        "simulated",
        "failure_node_type",
        "run_cost_id",
        "version_id",
        "agent_id",
        "version_key",
        "agent_key",
        "trace_id",
    ):
        op.drop_index(f"ix_agent_runs_{column}", table_name="agent_runs")
    op.drop_table("agent_runs")
