"""The list of chat windows a person can go back into — what the sidebar reads.

ADR-0004 stored what was said and deliberately nothing about *which* threads
exist: #6 only had to answer "what happened here", and a checkpoint key answers
that on its own. A sidebar asks the question that key cannot — "which of these
are mine, and what were they about" — so this module keeps a small index beside
the checkpointer rather than trying to enumerate it.

Two rules hold the index and the transcript together:

- **The index never holds message text.** `title` is a label; the conversation
  itself is read back through `Conversation.transcript()` from the checkpoint.
  Copying turns into this table would create a second account of what was said,
  and the two would disagree the first time a write half-failed.
- **A row is written by the same call that writes the turn.** `record_turn` runs
  on every message, so a conversation appears in the sidebar because it actually
  happened, not because something announced that it would.

Ownership is `(agent_id, user_id)` and never the `caller` string of ADR-0036 §6.
The two agree for a signed-in person and come apart for a Deployment's API key,
which is a caller with no sidebar — an integration's threads leave no row here
on purpose.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.agents.threads import ThreadNamespace, ThreadRef
from src.db.models import Conversation

#: How much of the first message becomes the label. Long enough to tell two
#: conversations apart at a glance, short enough not to wrap in a narrow column.
TITLE_LENGTH = 60

#: What a conversation is called before anybody has said anything in it, and the
#: fallback for a first message that is all whitespace or all emoji.
UNTITLED = "Cuộc trò chuyện mới"


def title_from(message: str) -> str:
    """`message` as a sidebar label.

    Truncated on a word boundary where there is one near the end, because a
    label cut mid-word reads as a rendering bug rather than as an abbreviation.
    Newlines collapse to spaces: a pasted multi-line question is one label.
    """
    collapsed = " ".join(message.split())
    if not collapsed:
        return UNTITLED
    if len(collapsed) <= TITLE_LENGTH:
        return collapsed
    cut = collapsed[:TITLE_LENGTH]
    spaced = cut.rsplit(" ", 1)[0]
    # Only honour the word boundary if it keeps most of the label; a first
    # "word" longer than the limit would otherwise truncate to nothing.
    return f"{spaced if len(spaced) >= TITLE_LENGTH // 2 else cut}…"


def record_turn(db: Session, *, agent_id: int, user_id: int, thread: ThreadRef, message: str) -> Conversation:
    """File this turn under its conversation, creating the row on the first one.

    Idempotent per thread by the table's unique constraint, so the hundredth
    message on a thread moves `last_message_at` rather than adding a row.

    The title is set once, from the first message, and never rewritten by a
    later turn — a person renames a conversation to make it findable, and a
    title that drifted back to whatever was said most recently would undo that.

    Committed by the caller. This runs inside the request that is already
    writing the turn, and a commit here would split one action across two
    transactions: a conversation could then appear in the sidebar pointing at a
    thread whose turn never landed.

    Both timestamps come from `func.now()` — the database's clock, never
    Python's. `created_at` already had to, being a server default, and a
    `last_message_at` stamped in the application would be read against it on
    every sort. Two clocks that disagree by a timezone would order the list by
    which process happened to write each row.
    """
    existing = db.scalar(
        select(Conversation).where(
            Conversation.agent_id == agent_id,
            Conversation.user_id == user_id,
            Conversation.conversation_id == thread.conversation_id,
            Conversation.namespace == thread.namespace,
        )
    )
    if existing is not None:
        existing.last_message_at = func.now()
        return existing

    conversation = Conversation(
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=thread.conversation_id,
        namespace=thread.namespace,
        title=title_from(message),
    )
    db.add(conversation)
    return conversation


def list_for(
    db: Session, *, agent_id: int, user_id: int, namespace: ThreadNamespace = ThreadNamespace.PRODUCTION
) -> list[Conversation]:
    """This person's conversations on this agent, most recently used first.

    Scoped by `user_id` in the query rather than filtered after: the sidebar is
    the one screen where somebody else's conversation appearing would be a
    privacy incident, and a WHERE clause cannot be forgotten downstream the way
    a post-filter can.
    """
    return list(
        db.scalars(
            select(Conversation)
            .where(
                Conversation.agent_id == agent_id,
                Conversation.user_id == user_id,
                Conversation.namespace == namespace,
            )
            .order_by(Conversation.last_message_at.desc(), Conversation.id.desc())
        )
    )


def find(
    db: Session,
    *,
    agent_id: int,
    user_id: int,
    conversation_id: str,
    namespace: ThreadNamespace = ThreadNamespace.PRODUCTION,
) -> Conversation | None:
    """One indexed conversation, or `None` if this person has no such row.

    `None` rather than an exception because both callers treat "not indexed" as
    an ordinary answer: reading falls through to an empty transcript, and
    renaming a conversation that is not yours must look exactly like renaming
    one that does not exist.
    """
    return db.scalar(
        select(Conversation).where(
            Conversation.agent_id == agent_id,
            Conversation.user_id == user_id,
            Conversation.conversation_id == conversation_id,
            Conversation.namespace == namespace,
        )
    )
