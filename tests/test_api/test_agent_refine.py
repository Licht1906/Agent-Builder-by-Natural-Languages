"""Refining an agent by chat, read through HTTP — see #18.

The claims here are the ones only the whole path can make: that a sentence
becomes an ordinary **draft** in the version lifecycle, that the version it was
refined from is left exactly as it was, that the version serving real users keeps
serving them throughout, and that a refinement nobody could make valid costs the
creator nothing but a message.

Every model is a scripted double. What the refined spec says is the double's
business; what happens to it — whether it is saved, what it is saved beside, and
what it leaves alone — is the platform's, and that is what each test is about.
"""

from typing import Any

import pytest
from sqlalchemy.orm import Session

from src.db.models import Agent, AgentVersion, User, VersionStatus
from src.models.agent_spec import AgentSpec
from tests.conftest import auth_header, refinable_spec_document, refinable_spec_without, writes_json

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

DROP_THE_LAST_STEP = "bỏ bước soạn thư"


def current_spec() -> AgentSpec:
    """The three-step agent every test here starts from."""
    return AgentSpec.model_validate(refinable_spec_document())


async def refine(client: Any, token: str, agent: Agent, version: AgentVersion, instruction: str) -> Any:
    return await client.post(
        f"/api/v1/agents/{agent.id}/versions/{version.id}/refine",
        json={"instruction": instruction},
        headers=auth_header(token),
    )


# --------------------------------------------------------------------------- #
# A turn that worked
# --------------------------------------------------------------------------- #


async def test_a_sentence_becomes_a_new_draft_version_of_the_same_agent(
    client, creator_token, seeded_creator: User, drafted_agent_of, scripted_chat_model_factory, db_session: Session
) -> None:
    """The ticket in one request: *"bỏ bước 3"* in, a saved draft of the same agent out."""
    agent, version = drafted_agent_of(seeded_creator, current_spec())
    scripted_chat_model_factory([[writes_json(refinable_spec_without("soan-thu"))]])

    response = await refine(client, creator_token, agent, version, DROP_THE_LAST_STEP)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["agent_id"] == agent.id
    assert payload["refined_from_version_id"] == version.id
    assert payload["status"] == VersionStatus.DRAFT.value
    assert [step["id"] for step in payload["spec"]["steps"]] == ["doc-email", "tra-cuu"]
