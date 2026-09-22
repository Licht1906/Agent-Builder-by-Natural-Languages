"""ingestion costs

Revision ID: 9dbf9accf508
Revises: e2d67d5875bd
Create Date: 2026-08-12 10:00:00.000000

The table behind ingest spend (ticket #101, ADR-0009 §4) — one row per fresh
(non-cached) extraction that actually called the OpenRouter VLM. Not a row on
`run_costs`: ingesting a Document is not a run, and `run_costs.agent_id` /
`version_id` / `conversation_id` are all `NOT NULL` because a run always has
those three. This is a second, independent cost axis — Knowledge Base /
Document rather than agent version — so it is a table of its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9dbf9accf508"
down_revision: str | Sequence[str] | None = "e2d67d5875bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `ingestion_costs`."""
    op.create_table(
        "ingestion_costs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("kb_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        # A cost row is a statement about one Document on one Knowledge Base
        # owned by somebody; once any of the three is gone there is nobody left
        # to bill — the same call `run_costs` makes for `agent_id`/`version_id`.
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ingestion_costs_document_id"), "ingestion_costs", ["document_id"])
    op.create_index(op.f("ix_ingestion_costs_kb_id"), "ingestion_costs", ["kb_id"])
    op.create_index(op.f("ix_ingestion_costs_owner_id"), "ingestion_costs", ["owner_id"])
    op.create_index(op.f("ix_ingestion_costs_created_at"), "ingestion_costs", ["created_at"])


def downgrade() -> None:
    """Drop `ingestion_costs`."""
    op.drop_index(op.f("ix_ingestion_costs_created_at"), table_name="ingestion_costs")
    op.drop_index(op.f("ix_ingestion_costs_owner_id"), table_name="ingestion_costs")
    op.drop_index(op.f("ix_ingestion_costs_kb_id"), table_name="ingestion_costs")
    op.drop_index(op.f("ix_ingestion_costs_document_id"), table_name="ingestion_costs")
    op.drop_table("ingestion_costs")
