"""Interview endpoints — the creator's first screen. See #16.

Its own module rather than more of `agent_routes` because an interview is not
about an agent: it runs *before* there is one. #17 is what turns a finished
interview into a draft version, and until then there is nothing in `agents` these
handlers could be scoped to.

They are scoped to the caller instead. `OwnedInterview` is the same shape as
`OwnedAgent` and refuses the same way — with a 404, never a 403. A session id is
guessable, and an endpoint that distinguished "no such interview" from "not
yours" would disclose what other people in the platform are building, one probe
at a time.

There is no endpoint here that decides anything. Starting reads a description,
answering reads an answer, and both hand back the same session shape — what is
known, what is left, and the one question still open. Whether the interview is
over is arithmetic the response carries (`complete`), not a state a handler set.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.agents.versions import VersionLifecycleError
from src.auth.dependencies import CurrentUser
from src.db.base import get_db
from src.db.models import InterviewSession
from src.interview.generation import NoAssignableModelError, SpecGenerationError, draft_from_interview
from src.interview.sessions import (
    InterviewError,
    NoOpenQuestionError,
    answer_current_question,
    clear_slot,
    list_sessions,
    set_slot,
    sheet_of,
    start_interview,
)
from src.interview.slots import SLOT_SCHEDULE, IncompleteSheetError, Slot, SlotSheet, SlotSpec
from src.models.agent_spec import AgentSpec
from src.models.schemas import (
    GeneratedSpecResponse,
    InterviewAnswerRequest,
    InterviewSessionResponse,
    InterviewSlotResponse,
    InterviewSlotUpdateRequest,
    InterviewStartRequest,
)
from src.services.llm import ChatModelFactory, get_chat_model_factory

router = APIRouter(prefix="/interviews", tags=["interview"])

ModelFactory = Annotated[ChatModelFactory, Depends(get_chat_model_factory)]


def _get_owned_interview(
    interview_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> InterviewSession:
    """The interview at `interview_id` if it belongs to the caller, otherwise 404.

    404 rather than 403 for somebody else's session, so the endpoint cannot be
    used to find out how many interviews another creator has started.
    """
    session = db.get(InterviewSession, interview_id)
    if session is None or session.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy phiên phỏng vấn")
    return session


OwnedInterview = Annotated[InterviewSession, Depends(_get_owned_interview)]


@router.post("", response_model=InterviewSessionResponse, status_code=status.HTTP_201_CREATED)
async def start_my_interview(
    payload: InterviewStartRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    model_factory: ModelFactory,
) -> InterviewSessionResponse:
    """Start an interview from a description, filling in everything it already answers.

    A description covering all five slots comes back `complete` with
    `next_question: null` — the creator is asked nothing and goes straight on to
    generating a spec. That is not a shortcut the handler takes; it is what
    happens when there is no empty slot left to ask about.
    """
    try:
        session = await start_interview(
            db, owner_id=user.id, description=payload.description, model_factory=model_factory
        )
    except InterviewError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return interview_response(session)


@router.get("", response_model=list[InterviewSessionResponse])
def list_my_interviews(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> list[InterviewSessionResponse]:
    """Every interview this creator has started, newest first.

    What makes an abandoned session findable. Resuming needs the id, and a creator
    who closed the page mid-answer has nowhere else to have kept it.
    """
    return [interview_response(session) for session in list_sessions(db, user.id)]


@router.get("/{interview_id}", response_model=InterviewSessionResponse)
def read_my_interview(interview: OwnedInterview) -> InterviewSessionResponse:
    """One interview in full — both the review screen and the resume path.

    They are the same request because they want the same thing: every slot with
    what it holds, and the question still open. A creator coming back after
    closing the page picks up at exactly the question they left.
    """
    return interview_response(interview)


@router.post("/{interview_id}/answers", response_model=InterviewSessionResponse)
async def answer_my_interviews_question(
    interview: OwnedInterview,
    payload: InterviewAnswerRequest,
    db: Annotated[Session, Depends(get_db)],
    model_factory: ModelFactory,
) -> InterviewSessionResponse:
    """Answer the open question. The response carries whatever is next, or nothing.

    An answer can fill more than the slot that was asked — creators rarely answer
    one question at a time — so `remaining` may fall by more than one. It never
    rises, and the answered slot is always filled, which is what bounds the
    interview at one question per slot.

    The two refusals are deliberately different statuses, because the caller's
    next move differs. A finished interview is a 409 — the body was fine and
    nothing they send will change the answer, so they should read the session. An
    answer of pure whitespace is a 422, the ordinary "fix this and send it again".
    Collapsing both into one status would tell half the callers the wrong thing.
    """
    try:
        answered = await answer_current_question(db, interview, payload.answer, model_factory=model_factory)
    except NoOpenQuestionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except InterviewError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return interview_response(answered)


@router.post("/{interview_id}/spec", response_model=GeneratedSpecResponse, status_code=status.HTTP_201_CREATED)
async def generate_my_interviews_spec(
    interview: OwnedInterview,
    db: Annotated[Session, Depends(get_db)],
    model_factory: ModelFactory,
) -> GeneratedSpecResponse:
    """Turn this interview into an agent, saved as a draft version — see #17.

    The one place the interview stops being a form and becomes something that
    runs. What comes back is an ordinary `draft`: it has to be trialled, evaluated
    and approved like any other version, and nothing here shortens that path.

    Called again on the same interview it produces another draft of the **same**
    agent, so a creator who did not like the first shape can simply ask again
    without ending up with two agents and no way to tell them apart.

    Four refusals, and they are different statuses because the next move differs.
    An unfinished interview is a 409 naming the slots still empty — the body was
    fine and there is a question to go and answer. A platform with no assignable
    model left is a 409 too: also nothing to retry, and the way out is an admin
    widening the allowlist. A meta-agent that could not write a valid spec in its
    allotted attempts is a 502 with a sentence about trying again — nothing about
    the request was wrong, and the creator has not lost their interview.

    The last is a 422 and is deliberately a net rather than a path. Generation
    already validates against the live allowlist, so `create_draft`'s own check
    can only fire if an admin narrows the allowlist *between* the two — a real
    race with a wrong-looking answer (500) if nothing catches it, and one no test
    can stage honestly.
    """
    try:
        version = await draft_from_interview(db, interview, model_factory=model_factory)
    except IncompleteSheetError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except NoAssignableModelError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except SpecGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    except VersionLifecycleError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return GeneratedSpecResponse(
        agent_id=version.agent_id,
        version_id=version.id,
        status=version.status,
        spec=AgentSpec.model_validate(version.spec),
        created_at=version.created_at,
    )


@router.put("/{interview_id}/slots/{slot}", response_model=InterviewSessionResponse)
def correct_my_interviews_slot(
    slot: Slot,
    interview: OwnedInterview,
    payload: InterviewSlotUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
) -> InterviewSessionResponse:
    """Overwrite a slot with what the creator says it should say.

    The only write that may replace a filled slot, and it exists because the
    review screen would otherwise be a screen for reading a misunderstanding.
    ADR-0011 §1 made the same argument for generated test cases: being shown a
    machine's reading of your own words, with no way to correct it, is worse than
    not being shown it.
    """
    try:
        corrected = set_slot(db, interview, slot, payload.value)
    except InterviewError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return interview_response(corrected)


@router.delete("/{interview_id}/slots/{slot}", response_model=InterviewSessionResponse)
def clear_my_interviews_slot(
    slot: Slot,
    interview: OwnedInterview,
    db: Annotated[Session, Depends(get_db)],
) -> InterviewSessionResponse:
    """Empty a slot, which puts its question back into the interview.

    For the creator who can see a reading is wrong but would rather be asked again
    than dictate the replacement. Returns the session with the question re-opened,
    so the same screen that showed the mistake shows the way out of it.
    """
    return interview_response(clear_slot(db, interview, slot))


def interview_response(session: InterviewSession) -> InterviewSessionResponse:
    """One session as the wire sees it, with every derived number derived here.

    `answered` and `remaining` are counted off the same sheet that produced
    `next_question`, in one place, so progress and question cannot contradict each
    other on a screen. #17 will render the same shape when it reads a finished
    interview, which is why this is public rather than a handler's local.
    """
    sheet = sheet_of(session)
    return InterviewSessionResponse(
        id=session.id,
        description=session.description,
        slots=[_slot_response(spec, sheet) for spec in SLOT_SCHEDULE],
        next_question=_next_question_response(sheet),
        answered=sheet.answered,
        remaining=sheet.remaining,
        total=sheet.total,
        complete=sheet.is_complete,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _next_question_response(sheet: SlotSheet) -> InterviewSlotResponse | None:
    """The open question as a slot row, or `None` when the interview is over."""
    asked = sheet.next_question
    return None if asked is None else _slot_response(asked, sheet)


def _slot_response(spec: SlotSpec, sheet: SlotSheet) -> InterviewSlotResponse:
    """One slot with its question and whatever it holds — `None` while it is empty.

    Carries the question even for a filled slot: the review screen's job is to let
    a creator check the system understood them, and a value shown without the
    question it answers is a sentence with nothing to check it against.
    """
    value = sheet.value_of(spec.slot)
    return InterviewSlotResponse(
        slot=spec.slot,
        question=spec.question,
        value=list(value) if isinstance(value, tuple) else value,
        filled=value is not None,
    )
