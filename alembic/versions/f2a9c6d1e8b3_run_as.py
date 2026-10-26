"""run as — who a trial run or an eval case asks as

Revision ID: f2a9c6d1e8b3
Revises: e1c7a3b95d24
Create Date: 2026-10-26 00:00:00.000000

DS-05 / ADR-0059 §3: on the platform the asker is whoever the creator picks
("Chạy với tư cách…"). Two rows have to remember that choice:

- `run_approvals.run_as` — a trial that pauses at a `human_approval` node
  resumes as a different request, and its second leg must read data as the
  same person the first leg did;
- `eval_cases.run_as` — a case is a question *asked by someone*, and the
  runner hands that someone to the run.

Both nullable: null is "nobody asking", which every `data_query` read denies.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a9c6d1e8b3"
down_revision: str | Sequence[str] | None = "e1c7a3b95d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_approvals", sa.Column("run_as", sa.String(), nullable=True))
    op.add_column("eval_cases", sa.Column("run_as", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("eval_cases", "run_as")
    op.drop_column("run_approvals", "run_as")
