"""eval_suites.uc1_agent_id — UC-1's own suite identity

Revision ID: d2f7a93c1b56
Revises: c1a4f7e9b625
Create Date: 2026-08-23 12:00:00.000000

ADR-0054 §6 (ticket EVAL-7 / #157). `eval_suites.agent_id` (EVAL-1 /
b7e4a92f5c31) is a `ForeignKey("agents.id")` — the V2 identity space
`agents`/`agent_versions` live in. UC-1 has no row there: its own agent
identity is `AgentSpec.metadata.id`, a string such as `agt_ar_assistant`
(`uc1_agent_spec_versions.agent_id`), a wholly separate space per ADR-0046
("UC-1's architecture ... uses a different vocabulary ... not yet
reconciled"). Minting a fake V2 `agents` row to hang a suite off of would put
a UC-1 agent in a V2 creator's own agent list — worse than the gap it closes.

This migration widens `eval_suites` instead of reconciling the two identity
spaces (out of this ticket's scope): `agent_id` becomes nullable, a second
nullable `uc1_agent_id` column is added, and a CHECK constraint requires
exactly one of the two set per row. `eval_cases.suite_id` needs no change —
a case belongs to a suite regardless of which pipeline the suite's agent is
in.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2f7a93c1b56"
down_revision: str | Sequence[str] | None = "c1a4f7e9b625"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("eval_suites", sa.Column("uc1_agent_id", sa.String(), nullable=True))
    op.alter_column("eval_suites", "agent_id", nullable=True)
    op.create_unique_constraint("uq_eval_suites_uc1_agent_id", "eval_suites", ["uc1_agent_id"])
    op.create_check_constraint(
        "ck_eval_suites_exactly_one_agent",
        "eval_suites",
        "(agent_id IS NOT NULL) <> (uc1_agent_id IS NOT NULL)",
    )


def downgrade() -> None:
    """Reverse — safe as long as no UC-1 suite exists yet; a real one would
    violate `agent_id`'s `NOT NULL` this restores and abort the migration,
    the same guard every other nullable-widening downgrade in this repo
    leaves to Postgres rather than pre-checking in Python.
    """
    op.drop_constraint("ck_eval_suites_exactly_one_agent", "eval_suites", type_="check")
    op.drop_constraint("uq_eval_suites_uc1_agent_id", "eval_suites", type_="unique")
    op.alter_column("eval_suites", "agent_id", nullable=False)
    op.drop_column("eval_suites", "uc1_agent_id")
