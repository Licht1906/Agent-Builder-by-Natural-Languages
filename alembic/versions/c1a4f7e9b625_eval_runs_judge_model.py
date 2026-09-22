"""eval_runs.judge_model

Revision ID: c1a4f7e9b625
Revises: b7e4a92f5c31
Create Date: 2026-08-23 11:00:00.000000

ADR-0054 §4 (ticket EVAL-3). `src.evals.judge.judge_answer` used to grade every
case on `meta_agent_model()` — the same model the meta-agent writes specs and
graphs with, against the lecture's own advice that a judge should be a
different family than the generator it grades (s52). `Settings.judge_model`
(default `META_AGENT_MODEL`, so this ticket changes no behaviour on its own)
gives that a knob; this column is where each run remembers which model it was
graded by, so a later `src.pm.reports.version_report` can refuse to diff a run
against a baseline scored under a different one.

Backfilled to the literal `META_AGENT_MODEL` value rather than left blank:
every row already in this table really was judged by that model — it is what
`meta_agent_model()` has always resolved to — so recording it is not a guess,
unlike ADR-0054 §1's `origin=promoted` backfill, which is why this migration
does not follow that one's "leave it be" caution.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1a4f7e9b625"
down_revision: str | Sequence[str] | None = "b7e4a92f5c31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `src.models.model_catalog.META_AGENT_MODEL`, copied rather than imported —
#: a migration must read the same on every future checkout regardless of what
#: that constant is renamed or repointed to later.
META_AGENT_MODEL = "gpt-4.1-2025-04-14"


def upgrade() -> None:
    op.add_column("eval_runs", sa.Column("judge_model", sa.String(), nullable=True))
    op.execute(f"UPDATE eval_runs SET judge_model = '{META_AGENT_MODEL}' WHERE judge_model IS NULL")
    op.alter_column("eval_runs", "judge_model", nullable=False, server_default="")


def downgrade() -> None:
    op.drop_column("eval_runs", "judge_model")
