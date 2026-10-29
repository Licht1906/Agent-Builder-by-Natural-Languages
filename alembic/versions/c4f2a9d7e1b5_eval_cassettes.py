"""eval cassettes — the views an eval case reads, frozen as its asker saw them

Revision ID: c4f2a9d7e1b5
Revises: b8e3f1a6c2d4
Create Date: 2026-10-29 12:00:00.000000

EV-02 / ADR-0069 §2: an eval run answers every `data_query` from these rows and
never reads the creator's database.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f2a9d7e1b5"
down_revision: str | Sequence[str] | None = "b8e3f1a6c2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_cassettes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("eval_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("view", sa.String(), nullable=False),
        sa.Column("run_as", sa.String(), nullable=True),
        sa.Column("schema_hash", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("case_id", "source_id", "view", name="uq_eval_cassettes_case_view"),
    )
    op.create_index("ix_eval_cassettes_case_id", "eval_cassettes", ["case_id"])


def downgrade() -> None:
    op.drop_index("ix_eval_cassettes_case_id", table_name="eval_cassettes")
    op.drop_table("eval_cassettes")
