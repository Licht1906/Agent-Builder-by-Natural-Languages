"""The runtime tracer bullet (#5, ADR-0003): a hand-written `AgentSpec` is executable.

Per the spec's testing policy, everything here is observed through HTTP — no
test imports `src.agents.runtime` directly, and none asserts the shape of the
compiled graph. The only seams used are the ones the project already committed
to: the `client` fixture (HTTP) and `get_chat_model_factory` (the LLM).

Tools are *not* mocked: `calculate` and `read_file` run for real (see
ADR-0003), which is what proves a tool call's real result reaches the model.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage

from tests.conftest import agent_spec as _agent_spec
from tests.conftest import chat_with_agent, parse_sse

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Helpers. The heavy fixtures — `deployed_agent_of`, `seeded_creator`,
# `scripted_chat_model_factory` — live in `tests/conftest.py`, shared with the
# conversation-memory suite (#6) so there is one definition of "an agent you can
# chat with" rather than one per ticket.
# --------------------------------------------------------------------------- #


_chat = chat_with_agent


def _parse_sse(body: str) -> list[dict[str, Any]]:
    """The `step` payloads — the leading `thread` event (#6) is not this suite's subject."""
    return parse_sse(body, event="step")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_a_multi_step_spec_executes_in_step_order(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    spec = _agent_spec(
        steps=[
            {
                "id": "extract",
                "title": "Trích xuất",
                "prompt": "p1",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            },
            {
                "id": "analyze",
                "title": "Phân tích",
                "prompt": "p2",
                "model": "gpt-4o",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            },
            {
                "id": "reply",
                "title": "Trả lời",
                "prompt": "p3",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            },
        ]
    )
    agent = deployed_agent_of(seeded_creator, spec)
    requested_model_ids, _ = scripted_chat_model_factory(
        [
            [AIMessage(content="kết quả bước 1")],
            [AIMessage(content="kết quả bước 2")],
            [AIMessage(content="kết quả bước cuối")],
        ]
    )

    response = await _chat(client, agent, "xin chào", creator_token)

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert [event["step_id"] for event in events] == ["extract", "analyze", "reply"]
    assert [event["content"] for event in events] == ["kết quả bước 1", "kết quả bước 2", "kết quả bước cuối"]
    assert events[-1]["title"] == "Trả lời"
    # Each step used exactly the model it declared, via the one factory function.
    assert requested_model_ids == ["gpt-4o-mini", "gpt-4o", "gpt-4o-mini"]
