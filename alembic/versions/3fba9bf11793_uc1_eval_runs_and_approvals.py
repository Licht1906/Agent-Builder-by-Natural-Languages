"""uc1 eval runs and approvals

Revision ID: 3fba9bf11793
Revises: 7f3a9c1e2b6d
Create Date: 2026-08-14 23:50:00.000000

UC1-17 (#133), spec.md §4.4/§13/§19: two tables the approval gate needs.
`uc1_eval_runs` is one row per golden-set smoke test (FR-33) —
`submit-for-approval` reads its most recent row's `pass_rate` in place of
UC1-39's stand-in, and `case_results` is the per-case log the approval
dossier (UC1-16) renders. `uc1_version_approvals` accumulates HITL #2's
distinct signers one HTTP call at a time, since `src.agentspec.lifecycle.approve`
needs every approver id at once but admins approve one at a time (FR-30/FR-32).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3fba9bf11793"
down_revision: str | Sequence[str] | None = "7f3a9c1e2b6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "uc1_eval_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("passed_count", sa.Integer(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("pass_rate", sa.Float(), nullable=False),
        sa.Column("case_results", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_uc1_eval_runs_agent_id", "uc1_eval_runs", ["agent_id"])

    op.create_table(
        "uc1_version_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("admin_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("agent_id", "version", "admin_id", name="uq_uc1_version_approvals_admin"),
    )
    op.create_index("ix_uc1_version_approvals_agent_id", "uc1_version_approvals", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_uc1_version_approvals_agent_id", table_name="uc1_version_approvals")
    op.drop_table("uc1_version_approvals")
    op.drop_index("ix_uc1_eval_runs_agent_id", table_name="uc1_eval_runs")
    op.drop_table("uc1_eval_runs")
