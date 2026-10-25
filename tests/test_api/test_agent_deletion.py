"""Deleting an agent: what goes with it, what survives it, and when it is refused.

The claim worth testing hardest is not "the row is gone" — it is that the
deletion *completes at all* on an agent with real history behind it. Two foreign
keys into `agents` and `agent_versions` have no `ON DELETE` clause, so a
plausible-looking implementation that deletes in the wrong order does not produce
wrong data: it raises `IntegrityError` from Postgres, and only against an agent
that has been published does it do so. That is why the fullest fixture here is a
deployed one.
"""

from collections.abc import Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    Agent,
    AgentVersion,
    Deployment,
    DeploymentStatus,
    SolutionInterviewSession,
    User,
)
from src.models.agent_spec import AgentSpec
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


async def test_deleting_a_draft_agent_removes_it(
    client: AsyncClient,
    creator_token: str,
    drafted_agent_of: Callable[..., tuple[Agent, AgentVersion]],
    seeded_creator: User,
    example_agent_spec: AgentSpec,
    db_session: Session,
) -> None:
    agent, _version = drafted_agent_of(seeded_creator, example_agent_spec)
    # Read before the row goes: afterwards `agent.id` is an attribute on an
    # expired object whose row no longer exists, and reading it raises.
    agent_id = agent.id

    response = await client.delete(f"/api/v1/agents/{agent_id}", headers=auth_header(creator_token))

    assert response.status_code == 204, response.text
    db_session.expire_all()
    listed = await client.get("/api/v1/agents", headers=auth_header(creator_token))
    assert agent_id not in [entry["id"] for entry in listed.json()]
    assert db_session.get(Agent, agent_id) is None


async def test_its_versions_go_with_it(
    client: AsyncClient,
    creator_token: str,
    drafted_agent_of: Callable[..., tuple[Agent, AgentVersion]],
    seeded_creator: User,
    example_agent_spec: AgentSpec,
    db_session: Session,
) -> None:
    agent, version = drafted_agent_of(seeded_creator, example_agent_spec)
    version_id = version.id

    await client.delete(f"/api/v1/agents/{agent.id}", headers=auth_header(creator_token))

    db_session.expire_all()
    assert db_session.get(AgentVersion, version_id) is None


async def test_a_published_agent_can_be_deleted_once_it_is_paused(
    client: AsyncClient,
    creator_token: str,
    deployed_agent_of: Callable[..., Agent],
    seeded_creator: User,
    example_agent_spec: AgentSpec,
    db_session: Session,
) -> None:
    """The full-history case: publication, version and agent, in the one order the keys allow.

    A wrong order here is an `IntegrityError`, not a subtle wrong answer — which
    is exactly why this test exists rather than only the draft one above.
    """
    agent = deployed_agent_of(seeded_creator, example_agent_spec)
    agent_id = agent.id
    paused = await client.put(
        f"/api/v1/agents/{agent_id}/deployment/status",
        json={"status": DeploymentStatus.DISABLED},
        headers=auth_header(creator_token),
    )
    assert paused.status_code == 200, paused.text

    response = await client.delete(f"/api/v1/agents/{agent_id}", headers=auth_header(creator_token))

    assert response.status_code == 204, response.text
    db_session.expire_all()
    assert db_session.get(Agent, agent_id) is None
    assert db_session.execute(select(Deployment).where(Deployment.agent_id == agent_id)).first() is None
    assert db_session.execute(select(AgentVersion).where(AgentVersion.agent_id == agent_id)).first() is None


async def test_an_agent_that_is_still_serving_is_refused(
    client: AsyncClient,
    creator_token: str,
    deployed_agent_of: Callable[..., Agent],
    seeded_creator: User,
    example_agent_spec: AgentSpec,
    db_session: Session,
) -> None:
    """Deleting something people are talking to is a decision taken twice."""
    agent = deployed_agent_of(seeded_creator, example_agent_spec)

    response = await client.delete(f"/api/v1/agents/{agent.id}", headers=auth_header(creator_token))

    assert response.status_code == 409
    assert "tạm dừng" in response.json()["detail"]
    assert db_session.get(Agent, agent.id) is not None


async def test_somebody_elses_agent_is_a_404_and_survives(
    client: AsyncClient,
    second_creator_token: str,
    drafted_agent_of: Callable[..., tuple[Agent, AgentVersion]],
    seeded_creator: User,
    example_agent_spec: AgentSpec,
    db_session: Session,
) -> None:
    """404 rather than 403 — on a delete most of all, "yours or not" is not a hint to give out."""
    agent, _version = drafted_agent_of(seeded_creator, example_agent_spec)

    response = await client.delete(f"/api/v1/agents/{agent.id}", headers=auth_header(second_creator_token))

    assert response.status_code == 404
    assert db_session.get(Agent, agent.id) is not None


async def test_deleting_needs_a_session(client: AsyncClient) -> None:
    assert (await client.delete("/api/v1/agents/1")).status_code == 401


async def test_an_interview_survives_the_agent_it_generated(
    client: AsyncClient,
    creator_token: str,
    drafted_agent_of: Callable[..., tuple[Agent, AgentVersion]],
    seeded_creator: User,
    example_agent_spec: AgentSpec,
    db_session: Session,
) -> None:
    """The sheet is the creator's own thinking — deleting the agent must not take it."""
    agent, _version = drafted_agent_of(seeded_creator, example_agent_spec)
    session = SolutionInterviewSession(
        owner_id=seeded_creator.id,
        description="Trả lời khiếu nại của khách",
        draft={},
        conflicts=[],
        turns=[],
        agent_id=agent.id,
    )
    db_session.add(session)
    db_session.commit()
    session_id = session.id

    response = await client.delete(f"/api/v1/agents/{agent.id}", headers=auth_header(creator_token))
    assert response.status_code == 204, response.text

    db_session.expire_all()
    survivor = db_session.get(SolutionInterviewSession, session_id)
    assert survivor is not None, "phiên phỏng vấn phải còn lại"
    assert survivor.agent_id is None, "và chỉ quên mất agent nó đã sinh ra"
    db_session.delete(survivor)
    db_session.commit()
