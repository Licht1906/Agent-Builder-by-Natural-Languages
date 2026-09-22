"""v2 workflow confirmed

Revision ID: c4f7a91d3e28
Revises: b8e5c1f2a704
Create Date: 2026-08-08 23:40:00.000000

The stage the Workflow Canvas moves a graph to (ticket #44, ADR-0022 §6). The
migration one back deliberately left it out — "an enum value nothing can reach
is a stage the platform advertises and cannot deliver" — and this is the
migration that arrives with the transition, so the value and the code path that
reaches it land together.

One value, no column. `workflow_confirmed` is a promise about the document
already in `agent_versions.workflow`: from here it is immutable, and an edit
forks a new version rather than overwriting it. That promise is enforced by
`src.workflow.drafts`, which is the only writer, rather than by a constraint
here — the same reasoning that kept a `CHECK` off the `workflow` column, since
the rule being enforced is about *transitions between rows*, not about the shape
of any one of them.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f7a91d3e28"
down_revision: str | Sequence[str] | None = "b8e5c1f2a704"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: SQLAlchemy stores an enum's *member name*, not its value — so
#: `WORKFLOW_CONFIRMED`, matching how every earlier migration wrote the statuses
#: beside it.
_NEW_STATUS = "WORKFLOW_CONFIRMED"


def upgrade() -> None:
    """Add the stage a confirmed graph sits at."""
    # `ADD VALUE` is permitted inside a transaction from PostgreSQL 12 as long as
    # the new value is not *used* in the same one — nothing below writes a status.
    op.execute(f"ALTER TYPE versionstatus ADD VALUE IF NOT EXISTS '{_NEW_STATUS}'")


def downgrade() -> None:
    """Nothing to undo, and that is not an omission.

    PostgreSQL cannot remove a value from an enum type, and rebuilding
    `versionstatus` to drop one would mean rewriting a column every other
    table's foreign keys hang off — a far larger risk than one unused label
    (the argument the previous migration already made).

    A version left sitting at `workflow_confirmed` after a downgrade keeps its
    workflow document and its status. What it loses is the code that treats that
    status as immutable, so the canvas would offer to edit a graph the creator
    had already frozen. Downgrading past this point is therefore a decision
    about *in-flight versions*, not only about a schema.
    """
