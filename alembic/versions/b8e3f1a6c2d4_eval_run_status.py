"""eval run status — a run is written when asked for and finished in the background

Revision ID: b8e3f1a6c2d4
Revises: a7d2c4e9f1b3
Create Date: 2026-10-29 00:00:00.000000

EV-01 / ADR-0069 §7: `POST …/workflow-eval-runs` answers with a `running`
row at once; a background job fills it in. Every run written before this was
finished when it was written, so existing rows are `COMPLETED` with every
case done.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e3f1a6c2d4"
down_revision: str | Sequence[str] | None = "a7d2c4e9f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = sa.Enum("RUNNING", "COMPLETED", "FAILED", name="evalrunstatus")


def upgrade() -> None:
    _STATUS.create(op.get_bind(), checkfirst=True)
    op.add_column("eval_runs", sa.Column("status", _STATUS, nullable=False, server_default="COMPLETED"))
    op.add_column("eval_runs", sa.Column("done_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("eval_runs", sa.Column("error", sa.String(), nullable=True))
    op.execute("UPDATE eval_runs SET done_count = total_count")


def downgrade() -> None:
    op.drop_column("eval_runs", "error")
    op.drop_column("eval_runs", "done_count")
    op.drop_column("eval_runs", "status")
    _STATUS.drop(op.get_bind(), checkfirst=True)
