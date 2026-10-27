"""sandbox live sends — the "Gửi thật" switch of a trial

Revision ID: d6a3b9e2f5c8
Revises: c5f2a8d1e4b7
Create Date: 2026-10-27 00:00:00.000000

DS-09: a trial can be switched to send its approved actions for real.
`run_approvals.live_sends` carries the switch across the pause to the leg
that sends; `agent_runs.live_sends` marks a rehearsal that did send, so a PM
can tell it from a mock (its `trigger` is still `chat_test`).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6a3b9e2f5c8"
down_revision: str | Sequence[str] | None = "c5f2a8d1e4b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_approvals", sa.Column("live_sends", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("agent_runs", sa.Column("live_sends", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_agent_runs_live_sends", "agent_runs", ["live_sends"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_live_sends", table_name="agent_runs")
    op.drop_column("agent_runs", "live_sends")
    op.drop_column("run_approvals", "live_sends")
