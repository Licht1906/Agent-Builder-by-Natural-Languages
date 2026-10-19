"""What a Deployment has actually been doing — the operations dashboard, #55 / ADR-0039.

The claim under test is not that the arithmetic adds up. It is that the numbers
are about **the Deployment serving people**, and nothing else. A creator reads
this screen to answer "is my agent all right", and the two ways that answer goes
wrong are both about what got counted: their own rehearsals inflating the traffic,
and a run that died being filed as a run that went fine.

So the sharp assertions are the exclusions and the failure. Everything is
observed over HTTP, and every run in here is a real run through the real runtime
— the ledger this screen reads is written by the runtime, not by the test.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata

from src.models.agent_spec import AgentSpec
from tests.conftest import (
    agent_spec,
    auth_header,
    chat_with_agent,
    install_chat_model_factory,
    trial_run_version,
)

pytestmark = pytest.mark.asyncio

_INPUT_TOKENS, _OUTPUT_TOKENS = 400, 100


def _answer(text: str) -> AIMessage:
    """One scripted answer reporting its own usage, the way a real provider does."""
    return AIMessage(
        content=text,
        usage_metadata=UsageMetadata(
            input_tokens=_INPUT_TOKENS, output_tokens=_OUTPUT_TOKENS, total_tokens=_INPUT_TOKENS + _OUTPUT_TOKENS
        ),
    )


def _one_step_spec() -> AgentSpec:
    return agent_spec(
        steps=[
            {
                "id": "s",
                "title": "Bước duy nhất",
                "prompt": "p",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            }
        ]
    )


async def read_dashboard(client, agent, token: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/agents/{agent.id}/deployment/operations", headers=auth_header(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


# --------------------------------------------------------------------------- #
# The four numbers, and the list under them
# --------------------------------------------------------------------------- #


async def test_a_deployment_nobody_has_used_reports_zeroes_rather_than_a_404(
    client, creator_token, seeded_creator, deployed_agent_of
) -> None:
    """ "Nothing yet" is a true answer, and the honest one for a freshly published agent.

    A 404 here would make the dashboard fail for exactly the agents whose owner
    has just published them and is watching hardest.
    """
    agent = deployed_agent_of(seeded_creator, _one_step_spec(), name="Agent chưa ai dùng")

    board = await read_dashboard(client, agent, creator_token)

    assert (board["runs"], board["errors"], board["tokens"]) == (0, 0, 0)
    assert board["cost_usd"] == 0.0
    assert board["recent_executions"] == []


async def test_production_turns_are_counted_with_their_tokens_and_cost(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    agent = deployed_agent_of(seeded_creator, _one_step_spec(), name="Agent đang phục vụ")
    for turn in ("một", "hai"):
        scripted_chat_model_factory([[_answer(f"trả lời {turn}")]])
        assert (await chat_with_agent(client, agent, turn, creator_token)).status_code == 200

    board = await read_dashboard(client, agent, creator_token)

    assert board["runs"] == 2
    assert board["errors"] == 0
    assert board["tokens"] == 2 * (_INPUT_TOKENS + _OUTPUT_TOKENS)
    assert board["cost_usd"] > 0.0
    assert len(board["recent_executions"]) == 2
    assert board["recent_executions"][0]["tokens"] == _INPUT_TOKENS + _OUTPUT_TOKENS


async def test_recent_executions_arrive_newest_first(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """The list is a "what just happened", so the run being asked about is at the top."""
    agent = deployed_agent_of(seeded_creator, _one_step_spec(), name="Agent nhiều lượt")
    for turn in ("một", "hai", "ba"):
        scripted_chat_model_factory([[_answer(f"trả lời {turn}")]])
        assert (await chat_with_agent(client, agent, turn, creator_token)).status_code == 200

    board = await read_dashboard(client, agent, creator_token)

    ids = [execution["id"] for execution in board["recent_executions"]]
    assert ids == sorted(ids, reverse=True)


# --------------------------------------------------------------------------- #
# A run that died — the reason this screen is worth opening
# --------------------------------------------------------------------------- #


async def test_a_run_that_died_is_counted_as_an_error_and_says_what_killed_it(
    client, creator_token, seeded_creator, deployed_agent_of
) -> None:
    """The distinction the whole dashboard turns on.

    Before #55 the ledger recorded what a broken run *cost* and nothing about it
    having broken, so a creator reading their run count could not tell a healthy
    agent from one failing every call. A row that says only "this run happened"
    is a row that describes the two cases identically.
    """
    agent = deployed_agent_of(seeded_creator, _one_step_spec(), name="Agent hỏng")

    def _dies(model_id: str) -> Any:
        raise RuntimeError("nhà cung cấp model từ chối")

    install_chat_model_factory(_dies)
    response = await chat_with_agent(client, agent, "chào", creator_token)
    assert response.status_code == 200

    board = await read_dashboard(client, agent, creator_token)

    assert board["runs"] == 1
    assert board["errors"] == 1
    assert "từ chối" in board["recent_executions"][0]["error"]


async def test_a_run_that_finished_carries_no_error(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    agent = deployed_agent_of(seeded_creator, _one_step_spec(), name="Agent lành")
    scripted_chat_model_factory([[_answer("xong")]])
    assert (await chat_with_agent(client, agent, "chào", creator_token)).status_code == 200

    board = await read_dashboard(client, agent, creator_token)

    assert board["recent_executions"][0]["error"] is None
    assert board["errors"] == 0


# --------------------------------------------------------------------------- #
# What is not deployment traffic
# --------------------------------------------------------------------------- #


async def test_the_creators_own_rehearsals_are_not_deployment_traffic(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """Both kinds of rehearsal stay off the board, and they are two different kinds.

    A sandbox run simulates its tools; a `trial` chat turn on a deployed version
    calls the real ones and is filed as a real-world run for exactly that reason
    (ADR-0009 §4). Neither is somebody *using* the agent, which is the only
    question this screen answers — so the ledger has to know which world a run's
    conversation belonged to, not just whether its tools were simulated.
    """
    agent = deployed_agent_of(seeded_creator, _one_step_spec(), name="Agent đang tập")

    scripted_chat_model_factory([[_answer("thử")]])
    assert (await chat_with_agent(client, agent, "thử", creator_token, namespace="trial")).status_code == 200

    scripted_chat_model_factory([[_answer("chạy thử")]])
    assert (await trial_run_version(client, agent, agent.versions[0], "chạy thử", creator_token)).status_code == 200

    board = await read_dashboard(client, agent, creator_token)

    assert board["runs"] == 0
    assert board["recent_executions"] == []


# --------------------------------------------------------------------------- #
# Who may read it
# --------------------------------------------------------------------------- #


async def test_another_creator_cannot_read_the_dashboard(
    client, second_creator_token, seeded_creator, deployed_agent_of
) -> None:
    """Owner-only, like every other Deployment setting (ADR-0036).

    Being allowed to *talk* to a published agent is not being allowed to see how
    much it is being talked to, what it costs its owner, or which conversations
    it has had.
    """
    agent = deployed_agent_of(seeded_creator, _one_step_spec(), name="Agent của người khác")

    response = await client.get(
        f"/api/v1/agents/{agent.id}/deployment/operations", headers=auth_header(second_creator_token)
    )

    assert response.status_code == 404


async def test_an_agent_nobody_published_has_no_dashboard(
    client, creator_token, seeded_creator, drafted_agent_of
) -> None:
    agent, _ = drafted_agent_of(seeded_creator, _one_step_spec(), name="Agent chưa publish")

    response = await client.get(f"/api/v1/agents/{agent.id}/deployment/operations", headers=auth_header(creator_token))

    assert response.status_code == 404
    assert "publish" in response.json()["detail"]
