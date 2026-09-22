"""uc1 pending actions

Revision ID: 8b1e2f7c9a4d
Revises: 3fba9bf11793
Create Date: 2026-08-15 01:30:00.000000

UC1-21 (#137) + UC1-23 (#139), spec.md §4.6/§19: the action-level approval
queue for UC-1's two `scope: write` tools (`mail.send_reminder`,
`task.create`), separate from `uc1_version_approvals` (which gates a whole
*version*, not one tool call). `action_hash` is written once, at creation,
and never updated — that is what makes FR-45's "editing the payload after
approval invalidates it" a fact the row itself can't silently drift out of.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b1e2f7c9a4d"
down_revision: str | Sequence[str] | None = "3fba9bf11793"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_VALUES = ("pending", "approved", "rejected")
_PENDING_ACTION_STATUS = postgresql.ENUM(*_STATUS_VALUES, name="uc1pendingactionstatus", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*_STATUS_VALUES, name="uc1pendingactionstatus").create(bind, checkfirst=True)

    op.create_table(
        "uc1_pending_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("tool", sa.String(), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("action_hash", sa.String(), nullable=False),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("approver_roles", sa.JSON(), nullable=False),
        sa.Column("requested_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("requested_by_sub", sa.String(), nullable=False),
        sa.Column("requested_by_employee_id", sa.String(), nullable=False),
        sa.Column("requested_by_department", sa.String(), nullable=False),
        sa.Column("requested_by_roles", sa.JSON(), nullable=False),
        sa.Column("status", _PENDING_ACTION_STATUS, nullable=False, server_default="pending"),
        sa.Column("decided_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("decision_reason", sa.String(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_uc1_pending_actions_agent_id", "uc1_pending_actions", ["agent_id"])
    op.create_index("ix_uc1_pending_actions_action_hash", "uc1_pending_actions", ["action_hash"])


def downgrade() -> None:
    op.drop_index("ix_uc1_pending_actions_action_hash", table_name="uc1_pending_actions")
    op.drop_index("ix_uc1_pending_actions_agent_id", table_name="uc1_pending_actions")
    op.drop_table("uc1_pending_actions")
    bind = op.get_bind()
    postgresql.ENUM(name="uc1pendingactionstatus").drop(bind, checkfirst=True)
