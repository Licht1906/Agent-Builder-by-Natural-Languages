"""eval suite changes — retired cases and the suite's history

Revision ID: f5c3a8e2b6d9
Revises: e2b7c5a9d3f1
Create Date: 2026-10-31 00:00:00.000000

EV-05 / ADR-0069 §5–6: a case whose spec or graph moved on is retired, not
deleted; every retirement, edit, deletion and triage is written down.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5c3a8e2b6d9"
down_revision: str | Sequence[str] | None = "e2b7c5a9d3f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND = sa.Enum("RETIRED", "EDITED", "DELETED", "TRIAGED", name="suitechangekind")
_VERDICT = sa.Enum("AGENT_WRONG", "CASE_WRONG", name="triageverdict")


def upgrade() -> None:
    op.add_column(
        "eval_cases",
        sa.Column(
            "retired_in_version_id",
            sa.Integer(),
            sa.ForeignKey("agent_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("eval_cases", sa.Column("retired_reason", sa.String(), nullable=True))
    op.add_column("eval_cases", sa.Column("retired_at", sa.DateTime(), nullable=True))
    op.add_column(
        "eval_cases",
        sa.Column("retired_failing_at_baseline", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # `create_table` creates both enum types itself.
    op.create_table(
        "eval_suite_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("suite_id", sa.Integer(), sa.ForeignKey("eval_suites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=True),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("kind", _KIND, nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("verdict", _VERDICT, nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_eval_suite_changes_suite_id", "eval_suite_changes", ["suite_id"])


def downgrade() -> None:
    op.drop_index("ix_eval_suite_changes_suite_id", table_name="eval_suite_changes")
    op.drop_table("eval_suite_changes")
    _VERDICT.drop(op.get_bind(), checkfirst=True)
    _KIND.drop(op.get_bind(), checkfirst=True)
    op.drop_column("eval_cases", "retired_failing_at_baseline")
    op.drop_column("eval_cases", "retired_at")
    op.drop_column("eval_cases", "retired_reason")
    op.drop_column("eval_cases", "retired_in_version_id")
