"""What the chat window can show while an answer is still being written (#8).

Two of ticket #8's acceptance criteria are claims about the *stream*, not about
the page: the answer appears bit by bit rather than in one lump, and the user
can see which tool the agent is reaching for. Neither is observable from a
frontend test — a chat window can only render what arrives — so both are
pinned here, at the same HTTP seam every other runtime test uses.

Nothing here imports `src.agents.runtime`. The events are read off the SSE
response exactly as the browser reads them.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage

from tests.conftest import agent_spec as _agent_spec
from tests.conftest import chat_with_agent, parse_sse, parse_sse_stream

pytestmark = pytest.mark.asyncio

_chat = chat_with_agent


def _one_step_spec(*, tools: list[str]) -> Any:
    """One step — the shape that would show a reader a blank screen if nothing streamed."""
    return _agent_spec(
        steps=[
            {
                "id": "reply",
                "title": "Trả lời",
                "prompt": "p",
                "model": "gpt-4o",
                "tools": tools,
                "max_iterations": 3,
                "next": None,
            }
        ]
    )


def _event_names(body: str) -> list[str]:
    return [name for name, _ in parse_sse_stream(body)]


# --------------------------------------------------------------------------- #
# The answer appears bit by bit
# --------------------------------------------------------------------------- #


async def test_an_answer_arrives_in_pieces_before_the_step_finishes(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    agent = deployed_agent_of(seeded_creator, _one_step_spec(tools=[]))
    scripted_chat_model_factory([[AIMessage(content="một hai ba bốn năm")]])

    response = await _chat(client, agent, "xin chào", creator_token)

    assert response.status_code == 200
    tokens = parse_sse(response.text, event="token")
    # More than one, or "appears bit by bit" is just the whole answer relabelled.
    assert len(tokens) > 1
    assert "".join(token["text"] for token in tokens) == "một hai ba bốn năm"
    # Every piece arrives before the step it belongs to is announced as done —
    # the last one included, which is what says the answer was streamed rather
    # than replayed after the fact. Not "the token is the event immediately
    # before the step": the run also reports what it has spent as it goes (#21),
    # and that lands between the last token and the step it paid for.
    names = _event_names(response.text)
    assert names.index("token") < names.index("step")
    assert max(index for index, name in enumerate(names) if name == "token") < names.index("step")
