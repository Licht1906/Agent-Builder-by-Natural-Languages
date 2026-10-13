"""The chat sidebar: which conversations are mine, and who cannot see them.

ADR-0004 stored what was said and nothing about which threads exist, because #6
only had to answer "what happened here". These are the tests for the index that
answers the other question — the one a person asks when they reload the page and
want their conversation back.

Two things are being pinned, and the second is the one worth breaking a build
over:

1. A conversation is listed because a turn actually happened on it, is labelled
   by what was said first, and comes back with its transcript when reopened.
2. **One person's list never contains another's conversation**, even when the
   two are talking to the same deployed agent and one of them knows the other's
   conversation id. That is ADR-0036 §6's per-caller thread key seen from the
   outside, and it is the assertion that would matter most if it ever failed.

Observed over HTTP like the rest of the suite: the sidebar is an HTTP client,
and a test that called the service module directly would pass while the route
that the browser actually reaches was wired to the wrong user.
"""

from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from src.db.models import DeploymentAccessMode
from tests.conftest import SECOND_CREATOR_EMAIL, auth_header, chat_with_agent, single_step_spec

pytestmark = pytest.mark.asyncio


def _conversation_id() -> str:
    return uuid4().hex


async def _shared_with_second_creator(client: Any, agent: Any, owner_token: str) -> None:
    """Put `second_creator` on this agent's allowlist.

    The cross-user tests need an agent that two people can both *reach*, or the
    isolation they are checking would be indistinguishable from the 404 a
    private deployment gives everyone but its owner — and that 404 is a
    different rule (ADR-0029 §3), already tested elsewhere. Sharing the agent
    first is what makes "they can talk to it but cannot see my conversations"
    the only thing left for these tests to prove.
    """
    granted = await client.put(
        f"/api/v1/agents/{agent.id}/deployment/access",
        json={"access_mode": DeploymentAccessMode.SELECTED_USERS.value, "allowed_user_emails": [SECOND_CREATOR_EMAIL]},
        headers=auth_header(owner_token),
    )
    assert granted.status_code == 200, granted.text


async def _list(client: Any, agent: Any, token: str) -> Any:
    return await client.get(f"/api/v1/agents/{agent.id}/conversations", headers=auth_header(token))


async def _messages(client: Any, agent: Any, conversation_id: str, token: str) -> Any:
    return await client.get(
        f"/api/v1/agents/{agent.id}/conversations/{conversation_id}/messages", headers=auth_header(token)
    )


# --------------------------------------------------------------------------- #
# A turn puts a conversation in the list
# --------------------------------------------------------------------------- #


async def test_an_agent_nobody_has_talked_to_lists_no_conversations(
    client, creator_token, seeded_creator, deployed_agent_of
) -> None:
    """The first visit is an empty list, not a 404 — see `list_my_conversations`."""
    agent = deployed_agent_of(seeded_creator, single_step_spec())

    listed = await _list(client, agent, creator_token)

    assert listed.status_code == 200, listed.text
    assert listed.json() == []


