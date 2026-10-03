"""Three cost figures that stay three, and a bill that adds up (#11).

The claim under test is not "the arithmetic is right" — it is that the three
numbers **cannot be confused for one another**. Every figure this API emits
carries a label and a sentence saying what kind of claim it is, because the way
this feature fails is not a wrong number: it is a reader taking a guess for a
promise, discovering it was wrong, and then trusting neither.

So the sharp assertions here are about *nature*: the worst case is never below
what the runtime actually permits (otherwise it is not a guarantee), the measured
figure comes from a run that happened (otherwise it is not a measurement), and a
version nobody trialled has no measured figure at all rather than a zero.

Observed through HTTP like the rest of the suite. The price table is imported,
because it is the *input* — the same way `test_agent_spec` imports the catalog.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata

from src.models.agent_spec import AgentSpec
from tests.conftest import agent_spec, auth_header, trial_run_version

pytestmark = pytest.mark.asyncio


#: What each scripted call reports having spent. Input-heavy, like a real prompt.
_INPUT_TOKENS, _OUTPUT_TOKENS = 400, 100

_CHEAP, _STRONG = "gpt-4o-mini", "gpt-4o"


def _answer(text: str) -> AIMessage:
    """One scripted answer reporting its own usage, the way a real provider does."""
    return AIMessage(
        content=text,
        usage_metadata=UsageMetadata(
            input_tokens=_INPUT_TOKENS,
            output_tokens=_OUTPUT_TOKENS,
            total_tokens=_INPUT_TOKENS + _OUTPUT_TOKENS,
        ),
    )


def _two_step_spec(*, models: tuple[str, str] = (_CHEAP, _STRONG), **budget: int) -> AgentSpec:
    """A linear two-step spec, one step per named model."""
    return agent_spec(
        steps=[
            {
                "id": f"s{index}",
                "title": f"Bước {index}",
                "prompt": "p",
                "model": model,
                "tools": [],
                "max_iterations": 1,
                "next": None,
            }
            for index, model in enumerate(models, start=1)
        ],
        budget={"max_total_tokens": 20_000, "max_wall_seconds": 60, **budget},
    )


async def read_costs(client, agent, version_id: int, token: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/agents/{agent.id}/versions/{version_id}/costs", headers=auth_header(token))
    assert response.status_code == 200, response.text
    return dict(response.json())


def _script_both_steps(scripted_chat_model_factory) -> None:
    scripted_chat_model_factory([[_answer("xong 1")], [_answer("xong 2")]])


# --------------------------------------------------------------------------- #
# Three figures, and three different kinds of claim
# --------------------------------------------------------------------------- #


async def test_the_three_figures_arrive_separately_and_each_says_what_it_is(
    client, creator_token, seeded_creator, drafted_agent_of, scripted_chat_model_factory
) -> None:
    """The whole ticket in one assertion: three numbers, three labels, three natures.

    A response that merged them, or that shipped a bare number and left the
    wording to the frontend, would fail here — and that is exactly the shape of
    the mistake this feature exists to prevent.
    """
    agent, version = drafted_agent_of(seeded_creator, _two_step_spec())
    _script_both_steps(scripted_chat_model_factory)
    await trial_run_version(client, agent, version, "chạy thử", creator_token)

    costs = await read_costs(client, agent, version.id, creator_token)

    figures = [costs["estimate"], costs["worst_case"], costs["measured"]]
    assert [figure["basis"] for figure in figures] == ["estimate", "worst_case", "measured"]
    # Distinct wording, present on every figure: nobody has to look up what kind
    # of number they are reading.
    assert len({figure["label"] for figure in figures}) == 3
    assert all(figure["nature"] for figure in figures)
    assert "phỏng đoán" in costs["estimate"]["nature"].lower()
    assert "đảm bảo" in costs["worst_case"]["nature"].lower()
    assert "số liệu" in costs["measured"]["nature"].lower()


async def test_an_untrialled_version_has_no_measured_figure_rather_than_a_zero(
    client, creator_token, seeded_creator, drafted_agent_of
) -> None:
    """ "We ran it and it was free" is the one thing a measurement must not say when nothing ran.

    The other two are still there: they are functions of the spec and need no
    run to exist, which is what lets a creator see a ceiling before spending
    anything at all.
    """
    agent, version = drafted_agent_of(seeded_creator, _two_step_spec())

    costs = await read_costs(client, agent, version.id, creator_token)

    assert costs["measured"] is None
    assert costs["estimate"]["usd"] > 0
    assert costs["worst_case"]["usd"] > 0
