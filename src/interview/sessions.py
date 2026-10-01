"""Running an interview: read, fill, ask, repeat — and commit after every turn.

Where the deterministic half (`slots`) and the model half (`extraction`) meet. The
order is the same on both turns a session has, and it is the order that makes the
ticket's promises true:

1. read the new text for every slot that is currently **empty**;
2. fill those slots, never touching one that already holds something;
3. if this turn was an answer, make sure the slot it answered is filled — from the
   creator's own words if the model read nothing out of them;
4. ask about the first slot still empty, or stop because none are.

Step 3 is the one that is easy to miss and load-bearing. Without it a model having
a bad minute could leave the answered slot empty, the same question would come back,
and the interview would loop on a creator who has already answered it — the exact
failure the ticket rules out by asking for a stop that does not depend on the LLM.
With it, answering a question always fills that question's slot, so the number of
empty slots strictly falls and the interview reaches zero in at most as many turns
as there are slots.

Every turn commits. An interview is a thing people abandon halfway — that is the
premise of the resume criterion — so "saved" has to mean "saved as of the last
thing you said", not "saved when you get to the end".
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import InterviewSession
from src.interview.extraction import read_slots
from src.interview.slots import SPEC_OF, Slot, SlotSheet, SlotValue, normalise
from src.services.llm import ChatModelFactory


class InterviewError(ValueError):
    """Something the caller sent cannot be used. Message is user-facing."""


class NoOpenQuestionError(InterviewError):
    """An answer arrived for an interview that has no question open.

    Its own type because it is the one failure here that is not about the body.
    Nothing the caller sends could fix it — the interview is simply finished — so
    it has to reach the HTTP layer as a conflict rather than as an unprocessable
    payload, and a handler cannot tell the two apart from a message.
    """


def sheet_of(session: InterviewSession) -> SlotSheet:
    """The slot sheet this row is holding.

    The single reader of the `slots` column. Everything else asks the sheet, so
    the stored JSON's shape is knowledge that stays inside this module and
    `SlotSheet.from_stored`.
    """
    return SlotSheet.from_stored(session.slots or {})


async def start_interview(
    db: Session,
    *,
    owner_id: int,
    description: str,
    model_factory: ChatModelFactory,
) -> InterviewSession:
    """Begin an interview by reading `description` for everything it already answers.

    The extraction pass happens *before* the first question rather than after, and
    that ordering is the criterion *"mô tả đủ chi tiết thì không hỏi câu nào"*: a
    description covering all five slots produces a session that is already
    complete, and the creator is never asked anything. There is no separate
    "should we skip the interview" decision to get wrong — skipping is what
    happens when nothing is left to ask.
    """
    text = _required(description, "Mô tả nghiệp vụ")
    sheet = SlotSheet.empty()
    found = await read_slots(text, wanted=sheet.pending, model_factory=model_factory)
    sheet = sheet.with_values(found)

    session = InterviewSession(owner_id=owner_id, description=text, slots=sheet.to_stored())
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


async def answer_current_question(
    db: Session,
    session: InterviewSession,
    answer: str,
    *,
    model_factory: ChatModelFactory,
) -> InterviewSession:
    """Answer the question the interview is currently asking, and move on.

    The answer is read for **every** empty slot, not only the one that was asked.
    A creator explaining who the agent is for usually says what it is for in the
    same breath, and re-asking them something they just said is the behaviour this
    ticket exists to prevent.

    Whatever else it fills, the answered slot ends up filled: the creator's raw
    words are stored if extraction read nothing usable out of them. Their own
    sentence is a worse-structured answer than the model's reading of it and a far
    better one than silence — and it is what guarantees this turn made progress.
    """
    asked = sheet_of(session).next_question
    if asked is None:
        raise NoOpenQuestionError("Phỏng vấn đã đủ slot, không còn câu hỏi nào để trả lời")
    text = _required(answer, "Câu trả lời")

    sheet = sheet_of(session)
    found = await read_slots(
        text,
        wanted=sheet.pending,
        model_factory=model_factory,
        description=session.description,
    )
    sheet = sheet.with_values(found)
    sheet = sheet.with_values({asked.slot: _fallback_value(asked.slot, text)})
    return _store(db, session, sheet)


def set_slot(db: Session, session: InterviewSession, slot: Slot, value: object) -> InterviewSession:
    """Replace what a slot holds with what the creator says it should hold.

    The correction path, and the reason the review screen is worth having. A slot
    filled by reading a description is a machine's reading of somebody's words,
    and #13 already made the argument for why a creator who can see such a reading
    is wrong must be able to fix it (ADR-0011 §1) — being shown a misunderstanding
    with no way to correct it is worse than not being shown it.

    The only write that is allowed to overwrite a filled slot, which is why it is a
    deliberate call and not something an extraction pass can reach.
    """
    cleaned = normalise(slot, value)
    if cleaned is None:
        raise InterviewError(f"Slot {SPEC_OF[slot].question!r} không được để trống")
    return _store(db, session, sheet_of(session).without(slot).with_values({slot: cleaned}))


def clear_slot(db: Session, session: InterviewSession, slot: Slot) -> InterviewSession:
    """Empty a slot, putting its question back into the interview.

    The other half of correcting a misreading: a creator who cannot answer a slot
    the way it was read, and would rather be asked again than dictate the whole
    value. Re-opens the question because `pending` is derived from emptiness —
    there is no separate "already asked" record to contradict it.
    """
    return _store(db, session, sheet_of(session).without(slot))


def list_sessions(db: Session, owner_id: int) -> list[InterviewSession]:
    """This creator's interviews, newest first — how an abandoned one is found again."""
    return list(
        db.execute(
            select(InterviewSession)
            .where(InterviewSession.owner_id == owner_id)
            .order_by(InterviewSession.created_at.desc(), InterviewSession.id.desc())
        ).scalars()
    )


def _fallback_value(slot: Slot, text: str) -> SlotValue:
    """The creator's own answer, shaped for `slot`.

    Only ever reached when extraction read nothing — `with_values` discards it the
    moment the model produced something for this slot. A list slot gets a
    one-item list rather than a split on punctuation: guessing where one
    capability ends and the next begins is how a single sentence becomes three
    half-sentences that each read as a separate requirement.
    """
    value = normalise(slot, text)
    if value is None:  # pragma: no cover - `_required` already refused blank text
        raise InterviewError("Câu trả lời không được để trống")
    return value


def _store(db: Session, session: InterviewSession, sheet: SlotSheet) -> InterviewSession:
    """Write `sheet` back to the row and commit, so a creator can walk away now."""
    session.slots = sheet.to_stored()
    db.commit()
    db.refresh(session)
    return session


def _required(value: str, subject: str) -> str:
    """`value` stripped, or the reason the turn cannot be taken.

    Refused here rather than only on the request schema, for the reason
    `src.evals.cases` gives: "an empty description is not an interview" is a fact
    about this domain, and a second caller deserves the same refusal in the same
    words.
    """
    cleaned = value.strip()
    if not cleaned:
        raise InterviewError(f"{subject} không được để trống")
    return cleaned
