"""run outcome and namespace on run_costs

Revision ID: d4a91c603b7e
Revises: a3f6c81b7d24
Create Date: 2026-08-10 14:00:00.000000

Two facts the ledger already had at write time and threw away (ticket #55,
ADR-0039 §3). Both exist because the operations dashboard asks questions
`run_costs` could not answer, and neither is a new logging mechanism — they are
columns on the table ADR-0009 §4 already made the record of every run.

`error` is how a run ended, in the runtime's own words, or NULL if it ended well.
Deliberately not where a budget stop is recorded: a run cut off by its own
ceiling produced real output and said why it stopped (ADR-0008 §3).

`namespace` is which world the run's conversation belonged to. `sandbox` already
says whether the *tools* were real, and the two come apart — a `trial` chat turn
on a deployed version calls real tools — so a dashboard filtering on `sandbox`
alone would count its owner's rehearsals as traffic.

Existing rows are backfilled `prod`, which is what they were: `record_run_cost`
was only ever called from the two production doors and the workflow chat, and the
one genuinely-trial writer (`src.agents.sandbox`) has always written
`sandbox = true` — those rows are excluded from the dashboard by that flag
whatever this column says.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a91c603b7e"
down_revision: str | Sequence[str] | None = "a3f6c81b7d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The enum `ThreadNamespace` maps to. Created here rather than inferred from the
#: column, so the downgrade has something to drop by name.
_NAMESPACE = sa.Enum("PRODUCTION", "TRIAL", name="threadnamespace")


def upgrade() -> None:
    """Add `error` and `namespace` to `run_costs`, backfilling the latter."""
    op.add_column("run_costs", sa.Column("error", sa.String(), nullable=True))
    _NAMESPACE.create(op.get_bind(), checkfirst=True)
    # Added nullable, filled, then made NOT NULL: a table with rows cannot take a
    # NOT NULL column in one step, and a server default left behind would let a
    # future writer omit the value and have it guessed at.
    op.add_column("run_costs", sa.Column("namespace", _NAMESPACE, nullable=True))
    op.execute("UPDATE run_costs SET namespace = 'PRODUCTION'")
    op.alter_column("run_costs", "namespace", nullable=False)


def downgrade() -> None:
    """Drop both columns and the enum type `namespace` owns."""
    op.drop_column("run_costs", "namespace")
    op.drop_column("run_costs", "error")
    _NAMESPACE.drop(op.get_bind(), checkfirst=True)
