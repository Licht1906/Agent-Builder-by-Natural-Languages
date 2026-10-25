"""A creator sees and edits their own agents, and nobody else's.

Ticket #4. The isolation rule is tested here, on the smallest possible agent
endpoints, so that every richer agent endpoint added later (#5, #7) inherits an
ownership rule that already has teeth.
"""

from collections.abc import Callable, Generator

import pytest
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import Agent, User
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


@pytest.fixture
def agent_of(db_session: Session) -> Generator[Callable[[User, str], Agent], None, None]:
    """Make an agent owned by `owner`, cleaned up afterwards."""
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
        db_session.delete(agent)
    db_session.commit()


@pytest.fixture
def seeded_creator(db_session: Session) -> User:
    """The seeded creator account, as an ORM row — the owner `creator_token` speaks for."""
    settings = get_settings()
    user = db_session.query(User).filter(User.email == settings.seed_creator_email).one()
    return user


async def test_listing_shows_only_your_own_agents(
    client, creator_token, seeded_creator, second_creator, agent_of
) -> None:
    mine = agent_of(seeded_creator, "Agent của tôi")
    theirs = agent_of(second_creator, "Agent của người khác")

    response = await client.get("/api/v1/agents", headers=auth_header(creator_token))

    assert response.status_code == 200
    listed_ids = [agent["id"] for agent in response.json()]
    assert mine.id in listed_ids
    assert theirs.id not in listed_ids
