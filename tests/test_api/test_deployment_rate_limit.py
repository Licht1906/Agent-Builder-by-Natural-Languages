"""The ceiling each caller of a Deployment is held to — see #51 / ADR-0037.

Four claims, and every one of them is about *whose* requests were counted.
An owner can set the number; the caller who goes past it is refused in a way they
can act on; the caller beside them is untouched; and the rule is the same rule
whichever channel the call came in on.

The limits are set to one or two rather than to the platform's default of sixty,
for the obvious reason — a suite that had to send sixty chat turns to observe the
sixty-first would be scripting sixty model replies to make one assertion.

The counters are process-wide in production (ADR-0037 §4), so a fresh limiter is
installed for every test here: without it the first test's spending would be the
second test's starting point, and the failure would surface as whichever test
happened to run third.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage

from src.db.models import DeploymentAccessMode, User
from src.deployments.rate_limit import SlidingWindowLimiter, get_rate_limiter
from src.main import app
from src.models.agent_spec import AgentSpec
from tests.conftest import DEPLOYMENT_API_KEY, agent_spec, auth_header, chat_with_agent

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def fresh_limiter():
    """Give each test its own counters, and take them away again afterwards."""
    limiter = SlidingWindowLimiter()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    yield
    app.dependency_overrides.pop(get_rate_limiter, None)


def _one_step_spec() -> AgentSpec:
    return agent_spec(
        steps=[
            {
                "id": "phuc-vu",
                "title": "Phục vụ",
                "prompt": "Trả lời khách",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            }
        ]
    )


async def _set_rate_limit(client, agent, limit: int, token: str) -> Any:
    return await client.put(
        f"/api/v1/agents/{agent.id}/deployment/rate-limit",
        json={"rate_limit_per_user": limit},
        headers=auth_header(token),
    )


async def _set_access(client, agent, emails: list[str], token: str) -> Any:
    return await client.put(
        f"/api/v1/agents/{agent.id}/deployment/access",
        json={"access_mode": "selected_users", "allowed_user_emails": emails},
        headers=auth_header(token),
    )


async def _invoke_with_key(client, agent, message: str) -> Any:
    return await client.post(
        f"/api/v1/agents/{agent.id}/invoke", json={"message": message}, headers={"X-API-Key": DEPLOYMENT_API_KEY}
    )


async def _chat(client, agent, token: str, scripted_chat_model_factory) -> Any:
    """One chat turn with a model scripted for it — the shape of an *allowed* call."""
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    return await chat_with_agent(client, agent, "chào", token)


# --------------------------------------------------------------------------- #
# Setting the number
# --------------------------------------------------------------------------- #


async def test_the_owner_can_set_the_limit_on_their_deployment(
    client, creator_token, seeded_creator, deployed_agent_of
) -> None:
    agent = deployed_agent_of(seeded_creator, _one_step_spec())

    response = await _set_rate_limit(client, agent, 5, creator_token)

    assert response.status_code == 200, response.text
    assert response.json()["rate_limit_per_user"] == 5
    read_back = await client.get(f"/api/v1/agents/{agent.id}/deployment", headers=auth_header(creator_token))
    assert read_back.json()["rate_limit_per_user"] == 5


@pytest.mark.parametrize("limit", [0, -1, 100_000])
async def test_a_limit_outside_the_allowed_range_is_refused(
    client, creator_token, seeded_creator, deployed_agent_of, limit: int
) -> None:
    """Zero is not "no limit" — it is `status: disabled` said less clearly (ADR-0037 §6)."""
    agent = deployed_agent_of(seeded_creator, _one_step_spec())

    response = await _set_rate_limit(client, agent, limit, creator_token)

    assert response.status_code == 422


async def test_only_the_owner_may_change_the_limit(
    client, second_creator_token, seeded_creator, deployed_agent_of
) -> None:
    agent = deployed_agent_of(seeded_creator, _one_step_spec())

    response = await _set_rate_limit(client, agent, 5, second_creator_token)

    assert response.status_code == 404


async def test_changing_the_limit_does_not_touch_anything_else(
    client, creator_token, second_creator: User, seeded_creator, deployed_agent_of
) -> None:
    """The fourth sibling of ADR-0036 §3's four endpoints, and it stays in its lane."""
    agent = deployed_agent_of(seeded_creator, _one_step_spec())
    await _set_access(client, agent, [second_creator.email], creator_token)

    changed = (await _set_rate_limit(client, agent, 5, creator_token)).json()

    assert changed["access_mode"] == "selected_users"
    assert [user["email"] for user in changed["allowed_users"]] == [second_creator.email]
    assert changed["status"] == "active"


