"""tool_registrations.allowed_actions — this platform's `allowed_tools`

Revision ID: a7f31c9e60b4
Revises: 9d57f1cfe7df
Create Date: 2026-08-19 15:40:00.000000

OpenAI's Agent Builder puts an `allowed_tools` list on its MCP node: of
everything the server said it can do, only these are offered. This column is
that idea landed where this platform can enforce it — on the *connection*, not
on each node, because an `MCP` node here names exactly one action (ADR-0021 §2)
and the question "which of this server's tools may be used at all" wants one
answer an admin reviewed once.

Empty is not "nothing allowed", it is "no narrowing" — every action
`tools/list` reported. That is what makes this migration safe to run against a
registry that already has rows: a `server_default` of `'[]'` leaves every
existing connection offering exactly what it offered yesterday.

`actions` itself is untouched. It stays the verbatim report from the server
(ADR-0023 §7), so an admin reviewing a narrowed connection can still see what
was left out and what it was.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7f31c9e60b4"
down_revision: str | Sequence[str] | None = "9d57f1cfe7df"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column, filled with the empty list every existing row means."""
    op.add_column(
        "tool_registrations",
        sa.Column("allowed_actions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    """Drop it.

    Lossy on purpose, and worth saying out loud: a connection that had been
    narrowed to three of its twelve actions comes back offering all twelve. The
    alternative — refusing to downgrade while any row is narrowed — would leave
    an operator unable to roll back a deploy because of a creator's setting.
    """
    op.drop_column("tool_registrations", "allowed_actions")
