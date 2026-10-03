"""Generating an agent from a finished interview, read through HTTP — see #17.

The claims here are the ones only the whole path can make: that a spec the model
wrote lands in the version lifecycle as an ordinary **draft**, that generating
twice refines one agent rather than breeding two, and that a meta-agent having a
bad day produces a sentence a creator can act on instead of a blank screen.

Every model is a scripted double. Which spec comes out is the double's business;
what happens to it — the slots it carries, the guardrails, whether it is saved at
all — is the platform's, and that is what each test is about.
"""

import json
from collections.abc import Generator
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from src.db.models import Agent, AgentVersion, InterviewSession, VersionStatus
from tests.conftest import auth_header, writes_spec

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

#: A description that answers all five slots, so the interview is over before the
#: first question — the state this ticket starts from.
FULL_DESCRIPTION = (
    "Tôi cần một trợ lý cho nhân viên chăm sóc khách hàng. Nó đọc email khiếu nại gửi vào hộp thư hỗ trợ, "
    "tra đơn hàng trong ERP rồi soạn thư trả lời để tôi duyệt."
)

PROHIBITIONS = ["không tự ý hứa hoàn tiền", "không gửi thư cho khách khi chưa được duyệt"]
SUCCESS_CRITERIA = ["thư nêu đúng mã đơn hàng khách nhắc tới", "thư nêu đúng ngày giao dự kiến"]


def reads_everything() -> AIMessage:
    """The extraction reply that finishes the interview in one pass."""
    return AIMessage(
        content=json.dumps(
            {
                "audience": "nhân viên chăm sóc khách hàng",
                "inputs": "email khiếu nại gửi vào hộp thư hỗ trợ",
                "capabilities": ["tra đơn hàng trong ERP", "soạn thư trả lời"],
                "success_criteria": SUCCESS_CRITERIA,
                "prohibitions": PROHIBITIONS,
            },
            ensure_ascii=False,
        )
    )


async def a_finished_interview(client: Any, token: str, factory: Any) -> int:
    """An interview whose description answered everything — nothing left to ask."""
    factory([[reads_everything()]])
    response = await client.post(
        "/api/v1/interviews", json={"description": FULL_DESCRIPTION}, headers=auth_header(token)
    )
    assert response.status_code == 201, response.text
    assert response.json()["complete"] is True
    return int(response.json()["id"])


async def generate(client: Any, token: str, interview_id: int) -> Any:
    return await client.post(f"/api/v1/interviews/{interview_id}/spec", headers=auth_header(token))


@pytest.fixture(autouse=True)
def _generated_agents(db_session: Session) -> Generator[None, None, None]:
    """Delete whatever these tests generated, whether or not they got to the end.

    Autouse and in a fixture rather than a line at the bottom of each test: the
    rows are created through a real endpoint, so a failing assertion halfway
    would otherwise leave an agent and a draft version behind and the *next*
    suite would be the one that looks broken — the argument
    `default_platform_config` makes about the config row.

    Interviews are cleared too, because `interview_sessions.agent_id` is what
    makes a second generation reuse an agent: a session surviving into another
    test would point at an agent this teardown has removed. Only the rows this
    test added — anything past the id the table was already at — so a suite
    running beside this one keeps its own scenery.
    """
    watermark = db_session.query(InterviewSession.id).order_by(InterviewSession.id.desc()).limit(1).scalar() or 0
    yield
    db_session.rollback()
    mine = db_session.query(InterviewSession).filter(InterviewSession.id > watermark)
    generated = [session.agent_id for session in mine.all()]
    mine.delete()
    for agent_id in [agent_id for agent_id in generated if agent_id is not None]:
        db_session.query(AgentVersion).filter(AgentVersion.agent_id == agent_id).delete()
        db_session.query(Agent).filter(Agent.id == agent_id).delete()
    db_session.commit()


# --------------------------------------------------------------------------- #
# A spec the model got right
# --------------------------------------------------------------------------- #


async def test_a_finished_interview_becomes_a_draft_version_of_a_new_agent(
    client, creator_token, scripted_chat_model_factory, db_session
) -> None:
    """The ticket in one request: slots in, a saved draft out — no new lifecycle."""
    interview_id = await a_finished_interview(client, creator_token, scripted_chat_model_factory)
    scripted_chat_model_factory([[writes_spec()]])

    response = await generate(client, creator_token, interview_id)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == VersionStatus.DRAFT.value
    assert [step["id"] for step in payload["spec"]["steps"]] == ["trich-xuat", "tra-cuu"]

    version = db_session.get(AgentVersion, payload["version_id"])
    assert version is not None
    assert version.status is VersionStatus.DRAFT
    assert version.agent_id == payload["agent_id"]
