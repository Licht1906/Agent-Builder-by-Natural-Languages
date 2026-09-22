"""secret store

Revision ID: 52f8db2e5b92
Revises: b3f1c9a7d0e2
Create Date: 2026-08-13 00:00:00.000000

RAG-19 (#105) / ADR-0045: the store `ToolRegistration.credential_secret_ref`
was always documented as pointing into, and never built — `src.workflow.
registrations` refuses `SECRET_REFERENCE` connections at run time for exactly
that reason. One table, `secret_store_entries`, holds every secret this
platform stores as a `key` plus its AES-256-GCM ciphertext; the key that opens
that ciphertext is `SECRET_STORE_KEY`, an environment variable, and never a
column here.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "52f8db2e5b92"
down_revision: str | Sequence[str] | None = "b3f1c9a7d0e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secret_store_entries",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("rotated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("secret_store_entries")
