"""Cumulative budget enforcement (#10): the run stops, and says which ceiling it hit.

The claim this file pins down is that `Budget` is a **mechanism**, not a number on
a screen: a run that spends past its token ceiling or runs past its time ceiling
is stopped mid-flight, and the client is told which ceiling in a structured event
rather than left with a hung stream or a raw traceback.

The property that makes it worth having is that spend is **cumulative**. Every
scenario here is built so that each step on its own stays comfortably inside the
ceiling — a runtime that charged each step separately, or each model call
separately, would let all of them run to the end. Only a ledger carried across
the whole run stops them.

Observed through HTTP like the rest of the suite: nothing here imports
`src.agents.budget` or `src.agents.runtime`. The seam is the model factory, and
what a scripted model reports about its own usage is what a real provider reports.
"""

from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata

from src.models.agent_spec import AgentSpec
from tests.conftest import (
    ScriptedRun,
    agent_spec,
    chat_with_agent,
    parse_sse,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

#: What each scripted model call reports having spent.
_TOKENS_PER_CALL = 500

#: How many steps a scenario spec has. Four, so that after the ceiling is crossed
#: there is still a step left for the run to be stopped *before* — with three, the
#: run would simply have finished, and "it stopped" would prove nothing.
_STEPS = 4

#: Crossed by the third call and not by the second, so the run is stopped on its
#: way into step 4. No single step can reach it, which is what makes a per-step
#: ledger unable to pass these tests.
_TOKEN_CEILING = 2 * _TOKENS_PER_CALL + _TOKENS_PER_CALL // 2

#: What the run has spent by the time it is stopped.
_TOKENS_AT_STOP = 3 * _TOKENS_PER_CALL


def _usage(total_tokens: int) -> UsageMetadata:
    """A provider's own report of what one call cost.

    Split across input and output because that is the shape providers use; the
    ledger's business is the total.
    """
    output_tokens = total_tokens // 5
    return UsageMetadata(
        input_tokens=total_tokens - output_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _answer(text: str, *, tokens: int = _TOKENS_PER_CALL) -> AIMessage:
    """One scripted answer that reports its own token usage, as a real provider does."""
    return AIMessage(content=text, usage_metadata=_usage(tokens))


def _script_one_answer_per_step(
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    *,
    steps: int = _STEPS,
) -> None:
    """Arm the seam so each step answers once, reporting `_TOKENS_PER_CALL`.

    The scenario nearly every test here shares: identical, modest calls whose
    only remarkable property is that they add up.
    """
    scripted_chat_model_factory([[_answer(f"xong {index}")] for index in range(1, steps + 1)])


def _budgeted_spec(
    *,
    steps: int = _STEPS,
    tools: list[str] | None = None,
    max_iterations: int = 1,
    max_total_tokens: int = _TOKEN_CEILING,
    max_wall_seconds: int = 60,
) -> AgentSpec:
    """A linear spec of `steps` steps under the given ceilings."""
    return agent_spec(
        steps=[
            {
                "id": f"s{index}",
                "title": f"Bước {index}",
                "prompt": "p",
                "model": "gpt-4o",
                "tools": tools or [],
                "max_iterations": max_iterations,
                "next": None,
            }
            for index in range(1, steps + 1)
        ],
        budget={"max_total_tokens": max_total_tokens, "max_wall_seconds": max_wall_seconds},
    )


def budget_event(body: str) -> dict[str, Any]:
    """The one event a stopped run ends with. Asserting there is exactly one is part of it."""
    events = parse_sse(body, event="budget_exceeded")
    assert len(events) == 1, body
    return events[0]


def completed_step_ids(body: str) -> list[str]:
    """Which steps got all the way to an answer — the work that survived the stop."""
    return [event["step_id"] for event in parse_sse(body, event="step")]


# --------------------------------------------------------------------------- #
# The token ceiling
# --------------------------------------------------------------------------- #


async def test_a_run_over_its_token_ceiling_is_stopped_with_a_structured_error(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """Structured and readable: which ceiling, what it was, what the run had spent."""
    agent = deployed_agent_of(seeded_creator, _budgeted_spec())
    _script_one_answer_per_step(scripted_chat_model_factory)

    response = await chat_with_agent(client, agent, "chạy đi", creator_token)

    assert response.status_code == 200, response.text
    event = budget_event(response.text)
    assert event["limit"] == "tokens"
    assert event["limit_value"] == _TOKEN_CEILING
    assert event["used"] == _TOKENS_AT_STOP
    # The step the run never got to start, which is what "stopped" means here.
    assert event["step_id"] == "s4"
    assert "token" in event["message"]


async def test_a_run_inside_its_budget_is_left_alone(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """The other half of enforcement: an ordinary run must not be cut short.

    The same steps reporting the same spend, against a ceiling that fits them
    exactly — so this also pins that the ceiling is a maximum and not a target to
    stay under.
    """
    agent = deployed_agent_of(seeded_creator, _budgeted_spec(max_total_tokens=_STEPS * _TOKENS_PER_CALL))
    _script_one_answer_per_step(scripted_chat_model_factory)

    response = await chat_with_agent(client, agent, "chạy đi", creator_token)

    assert completed_step_ids(response.text) == ["s1", "s2", "s3", "s4"]
    assert parse_sse(response.text, event="budget_exceeded") == []
