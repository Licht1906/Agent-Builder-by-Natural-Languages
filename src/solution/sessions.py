"""Running a V2 interview: read everything, settle what is known, ask the next thing.

Where the model's half (`designer`) and the arithmetic half (`draft`) meet. One
order, on every turn, and it is the order that makes the ticket's promises true:

1. read the description and the whole conversation for all seven fields;
2. write what was read over what was known — a later reading may *correct* an
   earlier one, which is how a flagged contradiction gets resolved;
3. if this turn answered a question and the field it asked about is still empty,
   store the creator's own sentence there;
4. ask the model's question if it is about something genuinely open, otherwise
   write one from the schedule — and ask nothing at all when the draft is full
   and no contradiction is standing.

Steps 3 and 4 are the ones doing the load-bearing work, and both are the code
overruling the model. ADR-0020 §3 deliberately handed the stop condition to the
model; it did not hand over the two guarantees that sit either side of it. A
model that reads nothing out of an answer cannot make the same question come
back (step 3), and a model that declares itself finished cannot end an interview
whose spec would be partial (step 4) — `is_complete` is arithmetic over empty
fields, never an opinion.

Every turn commits, for V1's reason: an interview is a thing people abandon
halfway, so "saved" has to mean "saved as of the last thing you said".
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import SolutionInterviewSession
from src.services.llm import ChatModelFactory
from src.solution.designer import DesignerReading, Question, Turn, read_problem
from src.solution.draft import FIELD_SCHEDULE, FieldSpec, SpecDraft, SpecField, field_named

#: How a contradiction is put to the creator when the model flagged one but did
#: not write a question of its own. Quotes the contradiction verbatim: it is the
#: model's sentence about the creator's own words, and paraphrasing it here would
#: leave them guessing which two things are being called incompatible.
_CONFLICT_QUESTION = "Chỗ này chưa khớp nhau, bạn nói rõ giúp: {conflict}"


class SolutionInterviewError(ValueError):
    """Something the caller sent cannot be used. Message is user-facing."""


class NoOpenQuestionError(SolutionInterviewError):
    """An answer arrived for an interview that has no question open.

    Its own type because it is the one failure here that is not about the body:
    nothing the caller sends could fix it — the interview is finished — so it has
    to reach the HTTP layer as a conflict rather than as an unprocessable payload.
    """


@dataclass(frozen=True, slots=True)
class InterviewState:
    """Everything one session knows: what is filled, what does not add up, what is open.

    Three parts, and only two of them are the model's. `draft` and `conflicts` are
    what it read and what it flagged; the question is chosen here from both. What
    is deliberately *not* a part is "is it finished" — that is `is_complete`,
    derived, so no stored flag can disagree with the fields themselves.
    """

    draft: SpecDraft
    conflicts: tuple[str, ...]
    question: Question | None

    @classmethod
    def empty(cls) -> "InterviewState":
        """The state before a description has been read — everything open."""
        return cls(draft=SpecDraft.empty(), conflicts=(), question=None)

    @property
    def missing(self) -> tuple[FieldSpec, ...]:
        """The fields still to be answered, in interview order."""
        return self.draft.missing

    @property
    def remaining(self) -> int:
        """How many fields are still empty."""
        return len(self.missing)

    @property
    def answered(self) -> int:
        """How many fields hold something — the other half of the progress a creator reads.

        Here rather than as `total - remaining` at the call site, for the reason
        V1's `SlotSheet.answered` gives: progress arithmetic done in the HTTP
        layer is the one place a count could drift from the question being asked
        beside it.

        It counts *fields*, not questions. ADR-0020 accepted that this interview
        has no reliable "N of M" indicator — a standing contradiction can keep it
        open with every field answered — and these numbers deliberately do not
        claim to be one.
        """
        return self.total - self.remaining

    @property
    def total(self) -> int:
        """How many fields the specification has in all — the denominator of the progress."""
        return len(FIELD_SCHEDULE)

    @property
    def is_complete(self) -> bool:
        """Whether a specification could be built and confirmed from this session now.

        Both halves matter. An empty field is a spec with a hole in it; an open
        contradiction is a spec whose two halves cannot both be true, and building
        one would hand the Workflow Generator a problem statement that argues with
        itself.
        """
        return self.draft.is_complete and not self.conflicts


def state_of(session: SolutionInterviewSession) -> InterviewState:
    """The state this row is holding.

    The single reader of the three JSON columns, so their stored shape is
    knowledge that stays inside this module — everything else asks the state.
    """
    return InterviewState(
        draft=SpecDraft.read(session.draft or {}),
        conflicts=tuple(session.conflicts or ()),
        question=_stored_question(session.open_question),
    )


def turns_of(session: SolutionInterviewSession) -> tuple[Turn, ...]:
    """The conversation so far, in order — what the designer is re-shown every turn."""
    return tuple(
        Turn(
            question=str(turn.get("question", "")),
            answer=str(turn.get("answer", "")),
            field=(str(turn["field"]) if turn.get("field") else None),
        )
        for turn in (session.turns or [])
    )


async def start_interview(
    db: Session,
    *,
    owner_id: int,
    description: str,
    model_factory: ChatModelFactory,
) -> SolutionInterviewSession:
    """Begin an interview by reading `description` for everything it already answers.

    The reading happens *before* the first question rather than after, and that
    ordering is user story #2: a description covering all seven fields produces a
    session that is already complete, and the creator is never asked anything.
    There is no separate "should we skip the interview" decision to get wrong —
    skipping is what happens when nothing is left open.
    """
    text = _required(description, "Mô tả bài toán")
    reading = await read_problem(description=text, turns=(), known=SpecDraft.empty(), model_factory=model_factory)
    state = _settled(InterviewState.empty(), reading, answered=None, answer="", turns=())

    session = SolutionInterviewSession(
        owner_id=owner_id,
        description=text,
        draft=state.draft.to_stored(),
        conflicts=list(state.conflicts),
        open_question=_question_document(state.question),
        turns=[],
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


async def answer_current_question(
    db: Session,
    session: SolutionInterviewSession,
    answer: str,
    *,
    model_factory: ChatModelFactory,
) -> SolutionInterviewSession:
    """Answer the question the interview is currently asking, and move on.

    The answer is not read on its own: the whole conversation goes back to the
    designer, so a one-word reply is understood against everything that came
    before it, and a creator who answers two questions in one breath has both
    read. That is also why an answer can fill more than the field that was asked.

    Whatever else it fills, the *asked* field ends up filled — from the creator's
    own words if the model read nothing usable out of them. Their sentence is a
    worse-structured answer than the model's reading of it and a far better one
    than silence, and it is what stops a bad model turn sending the same question
    straight back. A question about a contradiction has no single field to fall
    back into, and correctly gets none.
    """
    asked = state_of(session).question
    if asked is None:
        raise NoOpenQuestionError("Phỏng vấn đã đủ thông tin, không còn câu hỏi nào để trả lời")
    text = _required(answer, "Câu trả lời")

    previous = state_of(session)
    asked_field = None if asked.field is None else asked.field.value
    turns = (*turns_of(session), Turn(question=asked.text, answer=text, field=asked_field))
    reading = await read_problem(
        description=session.description,
        turns=turns,
        known=previous.draft,
        model_factory=model_factory,
    )
    state = _settled(previous, reading, answered=asked, answer=text, turns=turns)

    session.draft = state.draft.to_stored()
    session.conflicts = list(state.conflicts)
    session.open_question = _question_document(state.question)
    session.turns = [{"question": turn.question, "answer": turn.answer, "field": turn.field} for turn in turns]
    db.commit()
    db.refresh(session)
    return session


def list_sessions(db: Session, owner_id: int) -> list[SolutionInterviewSession]:
    """This creator's interviews, newest first — how an abandoned one is found again."""
    return list(
        db.execute(
            select(SolutionInterviewSession)
            .where(SolutionInterviewSession.owner_id == owner_id)
            .order_by(SolutionInterviewSession.created_at.desc(), SolutionInterviewSession.id.desc())
        ).scalars()
    )


