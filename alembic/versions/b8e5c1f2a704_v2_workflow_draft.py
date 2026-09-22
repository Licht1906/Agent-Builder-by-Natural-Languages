"""v2 workflow draft

Revision ID: b8e5c1f2a704
Revises: d9c14b7f2a63
Create Date: 2026-08-08 22:10:00.000000

V2's second structure lands (ticket #43, ADR-0021/0022/0025): the Workflow
Generator reads a confirmed Agent Solution Specification, picks a template from
the catalog, fills its placeholders, and leaves a graph waiting for the creator.

Two changes, and they are the same change seen twice:

1. `agent_versions` gains `workflow` — the `WorkflowJSON` document. A third
   nullable document column beside `spec` and `solution_spec`, because a version
   holds whichever documents its stage has reached and a row at
   `spec_confirmed` has no graph yet. Writing an empty one to keep the column
   full would be a workflow claiming an agent that cannot run, the same argument
   that made `spec` nullable one migration ago.
2. `versionstatus` gains `workflow_pending_confirmation`, the stage that
   document sits at while the creator edits it on the canvas.

`workflow_confirmed` is **not** added here. It is the transition the Workflow
Canvas performs (#44) behind the graph validator, and an enum value nothing can
reach is a stage the platform advertises and cannot deliver — the migration that
adds the transition is the one that should add the value.

No index and no constraint on `workflow`. Every read of it is by version id,
which is already the primary key, and everything the document has to satisfy is
enforced by the validator at confirm time rather than by the column — a database
constraint here would be the "draft saves are unchecked" promise (ADR-0022 §6)
refused one layer down.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8e5c1f2a704"
down_revision: str | Sequence[str] | None = "d9c14b7f2a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: SQLAlchemy stores an enum's *member name*, not its value — so
#: `WORKFLOW_PENDING_CONFIRMATION`, matching how every earlier migration wrote
#: the statuses beside it.
_NEW_STATUS = "WORKFLOW_PENDING_CONFIRMATION"


def upgrade() -> None:
    """Add the workflow document and the stage it waits at."""
    # `ADD VALUE` is permitted inside a transaction from PostgreSQL 12 as long as
    # the new value is not *used* in the same one — nothing below writes a status.
    op.execute(f"ALTER TYPE versionstatus ADD VALUE IF NOT EXISTS '{_NEW_STATUS}'")
    op.add_column("agent_versions", sa.Column("workflow", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drop the workflow column.

    **Destructive**: a drafted graph exists nowhere else. The version keeps its
    confirmed `solution_spec`, so re-running the Generator reproduces a draft —
    but not the edits a creator made to it on the canvas.

    The enum value is deliberately left behind. PostgreSQL cannot remove a value
    from an enum type, and rebuilding `versionstatus` to drop one would mean
    rewriting a column every other table's foreign keys hang off — a far larger
    risk than one unused label. Any row still sitting at the dropped status is
    left where it is: its status is now a stage with no document behind it,
    which the readers refuse by name rather than by crashing.
    """
    op.drop_column("agent_versions", "workflow")
