"""audit chain — what V2 runs read and did, hash-chained

Revision ID: e7b4c1d8a2f6
Revises: d6a3b9e2f5c8
Create Date: 2026-10-27 00:00:00.000000

DS-10 / ADR-0059 §11: UC-1's `audit_log` carried into V2's runtime.
`audit_events` is the chain itself — `seq` contiguous, each entry sealing
the one before (`src.audit.chain`); `audit_chain_head` is the one row every
append locks, created here empty so the first append has something to lock.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b4c1d8a2f6"
down_revision: str | Sequence[str] | None = "d6a3b9e2f5c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("prev_hash", sa.String(), nullable=False),
        sa.Column("entry_hash", sa.String(), nullable=False),
    )
    op.create_index("ix_audit_events_kind", "audit_events", ["kind"])
    op.create_index("ix_audit_events_agent_id", "audit_events", ["agent_id"])
    op.create_index("ix_audit_events_trace_id", "audit_events", ["trace_id"])
    head = op.create_table(
        "audit_chain_head",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("last_seq", sa.Integer(), nullable=False),
        sa.Column("last_hash", sa.String(), nullable=False),
    )
    op.bulk_insert(head, [{"id": 1, "last_seq": 0, "last_hash": "0" * 64}])


def downgrade() -> None:
    op.drop_table("audit_chain_head")
    op.drop_index("ix_audit_events_trace_id", table_name="audit_events")
    op.drop_index("ix_audit_events_agent_id", table_name="audit_events")
    op.drop_index("ix_audit_events_kind", table_name="audit_events")
    op.drop_table("audit_events")
