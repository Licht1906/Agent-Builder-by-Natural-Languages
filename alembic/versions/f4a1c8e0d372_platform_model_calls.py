"""platform_model_calls — the bill for what the platform spends on its own behalf

Revision ID: f4a1c8e0d372
Revises: e8b3d6f4a219
Create Date: 2026-08-24 12:00:00.000000

`run_costs` has always answered "what did this agent's *runs* cost". Nothing
answered "what did *building* the agents cost", and the gap was found the
expensive way: an OpenAI project hit its enforced spend limit while the
platform's own cost board reported $1.57 in total, because the board could only
see the half that goes through `src.agents.runtime`. The interview, the spec
draft, the composer, the refiner, the eval-case writer and the judge were
thirteen call sites that spent money and left no row anywhere.

One row per **round trip**, so a document that took three attempts is three
rows: the retry loop's cost is then visible as its own, instead of being folded
into the price of the graph it eventually produced.

No foreign keys — `agent_runs` already learned this (ADR-0053). A bill has to
outlive the thing it was spent on, or the ledger goes silent about exactly the
agents somebody deleted for being expensive.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a1c8e0d372"
down_revision: str | None = "e8b3d6f4a219"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_model_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_seconds", sa.Float(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    # The two group-bys every question of this table starts from: which job, and
    # what happened in a window.
    op.create_index("ix_platform_model_calls_purpose", "platform_model_calls", ["purpose"])
    op.create_index("ix_platform_model_calls_created_at", "platform_model_calls", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_platform_model_calls_created_at", table_name="platform_model_calls")
    op.drop_index("ix_platform_model_calls_purpose", table_name="platform_model_calls")
    op.drop_table("platform_model_calls")
