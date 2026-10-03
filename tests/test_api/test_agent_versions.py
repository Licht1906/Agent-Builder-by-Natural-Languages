"""HTTP surface for the version-lifecycle read paths — #7 / ADR-0005.

Only the *read* endpoints (list, compare) belong to this ticket; approve,
reject, deploy and rollback are HTTP surface owned by #14 and #15 and are not
tested here. Ownership follows the same rule as every other agent endpoint
(ADR-0002): another creator's agent, or one of its versions, is a 404.
"""

from collections.abc import Callable, Generator
from typing import Any

import pytest
from sqlalchemy.orm import Session

from src.agents.versions import approve, create_draft, mark_sandbox_passed, request_approval
from src.config import get_settings
from src.db.models import Agent, AgentVersion, User, VersionStatus
from src.deployments.publication import publish
from src.models.agent_spec import AgentSpec
from tests.conftest import auth_header, discard_agent, record_passing_eval

pytestmark = pytest.mark.asyncio

#: The smallest graph a V2 version can hold — one answering node and an `End`.
_WORKFLOW: dict[str, Any] = {
    "start_node_id": "answer",
    "nodes": [
        {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời", "model": "gpt-4o-mini"}},
        {"id": "done", "type": "end", "config": {}},
    ],
    "edges": [{"id": "e1", "source_node_id": "answer", "source_port": "done", "target_node_id": "done"}],
    "metadata": {},
}


@pytest.fixture
def seeded_creator(db_session: Session) -> User:
    settings = get_settings()
    return db_session.query(User).filter(User.email == settings.seed_creator_email).one()


@pytest.fixture
def agent_of(db_session: Session) -> Generator[Callable[[User, str], Agent], None, None]:
    created: list[Agent] = []

    def _make(owner: User, name: str) -> Agent:
        agent = Agent(owner_id=owner.id, name=name)
        db_session.add(agent)
        db_session.commit()
        db_session.refresh(agent)
        created.append(agent)
        return agent

    yield _make

    for agent in created:
        discard_agent(db_session, agent)


def _step_spec(*, name: str) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "name": name,
            "description": "Agent test cho luồng đọc version.",
            "slots": {
                "audience": "người kiểm thử",
                "inputs": "yêu cầu giả lập trong test",
                "capabilities": [],
                "success_criteria": [],
                "prohibitions": [],
            },
            "steps": [
                {
                    "id": "s",
                    "title": "S",
                    "prompt": "p",
                    "model": "gpt-4o-mini",
                    "tools": [],
                    "max_iterations": 1,
                    "next": None,
                }
            ],
            "guardrails": [],
            "budget": {"max_total_tokens": 20000, "max_wall_seconds": 60},
            "allowed_domains": [],
        }
    )


async def test_comparing_two_versions_answers_with_the_pipeline_half_and_no_graph(
    client, creator_token, seeded_creator, agent_of, db_session
) -> None:
    agent = agent_of(seeded_creator, "Agent để so sánh")
    v1 = create_draft(db_session, agent, _step_spec(name="v1"))
    v2 = create_draft(db_session, agent, _step_spec(name="v2"))

    response = await client.get(
        f"/api/v1/agents/{agent.id}/versions/compare",
        params={"version_a_id": v1.id, "version_b_id": v2.id},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["version_a_id"], body["version_b_id"]) == (v1.id, v2.id)
    assert body["spec"]["changed_fields"]["name"] == ["v1", "v2"]
    # Neither version is a V2 one, so there is no graph to speak of — and nothing
    # was skipped, because "no workflow either side" is not a refusal.
    assert body["workflow"] is None
    assert body["skipped"] == {}


async def test_comparing_two_v2_versions_answers_with_the_graph_half(
    client, creator_token, seeded_creator, agent_of, db_session
) -> None:
    agent = agent_of(seeded_creator, "Agent V2 để so sánh")
    rewired: dict[str, Any] = {
        **_WORKFLOW,
        "nodes": [*_WORKFLOW["nodes"], {"id": "search", "type": "web_search", "config": {}}],
    }
    versions = [
        AgentVersion(agent_id=agent.id, workflow=workflow, status=VersionStatus.WORKFLOW_CONFIRMED)
        for workflow in (_WORKFLOW, rewired)
    ]
    db_session.add_all(versions)
    db_session.commit()

    response = await client.get(
        f"/api/v1/agents/{agent.id}/versions/compare",
        params={"version_a_id": versions[0].id, "version_b_id": versions[1].id},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["workflow"]["added_node_ids"] == ["search"]
    assert body["spec"] is None
    assert body["skipped"] == {}


async def test_comparing_a_version_against_one_at_an_earlier_stage_says_which_document_is_missing(
    client, creator_token, seeded_creator, agent_of, db_session
) -> None:
    agent = agent_of(seeded_creator, "Agent nửa chừng")
    v1 = create_draft(db_session, agent, _step_spec(name="v1"))
    v2 = AgentVersion(agent_id=agent.id, workflow=_WORKFLOW, status=VersionStatus.WORKFLOW_CONFIRMED)
    db_session.add(v2)
    db_session.commit()

    response = await client.get(
        f"/api/v1/agents/{agent.id}/versions/compare",
        params={"version_a_id": v1.id, "version_b_id": v2.id},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 409
    assert str(v1.id) in response.json()["detail"]


async def test_listing_versions_is_ordered_and_flags_the_deployed_one(
    client, creator_token, seeded_creator, agent_of, db_session
) -> None:
    agent = agent_of(seeded_creator, "Agent với version")
    v1 = create_draft(db_session, agent, _step_spec(name="v1"))
    v1 = approve(
        db_session, request_approval(db_session, record_passing_eval(db_session, mark_sandbox_passed(db_session, v1)))
    )
    publish(db_session, agent, v1, publisher=seeded_creator)
    v2 = create_draft(db_session, agent, _step_spec(name="v2"))

    response = await client.get(f"/api/v1/agents/{agent.id}/versions", headers=auth_header(creator_token))

    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body] == [v1.id, v2.id]
    assert body[0]["is_deployed"] is True
    assert body[0]["status"] == "approved"
    assert body[1]["is_deployed"] is False
    assert body[1]["status"] == "draft"
