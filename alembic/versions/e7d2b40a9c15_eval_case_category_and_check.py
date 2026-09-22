"""eval case category and expected check

Revision ID: e7d2b40a9c15
Revises: a1c7d5e40b93
Create Date: 2026-08-09 12:00:00.000000

The two columns V2's automatic evaluation needs (ticket #48, ADR-0028 §2). A
case generated from an `AgentSolutionSpecification` knows which of requirement
§7's six checks grades it, and for the three that are read off the run's own
transcript it carries the rule to read rather than a sentence for a judge.

Both nullable, and that is the migration's whole substance: every `eval_cases`
row written before this — every V1 case, generated from a bare
`slots.success_criteria` string — has no category to state and no rule to apply,
and is still graded exactly as ADR-0011 has always graded it. A backfill would
have to invent a category for sentences nobody classified, which is the one thing
ADR-0020 §1 built the enum to avoid.

`category` reuses `successcriterioncategory`, the type
`agent_versions.solution_spec` already implies rather than owns — the spec is
stored as JSON, so this column is the first place the six values become a
database type. That is why the enum is created here and dropped again on the way
down.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7d2b40a9c15"
down_revision: str | Sequence[str] | None = "a1c7d5e40b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The six of `SuccessCriterionCategory` (ADR-0020 §1), spelled as SQLAlchemy
#: spells a `StrEnum` member — by *name*, which is what the ORM column stores.
_CATEGORY = sa.Enum(
    "WORKFLOW_SUCCESS",
    "TOOL_CORRECTNESS",
    "HUMAN_APPROVAL_COMPLIANCE",
    "SOURCE_GROUNDEDNESS",
    "CORRECTNESS",
    "RELEVANCE",
    name="successcriterioncategory",
)


def upgrade() -> None:
    """Add `category` and `expected_check` to `eval_cases`."""
    _CATEGORY.create(op.get_bind(), checkfirst=True)
    op.add_column("eval_cases", sa.Column("category", _CATEGORY, nullable=True))
    op.add_column("eval_cases", sa.Column("expected_check", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop both columns and the enum type the first of them owns."""
    op.drop_column("eval_cases", "expected_check")
    op.drop_column("eval_cases", "category")
    # Postgres ENUM types outlive their column — drop_column doesn't drop them.
    sa.Enum(name="successcriterioncategory").drop(op.get_bind(), checkfirst=True)
