"""V2's first screens: the solution-design interview, and the spec it hands over — see #42.

Two routers, because the two halves are scoped to different things and that is
not cosmetic. An interview belongs to a *person* — it runs before there is an
agent to attach it to — while a spec belongs to a *version*, and inherits the
ownership checks every other version-scoped endpoint already has
(`OwnedVersion`). Folding them together would mean re-deriving one of the two
ownership rules by hand.

There is deliberately no endpoint here that decides whether an interview is
finished. Starting reads a description, answering reads an answer, and both hand
back the same shape — the seven fields, what is still open, and the one question
waiting. `complete` is arithmetic the response carries, not a state a handler set.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.agents.versions import version_number
from src.api.agent_routes import OwnedAgent, OwnedVersion
from src.auth.dependencies import CurrentUser
from src.datasources.sources import unowned_database_refs
from src.db.base import get_db
from src.db.models import AgentVersion, SolutionInterviewSession
from src.models.schemas import (
    SolutionFieldResponse,
    SolutionInterviewAnswerRequest,
    SolutionInterviewResponse,
    SolutionInterviewStartRequest,
    SolutionQuestionResponse,
    SolutionSpecDraftResponse,
    SolutionSpecResponse,
    SolutionSuggestion,
    SolutionSuggestionsResponse,
    SolutionTurnResponse,
)
from src.models.solution_spec import AgentSolutionSpecification
from src.services.llm import ChatModelFactory, get_chat_model_factory
from src.solution.draft import FIELD_SCHEDULE, IncompleteDraftError
from src.solution.sessions import (
    InterviewState,
    NoOpenQuestionError,
    SolutionInterviewError,
    answer_current_question,
    list_sessions,
    start_interview,
    state_of,
    turns_of,
)
from src.solution.specs import SpecStageError, confirm_spec, propose_spec, revise_spec, solution_spec_of
from src.solution.suggestions import suggest_answers

router = APIRouter(prefix="/solution-interviews", tags=["solution"])
spec_router = APIRouter(prefix="/agents", tags=["solution"])

ModelFactory = Annotated[ChatModelFactory, Depends(get_chat_model_factory)]


def _get_owned_interview(
    interview_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> SolutionInterviewSession:
    """The interview at `interview_id` if it belongs to the caller, otherwise 404.

    404 rather than 403 for somebody else's session, the same rule `OwnedAgent`
    follows: a session id is guessable, and an endpoint that distinguished "no
    such interview" from "not yours" would disclose what other people in the
    platform are building, one probe at a time.
    """
    session = db.get(SolutionInterviewSession, interview_id)
    if session is None or session.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy phiên phỏng vấn")
    return session


OwnedInterview = Annotated[SolutionInterviewSession, Depends(_get_owned_interview)]


@router.post("", response_model=SolutionInterviewResponse, status_code=status.HTTP_201_CREATED)
async def start_my_solution_interview(
    payload: SolutionInterviewStartRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    model_factory: ModelFactory,
) -> SolutionInterviewResponse:
    """Describe a business problem; get back either a finished reading or what is still unclear.

    User story #1 and #2 in one request. A description that answers all seven
    fields comes back `complete` with `open_question: null` and the creator is
    asked nothing — not because the handler took a shortcut, but because there is
    nothing left open to ask about.
    """
    try:
        session = await start_interview(
            db, owner_id=user.id, description=payload.description, model_factory=model_factory
        )
    except SolutionInterviewError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return interview_response(session)


@router.get("", response_model=list[SolutionInterviewResponse])
def list_my_solution_interviews(
    user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> list[SolutionInterviewResponse]:
    """Every interview this creator has started, newest first.

    What makes an abandoned session findable. Resuming needs the id, and a creator
    who closed the page mid-answer has nowhere else to have kept it.
    """
    return [interview_response(session) for session in list_sessions(db, user.id)]


@router.get("/{interview_id}", response_model=SolutionInterviewResponse)
def read_my_solution_interview(interview: OwnedInterview) -> SolutionInterviewResponse:
    """One interview in full — both the review screen and the resume path.

    They are the same request because they want the same thing: every field with
    what it holds, the contradictions still standing, and the question still open.
    A creator coming back after closing the page picks up at exactly the question
    they left (user story #6).
    """
    return interview_response(interview)


@router.get("/{interview_id}/suggestions", response_model=SolutionSuggestionsResponse)
async def suggest_my_solution_interviews_answers(
    interview: OwnedInterview,
    model_factory: ModelFactory,
) -> SolutionSuggestionsResponse:
    """Vài câu trả lời gợi ý cho câu hỏi đang mở — người tạo bấm chọn hoặc gõ tay.

    Kiểu recommend của Claude Code: 0–5 phương án bám bài toán, cái đầu có thể là
    "khuyến nghị". Đây là một lời gọi model **chỉ đọc**, do người dùng kích hoạt
    khi mở màn hình câu hỏi — nó không đụng gì tới phiên và tách hẳn khỏi đường
    trả lời, nên trả gợi ý không làm chậm lượt trả lời.

    Phỏng vấn đã xong (`open_question` null) trả danh sách rỗng chứ không phải 409:
    đây là GET, và "không còn gì để gợi ý" là một câu trả lời hợp lệ cho nó.
    """
    state = state_of(interview)
    question = state.question
    if question is None:
        return SolutionSuggestionsResponse(field=None, multi=False, suggestions=[])
    result = await suggest_answers(
        description=interview.description,
        turns=turns_of(interview),
        known=state.draft,
        question=question,
        model_factory=model_factory,
    )
    return SolutionSuggestionsResponse(
        field=result.field,
        multi=result.multi,
        suggestions=[SolutionSuggestion(text=item.text, recommended=item.recommended) for item in result.items],
    )


@router.post("/{interview_id}/answers", response_model=SolutionInterviewResponse)
async def answer_my_solution_interviews_question(
    interview: OwnedInterview,
    payload: SolutionInterviewAnswerRequest,
    db: Annotated[Session, Depends(get_db)],
    model_factory: ModelFactory,
) -> SolutionInterviewResponse:
    """Answer the open question. The response carries whatever is next, or nothing.

    An answer can fill more than the field that was asked — creators rarely answer
    one question at a time — so `remaining` may fall by more than one.

    The two refusals are deliberately different statuses, because the caller's
    next move differs. A finished interview is a 409: the body was fine and
    nothing they send will change the answer, so they should read the session and
    go on to build the spec. An answer of pure whitespace is a 422, the ordinary
    "fix this and send it again".
    """
    try:
        answered = await answer_current_question(db, interview, payload.answer, model_factory=model_factory)
    except NoOpenQuestionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except SolutionInterviewError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return interview_response(answered)


@router.post("/{interview_id}/spec", response_model=SolutionSpecResponse, status_code=status.HTTP_201_CREATED)
def build_my_solution_interviews_spec(
    interview: OwnedInterview,
    db: Annotated[Session, Depends(get_db)],
) -> SolutionSpecResponse:
    """Turn a finished interview into a version awaiting confirmation — ADR-0020 §4.

    No model call: everything the spec says has already been read and settled, so
    this is the interview's draft becoming a validated document and an
    `AgentVersion` row. What comes back is a **proposal** at
    `spec_pending_confirmation` — the creator reviews all seven fields and
    confirms before anything downstream may read it.

    Both refusals are 409 and say which one it is, because the next move differs:
    a missing field is a question waiting to be answered, a contradiction is a
    thing to settle in conversation. Neither is a fault of the request body, which
    is why neither is a 422.
    """
    try:
        version = propose_spec(db, interview)
    except (IncompleteDraftError, SpecStageError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return spec_response(version)


@spec_router.get("/{agent_id}/versions/{version_id}/solution-spec", response_model=SolutionSpecResponse)
def read_my_versions_solution_spec(_agent: OwnedAgent, version: OwnedVersion) -> SolutionSpecResponse:
    """The seven fields this version holds — the review screen, and what #43 will read.

    Owner-only, like every other version-scoped read: a spec is the creator's own
    account of their business, and a deployed agent is not a published source
    listing.
    """
    return spec_response(version)


@spec_router.put("/{agent_id}/versions/{version_id}/solution-spec", response_model=SolutionSpecResponse)
def revise_my_versions_solution_spec(
    agent: OwnedAgent,
    version: OwnedVersion,
    payload: AgentSolutionSpecification,
    db: Annotated[Session, Depends(get_db)],
) -> SolutionSpecResponse:
    """Correct a *proposed* spec before confirming it — ADR-0020 §4.

    The whole document is replaced rather than patched field by field, because
    that is what the review screen has in hand: the creator is looking at all
    seven fields and sends back the seven they mean. It is also where a data
    source first gets a real `ref` — the interview never invents one.

    A 409 once the version is confirmed. The body may be perfectly valid; what is
    wrong is that this spec is immutable now, and the way to change the business
    intent is a new version.

    A 422 for a `database` source whose `ref` is not one of the creator's own
    Hệ CSDL (DS-02) — an id is all a ref is, and one typed by hand could name
    somebody else's ERP.
    """
    unowned = unowned_database_refs(db, agent.owner_id, payload)
    if unowned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Không có Hệ CSDL nào của bạn mang id: {', '.join(unowned)}",
        )
    try:
        revised = revise_spec(db, version, payload)
    except SpecStageError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return spec_response(revised)


@spec_router.post(
    "/{agent_id}/versions/{version_id}/solution-spec/confirmation",
    response_model=SolutionSpecResponse,
)
def confirm_my_versions_solution_spec(
    _agent: OwnedAgent,
    version: OwnedVersion,
    db: Annotated[Session, Depends(get_db)],
) -> SolutionSpecResponse:
    """`spec_pending_confirmation -> spec_confirmed`. User story #5.

    The point the creator says the system understood them, and the point the spec
    stops being editable. A version that is already confirmed is a 409 rather than
    a silent success: the caller believes it is confirming what it just read, and
    a version that has moved on since is not that.
    """
    try:
        confirmed = confirm_spec(db, version)
    except SpecStageError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return spec_response(confirmed)


def interview_response(session: SolutionInterviewSession) -> SolutionInterviewResponse:
    """One session as the wire sees it, with every derived number derived here.

    `answered`, `remaining` and `complete` are counted off the same state that
    produced `open_question`, in one place, so progress and question cannot
    contradict each other on a screen.
    """
    state = state_of(session)
    return SolutionInterviewResponse(
        id=session.id,
        description=session.description,
        draft=_draft_response(state),
        fields=[
            SolutionFieldResponse(field=spec.field, question=spec.question, filled=state.draft.filled(spec.field))
            for spec in FIELD_SCHEDULE
        ],
        open_question=_question_response(state),
        conflicts=list(state.conflicts),
        turns=[SolutionTurnResponse(question=turn.question, answer=turn.answer) for turn in turns_of(session)],
        answered=state.answered,
        remaining=state.remaining,
        total=state.total,
        complete=state.is_complete,
        agent_id=session.agent_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def spec_response(version: AgentVersion) -> SolutionSpecResponse:
    """One version's solution spec as the wire sees it.

    Re-validated on the way out by `solution_spec_of`, which is also what answers
    a caller that asked a V1 version for a document it never had — with a sentence
    rather than a 500.
    """
    try:
        spec = solution_spec_of(version)
    except SpecStageError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    return SolutionSpecResponse(
        agent_id=version.agent_id,
        version_id=version.id,
        version_number=version_number(version),
        status=version.status,
        spec=spec,
        created_at=version.created_at,
    )


def _draft_response(state: InterviewState) -> SolutionSpecDraftResponse:
    """The seven fields as they currently stand — an empty one reads as empty, not as absent."""
    draft = state.draft
    return SolutionSpecDraftResponse(
        goal=draft.goal or None,
        users=list(draft.users),
        data=list(draft.data),
        actions=list(draft.actions),
        expected_outcome=list(draft.expected_outcome),
        success_criteria=list(draft.success_criteria),
        prohibitions=list(draft.prohibitions),
    )


def _question_response(state: InterviewState) -> SolutionQuestionResponse | None:
    """The open question, or `None` when the interview has nothing left to ask."""
    question = state.question
    return None if question is None else SolutionQuestionResponse(field=question.field, text=question.text)
