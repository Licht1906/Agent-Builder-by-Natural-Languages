"""The V2 solution-design interview and its spec, read through HTTP — see #42.

The end-to-end shape of the ticket: a description goes in, a confirmed
`AgentSolutionSpecification` comes out, and every acceptance criterion is
checkable on the way. The model is a scripted double throughout, so what these
tests measure is the platform's own behaviour around it — what it asks, what it
refuses, and what it will not let the model decide.
"""

import json
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from src.db.models import Agent, AgentVersion, SolutionInterviewSession
from tests.conftest import ScriptedMetaAgent, auth_header, install_chat_model_factory

pytestmark = pytest.mark.asyncio

#: A description that answers all seven fields. Written the way a creator writes
#: it, because the claim under test is that a well-written description is *read*
#: rather than re-asked — a placeholder would prove nothing about that.
FULL_DESCRIPTION = (
    "Tôi cần một trợ lý cho nhân viên chăm sóc khách hàng: nó đọc email khiếu nại trong hộp thư hỗ trợ, "
    "tra cứu đơn hàng rồi soạn sẵn thư trả lời để nhân viên duyệt. Làm đúng nghĩa là thư nêu đúng mã đơn "
    "hàng khách nhắc tới. Tuyệt đối không được tự ý hứa hoàn tiền."
)

FULL_READING: dict[str, Any] = {
    "goal": "Trả lời khiếu nại của khách trong một ngày làm việc",
    "users": ["Nhân viên chăm sóc khách hàng"],
    "data": [{"name": "Hộp thư hỗ trợ", "kind": "connected_account"}],
    "actions": ["Tra cứu đơn hàng", "Soạn thư trả lời"],
    "expected_outcome": ["Một thư trả lời đã soạn sẵn chờ nhân viên gửi"],
    "success_criteria": [{"text": "Thư nêu đúng mã đơn hàng khách nhắc tới", "category": "correctness"}],
    "prohibitions": ["Không tự ý hứa hoàn tiền"],
}

ALL_FIELDS = ["goal", "users", "data", "actions", "expected_outcome", "success_criteria", "prohibitions"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def reads(**document: Any) -> AIMessage:
    """One designer reply, as JSON."""
    return AIMessage(content=json.dumps(document, ensure_ascii=False))


def scripted(*replies: AIMessage) -> None:
    """Point the model seam at the shared meta-agent double, answering with `replies`."""
    install_chat_model_factory(ScriptedMetaAgent(*replies))


async def start(client: AsyncClient, token: str, description: str = FULL_DESCRIPTION) -> Any:
    return await client.post(
        "/api/v1/solution-interviews", json={"description": description}, headers=auth_header(token)
    )


async def build_spec(client: AsyncClient, token: str, interview_id: int) -> Any:
    return await client.post(f"/api/v1/solution-interviews/{interview_id}/spec", headers=auth_header(token))


def spec_url(body: dict[str, Any]) -> str:
    return f"/api/v1/agents/{body['agent_id']}/versions/{body['version_id']}/solution-spec"


@pytest.fixture(autouse=True)
def _clean_up_interviews(db_session: Session) -> Any:
    """Delete whatever this module created — the tables outlive the test run."""
    yield
    for session in db_session.query(SolutionInterviewSession).all():
        db_session.delete(session)
    db_session.commit()
    # Never touches a published agent: since #50 that is an agent with a
    # `Deployment` row, and the pointer this used to read has moved onto it.
    for agent in db_session.query(Agent).filter(~Agent.deployment.has()).all():
        versions = db_session.query(AgentVersion).filter(AgentVersion.agent_id == agent.id).all()
        if versions and all(version.solution_spec is not None for version in versions):
            for version in versions:
                db_session.delete(version)
            db_session.delete(agent)
    db_session.commit()


# --------------------------------------------------------------------------- #
# The spec: propose, review, confirm
# --------------------------------------------------------------------------- #


async def test_the_creator_sees_all_seven_fields_and_confirms_them(client: AsyncClient, creator_token: str) -> None:
    """Criterion five, end to end: review, confirm, and the spec is immutable after."""
    scripted(reads(**FULL_READING, question=None))
    started = (await start(client, creator_token)).json()
    proposed = (await build_spec(client, creator_token, started["id"])).json()

    reviewed = (await client.get(spec_url(proposed), headers=auth_header(creator_token))).json()
    assert set(reviewed["spec"]) == set(ALL_FIELDS)

    confirmed = await client.post(f"{spec_url(proposed)}/confirmation", headers=auth_header(creator_token))

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "spec_confirmed"


# --------------------------------------------------------------------------- #
# Suggested answers for the open question — the Claude-Code-style recommend
# --------------------------------------------------------------------------- #

#: A description thin enough that `users` is still open after the first reading.
THIN_DESCRIPTION = "Tôi muốn một agent đọc email khiếu nại của khách rồi soạn thư trả lời."


async def _open_on_users(client: AsyncClient, token: str) -> int:
    """Start an interview whose first reading leaves `users` the open question."""
    scripted(
        reads(goal="Trả lời khiếu nại của khách", question={"field": "users", "text": "Ai sẽ dùng agent này?"}),
    )
    started = await start(client, token, THIN_DESCRIPTION)
    return int(started.json()["id"])


async def test_the_open_question_comes_with_pickable_answers(client: AsyncClient, creator_token: str) -> None:
    """A list field's question offers several answers, the first one recommended."""
    interview_id = await _open_on_users(client, creator_token)
    scripted(
        reads(
            suggestions=[
                {"text": "Nhân viên chăm sóc khách hàng", "recommended": True},
                {"text": "Nhân viên phòng kinh doanh", "recommended": False},
            ]
        ),
    )

    body = (
        await client.get(
            f"/api/v1/solution-interviews/{interview_id}/suggestions", headers=auth_header(creator_token)
        )
    ).json()

    assert body["field"] == "users"
    assert body["multi"] is True
    assert [s["text"] for s in body["suggestions"]] == [
        "Nhân viên chăm sóc khách hàng",
        "Nhân viên phòng kinh doanh",
    ]
    assert [s["recommended"] for s in body["suggestions"]] == [True, False]


async def test_a_finished_interview_offers_nothing_and_calls_no_model(
    client: AsyncClient, creator_token: str
) -> None:
    """`open_question` is null once the interview is complete — suggestions is then empty."""
    scripted(reads(**FULL_READING, question=None))
    started = (await start(client, creator_token)).json()
    assert started["complete"] is True

    body = (
        await client.get(
            f"/api/v1/solution-interviews/{started['id']}/suggestions", headers=auth_header(creator_token)
        )
    ).json()

    assert body == {"field": None, "multi": False, "suggestions": []}


async def test_another_creator_cannot_read_the_suggestions(
    client: AsyncClient, creator_token: str, second_creator_token: str
) -> None:
    """404, not 403 — the same shielding every interview-scoped endpoint gives."""
    interview_id = await _open_on_users(client, creator_token)

    response = await client.get(
        f"/api/v1/solution-interviews/{interview_id}/suggestions", headers=auth_header(second_creator_token)
    )

    assert response.status_code == 404
