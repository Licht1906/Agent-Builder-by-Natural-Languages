"""`run_approvals.caller` — the third part of a production thread's address

Revision ID: 66bab42a57ec
Revises: b3c8e47a9d15
Create Date: 2026-08-22 12:05:00.000000

`_identity_of` rebuilt a paused run's thread from `(agent_id, conversation_id,
namespace)` and said so in its own docstring, with a warning attached:

    Whoever gives a workflow run a production namespace has to add that column
    in the same change, or a resumed run will address a different conversation
    from the one it paused in.

`POST /agents/{id}/deployed-workflow-chat` gave workflow runs a production
namespace, and production threads are addressed **per caller** so that two
end-users of one Deployment never land in each other's history. Without the
column the resumed leg addressed a thread with no caller — a different thread —
found no checkpoint there, and replayed the graph from its first node instead
of carrying on from the pause.

Nullable, and no backfill: `NULL` is the honest value for every row already
there. A trial run has no caller by design, and a production row written before
this column existed cannot have its caller recovered — the address it needs is
gone, and inventing one would point the resume at a thread nobody talked on.
Those rows resume the way they always did; new ones resume correctly.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "66bab42a57ec"
down_revision: str | Sequence[str] | None = "b3c8e47a9d15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_approvals", sa.Column("caller", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("run_approvals", "caller")
