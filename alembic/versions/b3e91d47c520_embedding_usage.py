"""embedding_usage — the token ledger the embedding provider does not offer

Revision ID: b3e91d47c520
Revises: a7f5c34e91d2
Create Date: 2026-08-13 16:40:00.000000

No embedding provider will say how much you have spent with it: Voyage grants a
free token allowance per model and publishes no API to ask how much remains,
OpenAI simply bills (ADR-0045 §8). This table is where the platform keeps that
number itself: one row per call, taken from the `usage.total_tokens` every
response carries.

Additive and unconditional — a deployment with no embedding provider writes no
rows and reads a total of zero, which is the correct answer for it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e91d47c520"
down_revision: str | Sequence[str] | None = "a7f5c34e91d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `embedding_usage` and the index every total is read through."""
    op.create_table(
        "embedding_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Every read is `SUM(tokens) WHERE model = ?`, asked before each indexing
    # run — the allowance is per model, so the model is the whole access path.
    op.create_index(op.f("ix_embedding_usage_model"), "embedding_usage", ["model"])


def downgrade() -> None:
    """Drop the ledger. Nothing else depends on it; what is lost is the record of
    how much of each allowance had been spent, which cannot be recovered from
    the provider afterwards."""
    op.drop_index(op.f("ix_embedding_usage_model"), table_name="embedding_usage")
    op.drop_table("embedding_usage")
