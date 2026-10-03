"""The creator's own flow diagram, and the permissions under it — #20.

The spec screen (#19) already carries the steps. What it cannot derive is the
graph or the risk levels: `next=None` means "the next step in the list", a rule
`AgentSpec` owns, and a tool's risk is the registry's and never the spec's. A
screen that re-derived either would eventually disagree with the runtime about
what the agent does, or with the platform about how dangerous it is.

So both come off one endpoint, scoped to the owner exactly like the version read
it sits beside.
"""

from collections.abc import Callable, Generator

import pytest
from sqlalchemy.orm import Session

from src.agents.versions import create_draft
from src.config import get_settings
from src.db.models import Agent, User
from src.models.agent_spec import AgentSpec
from tests.conftest import agent_spec, auth_header, discard_agent

pytestmark = pytest.mark.asyncio


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


def _three_step_spec() -> AgentSpec:
    """Three steps in a row, the middle one asking for a tool nobody waves through.

    `next` is left `None` throughout on purpose: the fall-through rule is the
    thing the endpoint is being asked to resolve, and a spec that spelled every
    successor out would prove nothing about it.
    """
    return agent_spec(
        steps=[
            {
                "id": "doc",
                "title": "Đọc khiếu nại",
                "prompt": "Đọc email và trích mã đơn.",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            },
            {
                "id": "tra-cuu",
                "title": "Tra đơn hàng",
                "prompt": "Tra trạng thái đơn hàng.",
                "model": "gpt-4.1",
                "tools": ["db_query", "send_email"],
                "max_iterations": 3,
                "next": None,
            },
            {
                "id": "tra-loi",
                "title": "Soạn thư trả lời",
                "prompt": "Viết thư trả lời khách.",
                "model": "gpt-4.1",
                "tools": ["send_email"],
                "max_iterations": 2,
                "next": None,
            },
        ]
    )


async def test_flow_is_the_steps_joined_in_the_order_they_run(
    client, creator_token, seeded_creator, agent_of, db_session
) -> None:
    """The graph the screen draws, resolved by the same rule the runtime executes by.

    Every `next` in the spec is `None`, so the two edges here exist only because
    `AgentSpec.next_step_id` says fall-through means the next step in the list.
    The last step has no outgoing edge, which is how a reader sees where a run
    ends without a marker saying so.
    """
    agent = agent_of(seeded_creator, "Agent ba bước")
    version = create_draft(db_session, agent, _three_step_spec())

    response = await client.get(
        f"/api/v1/agents/{agent.id}/versions/{version.id}/flow", headers=auth_header(creator_token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["version_id"] == version.id
    assert [node["step_id"] for node in body["flow"]["nodes"]] == ["doc", "tra-cuu", "tra-loi"]
    assert body["flow"]["nodes"][1]["title"] == "Tra đơn hàng"
    assert body["flow"]["nodes"][1]["model"] == "gpt-4.1"
    assert body["flow"]["edges"] == [
        {"from_step_id": "doc", "to_step_id": "tra-cuu"},
        {"from_step_id": "tra-cuu", "to_step_id": "tra-loi"},
    ]
