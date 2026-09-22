"""external connections

Revision ID: 7c4f2a91d6e3
Revises: 52f8db2e5b92
Create Date: 2026-08-13 03:00:00.000000

RAG-20 (#106) / ADR-0045: `external_connections` is BYODB's row — a creator's
pointer at a vector database this platform does not run. `secret_ref` names a
key in `secret_store_entries` (RAG-19), never a value; `connection_url` is the
one address `POST .../test` is ever allowed to dial, always through
`src.services.ssrf_guard` first.

`knowledge_bases.external_connection_id` has existed, nullable and
unconstrained, since #90 — this is the FK RAG-20 promised to add once the
table it names existed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c4f2a91d6e3"
down_revision: str | Sequence[str] | None = "52f8db2e5b92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("provider", sa.Enum("QDRANT", "PINECONE", "WEAVIATE", "PGVECTOR", name="externalconnectionprovider"), nullable=False),
        sa.Column("connection_url", sa.String(), nullable=False),
        sa.Column("secret_ref", sa.String(), nullable=False),
        sa.Column("collection_name", sa.String(), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("distance_metric", sa.Enum("COSINE", "EUCLIDEAN", "DOT", name="vectordistancemetric"), nullable=False),
        sa.Column("field_mapping", sa.JSON(), nullable=False),
        sa.Column("status", sa.Enum("UNTESTED", "CONNECTED", "FAILED", name="externalconnectionstatus"), nullable=False),
        sa.Column("status_detail", sa.String(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_external_connections_owner_id", "external_connections", ["owner_id"])

    op.create_foreign_key(
        "fk_knowledge_bases_external_connection_id",
        "knowledge_bases",
        "external_connections",
        ["external_connection_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_knowledge_bases_external_connection_id", "knowledge_bases", type_="foreignkey")
    op.drop_index("ix_external_connections_owner_id", table_name="external_connections")
    op.drop_table("external_connections")
    sa.Enum(name="externalconnectionstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="vectordistancemetric").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="externalconnectionprovider").drop(op.get_bind(), checkfirst=True)
