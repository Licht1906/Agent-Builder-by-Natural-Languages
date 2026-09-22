"""uc1 sandbox

Revision ID: 7f3a9c1e2b6d
Revises: cadb5f6f09f9
Create Date: 2026-08-14 22:00:00.000000

UC1-38 (#126), spec.md §4.5/§19: `uc1_agent_spec_versions` is the first place a
UC-1 `AgentSpec` is persisted and addressed by `(agent_id, version)` over
HTTP; `uc1_sandbox_runs` is FR-37's transcript, one row per sandbox chat turn.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f3a9c1e2b6d"
down_revision: str | Sequence[str] | None = "cadb5f6f09f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SANDBOX_RUN_STATUS = postgresql.ENUM("completed", "budget_exceeded", name="uc1sandboxrunstatus", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM("completed", "budget_exceeded", name="uc1sandboxrunstatus").create(bind, checkfirst=True)

    op.create_table(
        "uc1_agent_spec_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("agent_id", "version", name="uq_uc1_agent_spec_versions_agent_version"),
    )
    op.create_index("ix_uc1_agent_spec_versions_agent_id", "uc1_agent_spec_versions", ["agent_id"])

    op.create_table(
        "uc1_sandbox_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("principal_employee_id", sa.String(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("status", _SANDBOX_RUN_STATUS, nullable=False),
        sa.Column("transcript", sa.JSON(), nullable=False),
        sa.Column("total_cost_usd", sa.Float(), nullable=False),
        sa.Column("final_answer", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_uc1_sandbox_runs_agent_id", "uc1_sandbox_runs", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_uc1_sandbox_runs_agent_id", table_name="uc1_sandbox_runs")
    op.drop_table("uc1_sandbox_runs")
    op.drop_index("ix_uc1_agent_spec_versions_agent_id", table_name="uc1_agent_spec_versions")
    op.drop_table("uc1_agent_spec_versions")
    bind = op.get_bind()
    postgresql.ENUM(name="uc1sandboxrunstatus").drop(bind, checkfirst=True)
