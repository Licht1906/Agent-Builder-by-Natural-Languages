"""conversations — the index a chat sidebar is read from

Revision ID: c4a71e83f6d2
Revises: b3e91d47c520
Create Date: 2026-08-16 10:20:00.000000

ADR-0004 put what was said in LangGraph's checkpoint tables and stopped there,
because #6 only ever needed to answer "what happened on *this* thread". A
sidebar asks the other question — "which threads are mine" — and a checkpoint
key is one opaque string with no owner column to group on.

This table is that index and nothing more: no message text, no duplication of
the transcript. Dropping it loses the way back into a conversation, not the
conversation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4a71e83f6d2"
down_revision: str | Sequence[str] | None = "b3e91d47c520"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out rather than reflected off the ORM enum: a migration is a
#: statement about the database at one moment, and one that read today's Python
#: would quietly change meaning the day the enum gains a member.
#:
#: `create_type=False` because `threadnamespace` is not this migration's to
#: create — `d4a91c603b7e` brought it in for `run_costs`, and a second CREATE
#: TYPE is a hard failure rather than a no-op. The `create(checkfirst=True)`
#: below covers the other order, where a database reached this revision without
#: ever having had the type.
_NAMESPACE = postgresql.ENUM("PRODUCTION", "TRIAL", name="threadnamespace", create_type=False)


def upgrade() -> None:
    """Create `conversations`, its uniqueness rule, and the index the sidebar reads."""
    sa.Enum("PRODUCTION", "TRIAL", name="threadnamespace").create(op.get_bind(), checkfirst=True)
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("namespace", _NAMESPACE, nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "user_id", "conversation_id", "namespace", name="uq_conversations_thread"),
    )
    op.create_index("ix_conversations_owner_recent", "conversations", ["agent_id", "user_id", "last_message_at"])


def downgrade() -> None:
    """Drop the index table.

    The `threadnamespace` enum is deliberately left alone: this migration did not
    create it, and dropping a type two other tables still use would take them
    with it.
    """
    op.drop_index("ix_conversations_owner_recent", table_name="conversations")
    op.drop_table("conversations")