async def test_one_turn_creates_one_conversation_titled_by_what_was_said(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    agent = deployed_agent_of(seeded_creator, single_step_spec())
    scripted_chat_model_factory([[AIMessage(content="Hà Nội")]])

    sent = await chat_with_agent(client, agent, "thủ đô Việt Nam là gì?", creator_token)
    assert sent.status_code == 200, sent.text

    rows = (await _list(client, agent, creator_token)).json()
    assert len(rows) == 1
    assert rows[0]["title"] == "thủ đô Việt Nam là gì?"


async def test_timestamps_leave_the_api_saying_which_timezone_they_are_in(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """The bug this pins: a naive `2026-08-16T01:48:33` is read by
    `new Date(...)` in a browser as *local* time, so a reader in UTC+7 saw a
    conversation they had just opened dated seven hours ago. The instant stored
    is correct; what was missing was the label."""
    agent = deployed_agent_of(seeded_creator, single_step_spec())
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    await chat_with_agent(client, agent, "câu hỏi", creator_token)

    row = (await _list(client, agent, creator_token)).json()[0]

    for field in ("created_at", "last_message_at"):
        # `+00:00` or `Z` — either is a zone; a bare timestamp is the bug.
        assert row[field].endswith(("+00:00", "Z")), f"{field} went out unlabelled: {row[field]!r}"


async def test_a_second_turn_moves_the_same_conversation_rather_than_adding_one(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """The unique constraint doing its job: a long chat is one row, not one row per message."""
    agent = deployed_agent_of(seeded_creator, single_step_spec())
    conversation = _conversation_id()

    scripted_chat_model_factory([[AIMessage(content="một")]])
    await chat_with_agent(client, agent, "câu hỏi đầu", creator_token, conversation_id=conversation)
    scripted_chat_model_factory([[AIMessage(content="hai")]])
    await chat_with_agent(client, agent, "câu hỏi sau", creator_token, conversation_id=conversation)

    rows = (await _list(client, agent, creator_token)).json()
    assert len(rows) == 1
    # The title is the *first* message and stays it, so a rename is not undone
    # by whatever gets asked next.
    assert rows[0]["title"] == "câu hỏi đầu"


async def test_two_conversations_are_listed_most_recently_used_first(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    agent = deployed_agent_of(seeded_creator, single_step_spec())

    scripted_chat_model_factory([[AIMessage(content="một")]])
    await chat_with_agent(client, agent, "cuộc cũ", creator_token, conversation_id=_conversation_id())
    scripted_chat_model_factory([[AIMessage(content="hai")]])
    await chat_with_agent(client, agent, "cuộc mới", creator_token, conversation_id=_conversation_id())

    titles = [row["title"] for row in (await _list(client, agent, creator_token)).json()]
    assert titles == ["cuộc mới", "cuộc cũ"]


# --------------------------------------------------------------------------- #
# Reopening one
# --------------------------------------------------------------------------- #


async def test_reopening_a_conversation_returns_what_was_said_in_it(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """The transcript comes from the checkpoint, which is the only thing that has it."""
    agent = deployed_agent_of(seeded_creator, single_step_spec())
    conversation = _conversation_id()
    scripted_chat_model_factory([[AIMessage(content="Hà Nội")]])
    await chat_with_agent(client, agent, "thủ đô là gì?", creator_token, conversation_id=conversation)

    read = await _messages(client, agent, conversation, creator_token)

    assert read.status_code == 200, read.text
    turns = read.json()["turns"]
    assert [turn["role"] for turn in turns] == ["user", "agent"]
    assert turns[0]["content"] == "thủ đô là gì?"
    assert turns[1]["content"] == "Hà Nội"


async def test_a_conversation_nobody_has_used_reads_back_empty_rather_than_404(
    client, creator_token, seeded_creator, deployed_agent_of
) -> None:
    """A 404 here would let a caller probe which conversation ids exist."""
    agent = deployed_agent_of(seeded_creator, single_step_spec())

    read = await _messages(client, agent, _conversation_id(), creator_token)

    assert read.status_code == 200, read.text
    assert read.json()["turns"] == []


# --------------------------------------------------------------------------- #
# Renaming and removing
# --------------------------------------------------------------------------- #


async def test_renaming_a_conversation_changes_its_label_and_nothing_else(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    agent = deployed_agent_of(seeded_creator, single_step_spec())
    conversation = _conversation_id()
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    await chat_with_agent(client, agent, "một câu hỏi dài", creator_token, conversation_id=conversation)

    renamed = await client.patch(
        f"/api/v1/agents/{agent.id}/conversations/{conversation}",
        json={"title": "Nghiên cứu thị trường"},
        headers=auth_header(creator_token),
    )

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "Nghiên cứu thị trường"
    # Still the same conversation, and still holding what was said in it.
    assert (await _messages(client, agent, conversation, creator_token)).json()["turns"][0]["content"] == (
        "một câu hỏi dài"
    )


async def test_a_blank_title_is_refused(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """Clearing the box is a 422, not a conversation with no name."""
    agent = deployed_agent_of(seeded_creator, single_step_spec())
    conversation = _conversation_id()
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    await chat_with_agent(client, agent, "câu hỏi", creator_token, conversation_id=conversation)

    refused = await client.patch(
        f"/api/v1/agents/{agent.id}/conversations/{conversation}",
        json={"title": ""},
        headers=auth_header(creator_token),
    )

    assert refused.status_code == 422, refused.text


async def test_removing_a_conversation_takes_it_off_the_list_but_keeps_the_transcript(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """The documented limit of `remove_my_conversation`, pinned so the UI cannot
    start promising an erase this code does not perform."""
    agent = deployed_agent_of(seeded_creator, single_step_spec())
    conversation = _conversation_id()
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    await chat_with_agent(client, agent, "câu hỏi", creator_token, conversation_id=conversation)

    removed = await client.delete(
        f"/api/v1/agents/{agent.id}/conversations/{conversation}", headers=auth_header(creator_token)
    )

    assert removed.status_code == 204, removed.text
    assert (await _list(client, agent, creator_token)).json() == []
    # The checkpoint is untouched — LangGraph owns those tables (ADR-0004 §2).
    assert (await _messages(client, agent, conversation, creator_token)).json()["turns"] != []


async def test_renaming_a_conversation_that_is_not_mine_is_a_404(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory, second_creator_token
) -> None:
    """Same answer as "no such conversation", so a rename cannot be used to
    discover that somebody else's exists."""
    agent = deployed_agent_of(seeded_creator, single_step_spec(), access_mode=DeploymentAccessMode.SELECTED_USERS)
    await _shared_with_second_creator(client, agent, creator_token)
    conversation = _conversation_id()
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    await chat_with_agent(client, agent, "riêng tư", creator_token, conversation_id=conversation)

    refused = await client.patch(
        f"/api/v1/agents/{agent.id}/conversations/{conversation}",
        json={"title": "của tôi bây giờ"},
        headers=auth_header(second_creator_token),
    )

    assert refused.status_code == 404, refused.text


# --------------------------------------------------------------------------- #
# The isolation that matters
# --------------------------------------------------------------------------- #


async def test_one_persons_list_never_contains_anothers_conversation(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory, second_creator_token
) -> None:
    agent = deployed_agent_of(seeded_creator, single_step_spec(), access_mode=DeploymentAccessMode.SELECTED_USERS)
    await _shared_with_second_creator(client, agent, creator_token)
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    await chat_with_agent(client, agent, "bí mật của tôi", creator_token)

    theirs = await _list(client, agent, second_creator_token)

    assert theirs.status_code == 200, theirs.text
    assert theirs.json() == []


async def test_knowing_someone_elses_conversation_id_does_not_open_it(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory, second_creator_token
) -> None:
    """ADR-0036 §6 from the outside: the caller is *part of the key*, so an id
    that leaked is an id that addresses the reader's own empty thread."""
    agent = deployed_agent_of(seeded_creator, single_step_spec(), access_mode=DeploymentAccessMode.SELECTED_USERS)
    await _shared_with_second_creator(client, agent, creator_token)
    conversation = _conversation_id()
    scripted_chat_model_factory([[AIMessage(content="câu trả lời riêng")]])
    await chat_with_agent(client, agent, "bí mật của tôi", creator_token, conversation_id=conversation)

    read = await _messages(client, agent, conversation, second_creator_token)

    assert read.status_code == 200, read.text
    assert read.json()["turns"] == []
