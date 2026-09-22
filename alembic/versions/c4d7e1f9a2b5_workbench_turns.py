"""workbench turns — the meta agent's conversation memory (ADR-0050)

Revision ID: c4d7e1f9a2b5
Revises: b8e4f0a2c9d1
Create Date: 2026-08-20 21:20:00.000000

One table: who said what, on which version, with which proposal attached and
which tool trail behind it. Keyed to the agent so the conversation survives
version forks; `workflow_after`/`steps_json` are the evidence half of "the
meta agent is an agent".
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d7e1f9a2b5"
down_revision: str | Sequence[str] | None = "b8e4f0a2c9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = sa.Enum("CREATOR", "META_AGENT", name="workbenchturnrole")


def upgrade() -> None:
    op.create_table(
        "workbench_turns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "version_id", sa.Integer(), sa.ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("role", _ROLE, nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("question", sa.String(), nullable=True),
        sa.Column("workflow_after", sa.JSON(), nullable=True),
        sa.Column("steps_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_workbench_turns_agent_id", "workbench_turns", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_workbench_turns_agent_id", table_name="workbench_turns")
    op.drop_table("workbench_turns")
    _ROLE.drop(op.get_bind(), checkfirst=True)
