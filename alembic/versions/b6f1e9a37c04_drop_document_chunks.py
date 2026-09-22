"""drop document_chunks — one retrieval stack, and it does not live in Postgres

Revision ID: b6f1e9a37c04
Revises: a9e4c7d21f38
Create Date: 2026-08-27 00:00:00.000000

Two branches built a RAG stack in parallel and the merge kept both: a pgvector
one whose index was this table (`a7f5c34e91d2`), and a Qdrant one. Retrieval is
now `QdrantKnowledgeStore` and only that (`src.workflow.knowledge`), so nothing
reads or writes `document_chunks` any more and a table nobody writes is a table
that answers questions about a corpus as it was on the day the merge landed.

**Dropped rather than left standing.** The rows are a *cache* — they were only
ever written by `ensure_indexed` from files still on disk, and every Document
they were derived from is in `documents` with its bytes under
`tool_file_base_dir`. Nothing here is a source of truth, so there is nothing to
migrate into Qdrant: a shelf that needs points has `POST
/knowledge-bases/{kb_id}/documents/{document_id}/reingest`, which reads those
same bytes through the pipeline that is actually serving.

The `vector` extension stays. `a7f5c34e91d2` created it, but dropping it is not
this revision's business — it is a database-wide object, cheap to leave, and an
operator who put their own `vector` column somewhere would lose it.

`downgrade` rebuilds the table empty, matching `a7f5c34e91d2`'s own shape. It
does not restore the rows, for the reason above: they were a cache of files that
are still there.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op
from src.workflow.embeddings import EMBEDDING_DIMENSIONS

# revision identifiers, used by Alembic.
revision: str = "b6f1e9a37c04"
down_revision: str | Sequence[str] | None = "a9e4c7d21f38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_document_chunks_owner_ref", table_name="document_chunks")
    op.drop_index("ix_document_chunks_owner_id", table_name="document_chunks")
    op.drop_table("document_chunks")


def downgrade() -> None:
    """Rebuild the table, empty — see the module docstring on why the rows do not come back."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("ref", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("chunker_version", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_chunks_owner_id", "document_chunks", ["owner_id"])
    op.create_index("ix_document_chunks_owner_ref", "document_chunks", ["owner_id", "ref"])
