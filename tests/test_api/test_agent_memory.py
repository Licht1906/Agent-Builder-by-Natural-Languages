"""In-session conversation memory (#6, ADR-0004): threads, namespaces, readback.

Everything runs against the real Postgres the rest of the suite uses — that is
the point of the ticket, not an accident of the setup. A `MemorySaver` would
pass every assertion here except the one that matters (a thread outliving the
process), which is exactly why the checkpointer is Postgres-only.

Observed through HTTP like the rest of the suite. The one exception is
`close_checkpointer`, imported directly to simulate a process restart: there is
no HTTP request that restarts the server.
"""

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from tests.conftest import ScriptedToolModel, chat_with_agent, single_step_spec

pytestmark = pytest.mark.asyncio


def _conversation_id() -> str:
    """A conversation nobody has used before, so "empty history" means something."""
    return uuid4().hex


_chat = chat_with_agent


def _human_texts(messages: list[BaseMessage]) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, HumanMessage)]


def _last_batch_seen_by(created_models: list[ScriptedToolModel]) -> list[BaseMessage]:
    """The context the step actually ran with — the only way to observe memory from outside."""
    return created_models[-1].received_message_batches[0]


# --------------------------------------------------------------------------- #
# Same thread remembers; a new thread does not
# --------------------------------------------------------------------------- #


async def test_a_second_turn_in_the_same_thread_sees_the_first_turns_context(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    agent = deployed_agent_of(seeded_creator, single_step_spec())
    conversation = _conversation_id()

    scripted_chat_model_factory([[AIMessage(content="Hà Nội là thủ đô Việt Nam")]])
    first = await _chat(client, agent, "thủ đô Việt Nam là gì?", creator_token, conversation_id=conversation)
    assert first.status_code == 200, first.text

    _, second_models = scripted_chat_model_factory([[AIMessage(content="Khoảng 8 triệu người")]])
    second = await _chat(client, agent, "còn dân số thì sao?", creator_token, conversation_id=conversation)

    assert second.status_code == 200, second.text
    # The follow-up question is answerable because the model was handed the
    # first turn — both what the user asked and what the agent replied.
    seen = _last_batch_seen_by(second_models)
    assert "thủ đô Việt Nam là gì?" in _human_texts(seen)
    assert any(str(m.content) == "Hà Nội là thủ đô Việt Nam" for m in seen if isinstance(m, AIMessage))
    assert _human_texts(seen)[-1] == "còn dân số thì sao?"


# --------------------------------------------------------------------------- #
# A conversation id cannot reach outside its own thread
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "forged",
    [
        "prod:1:stolen",  # a fully-formed thread key for someone else's agent
        "",  # no conversation at all
    ],
)
async def test_a_conversation_id_that_could_address_another_thread_is_refused(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory, forged: str
) -> None:
    agent = deployed_agent_of(seeded_creator, single_step_spec())
    requested_model_ids, _ = scripted_chat_model_factory([[AIMessage(content="không nên tới đây")]])

    response = await _chat(client, agent, "hi", creator_token, conversation_id=forged)

    assert response.status_code == 422
    assert requested_model_ids == []  # rejected before the runtime started
