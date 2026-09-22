"""eval_suites — the test set moves from version to agent

Revision ID: b7e4a92f5c31
Revises: c4d9e8f2a1b7
Create Date: 2026-08-23 09:00:00.000000

ADR-0054 §1 (ticket EVAL-1). `eval_cases.version_id` used to be the case's
scope: a new version from `create_draft` got an empty test set, and the
Baseline·Candidate·Delta report (ADR-0053) could only match two versions'
cases by `(input_message, criterion)` string equality — two independently
LLM-generated sets almost never collide, so `_regressions` was reporting
almost nothing.

`eval_suites` is the fix: one suite per agent (MVP — `UNIQUE(agent_id)`), and
a case belongs to the suite, not to the version that happened to create it.
`version_id` is renamed `introduced_in_version_id` — still useful provenance,
no longer the scope key — and widened nullable, because a suite case can now
outlive the version that introduced it.

Four more columns land at the same time because they are the same migration
in ADR-0054's own accounting: `origin` gains `uploaded`/`promoted`/`grid`
(EVAL-2/EVAL-6/EVAL-4 write them; this migration only widens the enum so a
later ticket's `INSERT` does not have to run its own `ALTER TYPE`), `tags`
gives the creator a way to filter their own suite, `extra` is where an
uploaded row's foreign columns land (UC-1's `principal`, `retrieval_context`),
and `cell_key`/`zone` are the two the scenario-grid ticket (EVAL-4) will fill
in on `origin=grid` cases — nullable and untouched by everything else.

Backfill order matters: the suite rows and `suite_id` backfill both read the
*old* `version_id` before it is renamed away, via `agent_versions.agent_id` —
exactly ADR-0054's "suite_id backfill qua agent_versions.agent_id". `origin`
itself is left exactly as it was on every existing row: ADR-0054 §1 is
explicit that `promoted` is only stamped where the migration is *sure* a case
came from `promote_run_to_case`, and "not sure" here means every row already
in the table, since nothing before this migration ever set anything but
`generated`/`manual`. Guessing would be worse than leaving it be.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e4a92f5c31"
down_revision: str | Sequence[str] | None = "c4d9e8f2a1b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: MVP default suite name (ADR-0054 §1: "một suite mặc định mỗi agent"). Used
#: both by the backfill below and by `src.evals.cases.get_or_create_suite`.
DEFAULT_SUITE_NAME = "Bộ đề mặc định"


def upgrade() -> None:
    """Create `eval_suites`, move `eval_cases` onto it, widen `origin`, add `eval_runs.skipped`."""
    op.create_table(
        "eval_suites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", name="uq_eval_suites_agent_id"),
    )

    # Three more origins than `c7a3e91d40b2` gave the type. `IF NOT EXISTS`
    # each, same reason `d5a83c2b17e0` uses it: a database that already has
    # one of these (a developer testing this migration twice) upgrades clean.
    op.execute("ALTER TYPE evalcaseorigin ADD VALUE IF NOT EXISTS 'UPLOADED'")
    op.execute("ALTER TYPE evalcaseorigin ADD VALUE IF NOT EXISTS 'PROMOTED'")
    op.execute("ALTER TYPE evalcaseorigin ADD VALUE IF NOT EXISTS 'GRID'")

    op.add_column("eval_cases", sa.Column("suite_id", sa.Integer(), nullable=True))
    op.add_column("eval_cases", sa.Column("tags", sa.JSON(), nullable=True))
    op.add_column("eval_cases", sa.Column("extra", sa.JSON(), nullable=True))
    op.add_column("eval_cases", sa.Column("cell_key", sa.String(), nullable=True))
    op.add_column("eval_cases", sa.Column("zone", sa.String(), nullable=True))

    # One suite per agent that has ever had a case, read through the version
    # every existing case still points at (renamed away two steps down).
    op.execute(
        f"""
        INSERT INTO eval_suites (agent_id, name, created_at, updated_at)
        SELECT DISTINCT av.agent_id, '{DEFAULT_SUITE_NAME}', now(), now()
        FROM eval_cases ec JOIN agent_versions av ON av.id = ec.version_id
        """
    )
    op.execute(
        """
        UPDATE eval_cases ec SET suite_id = es.id
        FROM agent_versions av, eval_suites es
        WHERE av.id = ec.version_id AND es.agent_id = av.agent_id
        """
    )
    op.alter_column("eval_cases", "suite_id", nullable=False)
    op.create_foreign_key(
        "fk_eval_cases_suite_id_eval_suites", "eval_cases", "eval_suites", ["suite_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index(op.f("ix_eval_cases_suite_id"), "eval_cases", ["suite_id"])

    # `tags` defaults to an empty list rather than staying nullable: every
    # reader of it (the filter bar, EVAL-5) wants a list to iterate, never a
    # null to check for first.
    op.execute("UPDATE eval_cases SET tags = '[]'::json WHERE tags IS NULL")
    op.alter_column("eval_cases", "tags", nullable=False)

    # `version_id` -> `introduced_in_version_id`: same column, same values
    # (a rename preserves them), now nullable and `SET NULL` on delete — a
    # suite case is no longer scoped to the version that introduced it, so
    # that version disappearing must not take the case with it.
    op.drop_constraint("eval_cases_version_id_fkey", "eval_cases", type_="foreignkey")
    op.drop_index(op.f("ix_eval_cases_version_id"), table_name="eval_cases")
    op.alter_column("eval_cases", "version_id", new_column_name="introduced_in_version_id", nullable=True)
    op.create_foreign_key(
        "fk_eval_cases_introduced_in_version_id_agent_versions",
        "eval_cases",
        "agent_versions",
        ["introduced_in_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_eval_cases_introduced_in_version_id"), "eval_cases", ["introduced_in_version_id"])

    # `eval_runs.skipped[]` — ADR-0054 §1's `ungrounded` cases, recorded
    # beside `results[]` rather than folded into it: a skipped case never ran,
    # so it has no answer, no verdict, nothing `EvalCaseResult` promises.
    op.add_column("eval_runs", sa.Column("skipped", sa.JSON(), nullable=True))
    op.execute("UPDATE eval_runs SET skipped = '[]'::json WHERE skipped IS NULL")
    op.alter_column("eval_runs", "skipped", nullable=False)


def downgrade() -> None:
    """Reverse every column and table this migration added.

    The three new `origin` values are left in the enum. Postgres has no `DROP
    VALUE`, and rebuilding the type without them would fail on any row a
    downgraded-from feature already wrote — the same call `d5a83c2b17e0`
    makes and the same reason.
    """
    op.drop_column("eval_runs", "skipped")

    op.drop_index(op.f("ix_eval_cases_introduced_in_version_id"), table_name="eval_cases")
    op.drop_constraint("fk_eval_cases_introduced_in_version_id_agent_versions", "eval_cases", type_="foreignkey")
    op.alter_column("eval_cases", "introduced_in_version_id", new_column_name="version_id", nullable=False)
    op.create_foreign_key(
        "eval_cases_version_id_fkey", "eval_cases", "agent_versions", ["version_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index(op.f("ix_eval_cases_version_id"), "eval_cases", ["version_id"])

    op.drop_index(op.f("ix_eval_cases_suite_id"), table_name="eval_cases")
    op.drop_constraint("fk_eval_cases_suite_id_eval_suites", "eval_cases", type_="foreignkey")
    op.drop_column("eval_cases", "suite_id")
    op.drop_column("eval_cases", "zone")
    op.drop_column("eval_cases", "cell_key")
    op.drop_column("eval_cases", "extra")
    op.drop_column("eval_cases", "tags")

    op.drop_table("eval_suites")
