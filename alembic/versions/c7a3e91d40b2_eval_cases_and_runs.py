"""eval cases and runs

Revision ID: c7a3e91d40b2
Revises: b2f4a71c9d38
Create Date: 2026-08-02 14:10:00.000000

The two tables behind eval (ticket #13, ADR-0011).

`eval_cases` is the editable half: a test set generated from the success
criteria the creator declared at interview time, which they may then correct,
extend or throw away before anything is graded against it.

`eval_runs` is the recorded half. Its `results` column is a snapshot of every
case as it was at the moment it ran, not a set of foreign keys back into
`eval_cases` — the cases are editable by design, and a result pointing at a case
that has since been rewritten would read as evidence about a test that never
happened. Same reasoning as `sandbox_runs.transcript`.

Both cascade from `agent_versions` for the reason a transcript does: a test set
and its scores are statements about one immutable spec, and say nothing once
that spec no longer exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7a3e91d40b2"
down_revision: str | Sequence[str] | None = "b2f4a71c9d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `eval_cases` and `eval_runs`."""
    op.create_table(
        "eval_cases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("criterion", sa.String(), nullable=False),
        sa.Column("input_message", sa.String(), nullable=False),
        sa.Column("expected", sa.String(), nullable=False),
        sa.Column("origin", sa.Enum("GENERATED", "MANUAL", name="evalcaseorigin"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["agent_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_eval_cases_version_id"), "eval_cases", ["version_id"])

    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        # The bar as it stood when this run happened. The approval gate reads the
        # live configuration instead (ADR-0011 §5); this column is what lets a
        # reader of an old run see what it was measured against.
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("passed_count", sa.Integer(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["agent_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_eval_runs_version_id"), "eval_runs", ["version_id"])


def downgrade() -> None:
    """Drop both tables and the enum type `eval_cases` owns."""
    op.drop_index(op.f("ix_eval_runs_version_id"), table_name="eval_runs")
    op.drop_table("eval_runs")
    op.drop_index(op.f("ix_eval_cases_version_id"), table_name="eval_cases")
    op.drop_table("eval_cases")
    # Postgres ENUM types outlive their column — drop_table doesn't drop them.
    sa.Enum(name="evalcaseorigin").drop(op.get_bind(), checkfirst=True)
