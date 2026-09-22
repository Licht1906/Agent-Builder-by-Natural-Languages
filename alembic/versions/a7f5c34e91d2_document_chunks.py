"""document_chunks — the retrieval index behind KnowledgeSearch

Revision ID: a7f5c34e91d2
Revises: d4a91c603b7e
Create Date: 2026-08-13 14:10:00.000000

The table ADR-0045 adds. Until now a `KnowledgeSearch` node re-read, re-split
and re-scored every attached file on every question, which meant there was no
object in the system that *was* a passage — nowhere for an embedding to live,
and nothing to measure retrieval quality against. This is that object.

**Requires the `vector` extension.** `CREATE EXTENSION` is run here rather than
by hand in a runbook, because a schema that half-exists is worse than one that
refused to migrate. The extension ships with the `pgvector/pgvector` image the
compose file now uses; on stock `postgres:16` this revision fails at the first
statement, which is the correct place to find out.

No backfill. Chunks are written the first time a document is searched
(`src.workflow.index.ensure_indexed`), so an existing library indexes itself on
its next question rather than paying for every document a creator ever uploaded
— including the ones no workflow points at.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op
from src.workflow.embeddings import EMBEDDING_DIMENSIONS

# revision identifiers, used by Alembic.
revision: str = "a7f5c34e91d2"
down_revision: str | Sequence[str] | None = "d4a91c603b7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enable pgvector, then create `document_chunks` and its two indexes."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.String(), nullable=False, server_default=""),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("chunker_version", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        # CASCADE, matching `agent_memories`: a deleted account's chunks are
        # passages nobody can search, cite, or delete by hand — and they are
        # derived data, so losing them costs nothing but a re-index.
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_document_chunks_owner_id"), "document_chunks", ["owner_id"])
    # The index the workload actually uses: every read and the delete half of
    # every re-index ask for one owner's chunks of one ref.
    op.create_index("ix_document_chunks_owner_ref", "document_chunks", ["owner_id", "ref"])
    # No ANN index (`ivfflat`/`hnsw`) on purpose. A search is already narrowed
    # by owner and by the handful of refs the node names, so the vector scan
    # runs over tens or hundreds of rows — where an approximate index is slower
    # to build, needs tuning per row count, and trades away exactness for a
    # speed-up nobody would measure. Add one when a library is large enough to
    # justify it; the query does not change.


def downgrade() -> None:
    """Drop the index. The extension stays — it is a database-wide object this
    revision may not have been the only one to want."""
    op.drop_index("ix_document_chunks_owner_ref", table_name="document_chunks")
    op.drop_index(op.f("ix_document_chunks_owner_id"), table_name="document_chunks")
    op.drop_table("document_chunks")