#: How many times one field — or the contradiction question — may be put to the
#: creator before the interview stops asking about it. Two, not one: the two
#: structured fields (`data`, `success_criteria`) legitimately need a second,
#: better-shaped question (ADR-0030 §1). Three is where the measured failure
#: began — *"file chính sách có định dạng gì?"* ten turns running on 2026-08-30 —
#: and nothing a creator says on a third ask has ever been read differently.
MAX_QUESTIONS_PER_FIELD = 2

#: The two fields a model may ask about again while they already hold something
#: — because "holding something" is not "holding everything" for a list whose
#: entries each need a `kind` or a `category` (ADR-0030 §1). Bounded by
#: `MAX_QUESTIONS_PER_FIELD` like every other question.
_REFINABLE_FIELDS: frozenset[SpecField] = frozenset({SpecField.DATA, SpecField.SUCCESS_CRITERIA})


def _numeric_sentences_lost(answer: str, draft: SpecDraft) -> tuple[str, ...]:
    """The sentences of `answer` whose numbers the reading did not carry into any criterion.

    A number in a criterion is a business rule — *"từ 60 ngày là thư cảnh báo"* —
    and the one thing the Workflow Composer cannot recover from anywhere else.
    Measured on 2026-08-30 (`benchmark/pitch_kinh_doanh`, agent 2, runs 25–29):
    the model read that answer back as *"chính xác"* and *"gọi đúng công cụ"*
    five times out of five, and the graph's threshold became whatever the
    composer guessed. Code, not the model, decides that a number said out loud
    survives: every sentence carrying a digit the criteria lost is kept verbatim.
    """
    kept = " ".join(criterion.text for criterion in draft.success_criteria)
    sentences = [part.strip() for part in re.split(r"(?<=[.;!?])\s+", answer) if part.strip()]
    return tuple(
        sentence
        for sentence in sentences
        if any(number not in kept for number in re.findall(r"\d+", sentence))
    )


