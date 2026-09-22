"""approval decisions

Revision ID: e51c8a97b3d4
Revises: c7a3e91d40b2
Create Date: 2026-08-02 16:40:00.000000

The audit trail behind the approval queue (ticket #14, ADR-0012).

One row per decision an admin made about a version — approved, rejected, or the
eval bar waived — and nothing ever updates or deletes one. A version that goes
round the loop twice accumulates rows, so the history reads as the story it was.

`eval_score` is a snapshot rather than a pointer into `eval_runs`, for the same
reason `eval_runs.results` snapshots its cases: the approval gate reads the
*latest* eval run, so a creator who evaluates again after an override would
otherwise rewrite what that override was granted against. The one record that
has to survive scrutiny is "this bar was waived, and here is how far short it
was" — it cannot be assembled from rows that moved since.

Cascades from `agent_versions` like every other table that holds evidence about
a version. It does not cascade from `users`: an audit record whose actor
vanished is worse than one naming an account that no longer exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e51c8a97b3d4"
down_revision: str | Sequence[str] | None = "c7a3e91d40b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `approval_decisions`."""
    op.create_table(
        "approval_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("admin_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("APPROVED", "REJECTED", "EVAL_OVERRIDE", name="approvaldecisionkind"),
            nullable=False,
        ),
        # Nullable in the schema, mandatory in the domain for `REJECTED` and
        # `EVAL_OVERRIDE`. A per-kind constraint is not expressible in a column,
        # so `src.approvals.decisions` enforces it before the transition — there
        # is no path to `rejected` that writes a row without one.
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("eval_score", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["agent_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["admin_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_approval_decisions_version_id"), "approval_decisions", ["version_id"])


def downgrade() -> None:
    """Drop the table and the enum type it owns."""
    op.drop_index(op.f("ix_approval_decisions_version_id"), table_name="approval_decisions")
    op.drop_table("approval_decisions")
    # Postgres ENUM types outlive their column — drop_table doesn't drop them.
    sa.Enum(name="approvaldecisionkind").drop(op.get_bind(), checkfirst=True)
