"""`POST /runs/feedback` — a thumbs from the person who got the answer. ADR-0053 §3.

The cheapest label there is (PM playbook step 4; slide 11: *thumbs-down là
signal từ user*), and until this route the only people who could leave one
were PMs reading the board. Filed under the run's `trace_id` — the name the
`thread` event announced before the answer arrived — because the ledger row
(`agent_runs`) is written after the stream ends, so the row's own id is not
something the chat window can know while the answer is on screen. One door
for both pipelines: V2's `thread` event and UC-1's `done` event both carry
the id, and both ledgers file their callers under the same key.

Who may vote: the caller the run was served to, the agent's owner, or a PM /
admin. Anyone else gets the same 404 a run that does not exist gets — the
thumbs is about *their* answer, and a run id is not a thing to enumerate.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.threads import caller_key
from src.auth.dependencies import CurrentUser
from src.db.base import get_db
from src.db.models import PM_CONTROL_ROOM_ROLES, Agent, AgentRun, QualityScore, ScoreSource, User

router = APIRouter(prefix="/runs", tags=["runs"])

THUMBS = "thumbs_up"


class RunFeedbackRequest(BaseModel):
    trace_id: str = Field(min_length=4, max_length=128)
    thumbs_up: bool
    comment: str | None = Field(default=None, max_length=2000)


class RunFeedbackOut(BaseModel):
    score_id: int
    run_id: int
    thumbs_up: bool
    comment: str | None


def _may_vote(db: Session, run: AgentRun, user: User) -> bool:
    if user.role in PM_CONTROL_ROOM_ROLES:
        return True
    if run.caller and run.caller == caller_key(user_id=user.id):
        return True
    if run.agent_id is not None:
        agent = db.get(Agent, run.agent_id)
        return agent is not None and agent.owner_id == user.id
    return False


@router.post("/feedback", response_model=RunFeedbackOut, status_code=status.HTTP_201_CREATED)
def leave_run_feedback(
    user: CurrentUser, db: Annotated[Session, Depends(get_db)], payload: RunFeedbackRequest
) -> RunFeedbackOut:
    """Thumbs up/down on the run named by `trace_id` — the newest leg of it, if it paused and resumed."""
    run = db.execute(
        select(AgentRun).where(AgentRun.trace_id == payload.trace_id).order_by(AgentRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    if run is None or not _may_vote(db, run, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không có lần chạy nào mang mã này")
    score = QualityScore(
        agent_id=run.agent_id,
        version_id=run.version_id,
        run_cost_id=run.run_cost_id,
        run_id=run.id,
        name=THUMBS,
        value=1.0 if payload.thumbs_up else 0.0,
        comment=(payload.comment or "").strip() or None,
        source=ScoreSource.HUMAN,
        given_by=user.id,
    )
    db.add(score)
    db.commit()
    db.refresh(score)
    return RunFeedbackOut(score_id=score.id, run_id=run.id, thumbs_up=payload.thumbs_up, comment=score.comment)