def _asked_count(turns: Sequence[Turn], field: SpecField | None) -> int:
    """How many turns so far asked about `field` (`None` counts contradiction questions)."""
    wanted = None if field is None else field.value
    return sum(1 for turn in turns if turn.field == wanted)


def _settled(
    previous: InterviewState,
    reading: DesignerReading,
    *,
    answered: Question | None,
    answer: str,
    turns: Sequence[Turn],
) -> InterviewState:
    """What the session knows after one designer pass — steps 2 to 4 of the turn.

    `conflicts` is replaced rather than accumulated. Every pass re-reads the whole
    conversation, so the contradictions it reports are the ones that survive
    everything said so far; keeping an old one beside them would leave a
    resolved contradiction blocking the interview forever.

    Two caps, both counted off `turns`. A field the creator has answered
    `MAX_QUESTIONS_PER_FIELD` times and the model still read nothing out of is
    filled from their own words — forced for the two structured fields, see
    `SpecDraft.with_answer` — rather than asked again. A contradiction the
    creator has been asked about that many times is dropped: they have said
    what they meant twice, and the spec is theirs to confirm on the next screen.
    """
    draft = previous.draft.merged_with(reading.draft)
    if answered is not None and answered.field is not None and not draft.filled(answered.field):
        exhausted = _asked_count(turns, answered.field) >= MAX_QUESTIONS_PER_FIELD
        draft = draft.with_answer(answered.field, answer, force=exhausted)
    if answered is not None and answered.field is SpecField.SUCCESS_CRITERIA:
        draft = draft.with_criteria(_numeric_sentences_lost(answer, draft))
    conflicts = reading.conflicts if _asked_count(turns, None) < MAX_QUESTIONS_PER_FIELD else ()
    return InterviewState(
        draft=draft, conflicts=conflicts, question=_next_question(draft, conflicts, reading.question, turns)
    )


def _next_question(
    draft: SpecDraft, conflicts: Sequence[str], proposed: Question | None, turns: Sequence[Turn]
) -> Question | None:
    """The one question to put now, or `None` because there is nothing left to ask.

    The model's question is preferred — it has read the conversation and can ask
    something sharper than the schedule's generic sentence — but only when it is
    about a field that is still empty **and** has not already been asked
    `MAX_QUESTIONS_PER_FIELD` times. A question about a settled field is dropped,
    and that is the criterion *"không phải trả lời lại điều đã nói"* made
    structural rather than hoped for in a prompt.

    A standing contradiction no longer lets the model's question through
    regardless of field. That was the loop measured on 2026-08-30: the model
    filed "còn mơ hồ" under `conflicts`, and this function then returned its
    *how-do-you-do-it* question about an already-filled field ten turns in a row.
    Contradictions get their own question, from the schedule, capped like a field.

    Whenever the model's question is unusable the schedule answers instead, so the
    interview can never stall with something open and nothing to ask — and when
    every open thing has been asked its full allowance, `_settled` has already
    filled or dropped it, so `None` here means finished.
    """
    missing = draft.missing
    if proposed is not None and proposed.field is not None and _asked_count(turns, proposed.field) < MAX_QUESTIONS_PER_FIELD:
        # A settled field is not asked about again — except the two structured
        # ones, which can be *partly* settled: "data" holding the web source
        # while the policy the creator also mentioned has no kind yet. The
        # designer is told to leave such a source out and ask; this is what
        # lets that question through, and the cap is what keeps it from
        # becoming the loop it used to be.
        if not draft.filled(proposed.field) or proposed.field in _REFINABLE_FIELDS:
            return proposed
    if not missing and not conflicts:
        return None
    for spec in missing:
        if _asked_count(turns, spec.field) < MAX_QUESTIONS_PER_FIELD:
            return Question(field=spec.field, text=spec.question)
    if conflicts and _asked_count(turns, None) < MAX_QUESTIONS_PER_FIELD:
        return Question(field=None, text=_CONFLICT_QUESTION.format(conflict=conflicts[0]))
    return None


def _question_document(question: Question | None) -> dict[str, Any] | None:
    """The open question as the column holds it — `null` when the interview is over."""
    if question is None:
        return None
    return {"field": None if question.field is None else question.field.value, "text": question.text}


def _stored_question(document: dict[str, Any] | None) -> Question | None:
    """The question a row was holding, or `None` if it had none.

    A stored field name that is no longer one of the seven becomes a question
    about nothing in particular rather than a crash: a field the schema dropped
    should cost the creator a re-phrased question, not the session they had
    half-finished.
    """
    if not document or not str(document.get("text", "")).strip():
        return None
    return Question(field=field_named(document.get("field")), text=str(document["text"]).strip())


def _required(value: str, subject: str) -> str:
    """`value` stripped, or the reason the turn cannot be taken.

    Refused here rather than only on the request schema, for the reason
    `src.interview.sessions` gives: "an empty description is not an interview" is
    a fact about this domain, and a second caller deserves the same refusal in the
    same words.
    """
    cleaned = value.strip()
    if not cleaned:
        raise SolutionInterviewError(f"{subject} không được để trống")
    return cleaned
