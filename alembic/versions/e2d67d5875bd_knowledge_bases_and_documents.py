"""knowledge bases and documents

Revision ID: e2d67d5875bd
Revises: d4a91c603b7e
Create Date: 2026-08-12 03:12:00.007572

Ticket #90 / ADR-0043. `knowledge_bases` is the shelf a `KnowledgeSearch` node
will eventually address (RAG-05, #91 — not this migration); `documents` is
what a creator uploads onto one, identified by `id` rather than by the file
name it arrived under, so two Documents on the same shelf may share a name.
`agent_kb_links` is the many-to-many between the two, with no columns of its
own beyond the pair it names.

No data migration: `data/` is empty and no Postgres volume has ever carried a
document row, so this is purely additive — nothing here reads from or writes
to `documents.py`'s pre-existing on-disk files.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2d67d5875bd"
down_revision: str | Sequence[str] | None = "d4a91c603b7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("mode", sa.Enum("MANAGED", "BYODB", name="knowledgebasemode"), nullable=False),
        # No FK: RAG-20 (#106) has not landed the table this will eventually
        # point at — see the column's doc on `KnowledgeBase`.
        sa.Column("external_connection_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        # Cascade: a shelf is a per-creator resource and unreadable once the
        # account that owns it is gone — same call `interview_sessions` makes.
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_knowledge_bases_owner_id"), "knowledge_bases", ["owner_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kb_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(), nullable=False),
        sa.Column("content_sha256", sa.String(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "EXTRACTING", "CHUNKING", "EMBEDDING", "INDEXED", "FAILED", name="documentstatus"),
            nullable=False,
        ),
        sa.Column("status_detail", sa.String(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("indexed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Per KB, not global — the same file attached to two different shelves
        # is two legitimate Documents (see `Document`'s docstring).
        sa.UniqueConstraint("kb_id", "content_sha256", name="uq_documents_kb_content"),
    )
    op.create_index(op.f("ix_documents_kb_id"), "documents", ["kb_id"])

    op.create_table(
        "agent_kb_links",
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("kb_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id", "kb_id"),
    )


def downgrade() -> None:
    op.drop_table("agent_kb_links")
    op.drop_index(op.f("ix_documents_kb_id"), table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_knowledge_bases_owner_id"), table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
    # Postgres ENUM types outlive their columns — `drop_table` does not drop them.
    sa.Enum(name="documentstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="knowledgebasemode").drop(op.get_bind(), checkfirst=True)