# --------------------------------------------------------------------------- #
# Enforcing it
# --------------------------------------------------------------------------- #


async def test_a_caller_past_their_limit_is_refused_with_a_429(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """Not a 500 and not a silent drop: the caller is entitled to be here, just not this often."""
    agent = deployed_agent_of(seeded_creator, _one_step_spec())
    await _set_rate_limit(client, agent, 1, creator_token)
    assert (await _chat(client, agent, creator_token, scripted_chat_model_factory)).status_code == 200

    refused = await _chat(client, agent, creator_token, scripted_chat_model_factory)

    assert refused.status_code == 429
    assert "1" in refused.json()["detail"]
    assert int(refused.headers["Retry-After"]) > 0


async def test_the_refusal_happens_before_the_agent_runs(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """A ceiling that stopped a run halfway would have already spent the money it exists to save."""
    agent = deployed_agent_of(seeded_creator, _one_step_spec())
    await _set_rate_limit(client, agent, 1, creator_token)
    await _chat(client, agent, creator_token, scripted_chat_model_factory)

    _, models = scripted_chat_model_factory([[AIMessage(content="xong")]])
    await chat_with_agent(client, agent, "chào", creator_token)

    assert models == []


async def test_another_caller_is_unaffected_by_someone_else_hitting_their_limit(
    client,
    creator_token,
    second_creator: User,
    second_creator_token,
    seeded_creator,
    deployed_agent_of,
    scripted_chat_model_factory,
) -> None:
    """ADR-0029 §6: per caller, never an aggregate one heavy caller could spend for everybody."""
    agent = deployed_agent_of(seeded_creator, _one_step_spec(), access_mode=DeploymentAccessMode.SELECTED_USERS)
    await _set_access(client, agent, [second_creator.email], creator_token)
    await _set_rate_limit(client, agent, 1, creator_token)

    await _chat(client, agent, creator_token, scripted_chat_model_factory)
    assert (await _chat(client, agent, creator_token, scripted_chat_model_factory)).status_code == 429

    assert (await _chat(client, agent, second_creator_token, scripted_chat_model_factory)).status_code == 200


async def test_the_api_channel_is_held_to_the_same_ceiling(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """One rule, both channels — the uniformity ADR-0029 §5 asks for."""
    agent = deployed_agent_of(seeded_creator, _one_step_spec())
    await _set_rate_limit(client, agent, 1, creator_token)
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    assert (await _invoke_with_key(client, agent, "chào")).status_code == 200

    refused = await _invoke_with_key(client, agent, "chào")

    assert refused.status_code == 429
    # An integration reads headers, not Vietnamese prose: both numbers it needs
    # to back off correctly are there without parsing a sentence.
    assert int(refused.headers["Retry-After"]) > 0
    assert refused.headers["X-RateLimit-Limit"] == "1"


async def test_an_integration_out_of_quota_does_not_lock_out_the_web_chat(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """The key is its own caller (`k{deployment_id}`), so its allowance is its own."""
    agent = deployed_agent_of(seeded_creator, _one_step_spec())
    await _set_rate_limit(client, agent, 1, creator_token)
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    await _invoke_with_key(client, agent, "chào")
    assert (await _invoke_with_key(client, agent, "chào")).status_code == 429

    assert (await _chat(client, agent, creator_token, scripted_chat_model_factory)).status_code == 200


async def test_the_limit_is_the_one_the_owner_set_not_the_default(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """Two allowed turns and then a refusal, on a Deployment whose column says two."""
    agent = deployed_agent_of(seeded_creator, _one_step_spec())
    await _set_rate_limit(client, agent, 2, creator_token)

    assert (await _chat(client, agent, creator_token, scripted_chat_model_factory)).status_code == 200
    assert (await _chat(client, agent, creator_token, scripted_chat_model_factory)).status_code == 200

    assert (await _chat(client, agent, creator_token, scripted_chat_model_factory)).status_code == 429
