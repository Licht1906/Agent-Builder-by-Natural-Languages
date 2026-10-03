"""Publishing an agent, and who can reach it afterwards — see #15, widened by #50.

The subject is the Deployment: who may move its pointer, whether it is serving,
who may talk to what it serves, and which key opens its API door. Everything here
goes through HTTP, because the rules themselves already have their own unit tests
(`tests/test_deployments/`) — what is only observable from outside is that a
route *inherits* them instead of restating them, and that the two channels
ADR-0029 §5 promises are really both open.

Two claims in #15 are claims about what does **not** happen, and both are tested
by watching an agent keep answering the same way: creating a new draft does not
change what is serving, and a version being approved does not deploy itself.
"""

from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from src.db.models import Agent, AgentVersion, DeploymentAccessMode, User, VersionStatus
from src.models.agent_spec import AgentSpec
from tests.conftest import DEPLOYMENT_API_KEY, auth_header, chat_with_agent, parse_sse
from tests.conftest import agent_spec as _agent_spec

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Scenery
# --------------------------------------------------------------------------- #


def _spec_saying(step_id: str) -> AgentSpec:
    """A one-step spec whose step id is `step_id` — the thing a `step` event will name.

    Which version actually ran is not otherwise visible from outside: two
    versions of the same agent answer on the same URL. Giving each version a step
    id of its own is what turns "the pointer moved" into something a test can
    read off the stream rather than off the database it is meant to be testing.
    """
    return _agent_spec(
        steps=[
            {
                "id": step_id,
                "title": step_id,
                "prompt": "Trả lời khách",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            }
        ]
    )


@pytest.fixture
def version_of(db_session: Session):
    """A version of `agent` in whatever status the test needs, saying `step_id` when it runs.

    Statuses are written directly rather than walked to through the lifecycle:
    this suite is about the publication, and arranging a `rejected` version by
    actually getting it rejected would make every test here depend on the whole
    of #13 and #14.
    """

    def _make(agent: Agent, *, status: VersionStatus, step_id: str = "s") -> AgentVersion:
        version = AgentVersion(agent_id=agent.id, spec=_spec_saying(step_id).model_dump(mode="json"), status=status)
        db_session.add(version)
        db_session.commit()
        db_session.refresh(version)
        return version

    return _make


async def _deploy(client, agent: Agent, version: AgentVersion, token: str) -> Any:
    """Point `agent` at `version`. Deploy and rollback are the same call — see ADR-0005 §3."""
    return await client.put(
        f"/api/v1/agents/{agent.id}/deployed-version",
        json={"version_id": version.id},
        headers=auth_header(token),
    )


async def _read_deployment(client, agent: Agent, token: str) -> Any:
    return await client.get(f"/api/v1/agents/{agent.id}/deployment", headers=auth_header(token))


async def _set_status(client, agent: Agent, status: str, token: str) -> Any:
    return await client.put(
        f"/api/v1/agents/{agent.id}/deployment/status", json={"status": status}, headers=auth_header(token)
    )


async def _set_access(client, agent: Agent, mode: str, emails: list[str], token: str) -> Any:
    return await client.put(
        f"/api/v1/agents/{agent.id}/deployment/access",
        json={"access_mode": mode, "allowed_user_emails": emails},
        headers=auth_header(token),
    )


async def _regenerate_key(client, agent: Agent, token: str) -> Any:
    return await client.post(f"/api/v1/agents/{agent.id}/deployment/api-key", headers=auth_header(token))


async def _invoke_with_key(client, agent: Agent, message: str, api_key: str, **body: Any) -> Any:
    """One API-channel call: no session at all, only the Deployment's key."""
    return await client.post(
        f"/api/v1/agents/{agent.id}/invoke", json={"message": message, **body}, headers={"X-API-Key": api_key}
    )


async def _step_ids_from_chatting(client, agent: Agent, token: str, scripted_chat_model_factory) -> list[str]:
    """Which steps answered — i.e. which version is actually serving right now."""
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    response = await chat_with_agent(client, agent, "chào", token)
    assert response.status_code == 200, response.text
    return [event["step_id"] for event in parse_sse(response.text, event="step")]


# --------------------------------------------------------------------------- #
# Publishing: one row, moved rather than added to
# --------------------------------------------------------------------------- #


async def test_publishing_an_approved_version_creates_the_deployment(
    client, creator_token, approval_agent, version_of
) -> None:
    version = version_of(approval_agent, status=VersionStatus.APPROVED)

    response = await _deploy(client, approval_agent, version, creator_token)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_version_id"] == version.id
    assert body["status"] == "active"
    assert body["access_mode"] == "private"


async def test_publishing_hands_the_owner_their_api_key_exactly_once(
    client, creator_token, approval_agent, version_of
) -> None:
    """The plaintext exists in this one response and nowhere else (ADR-0029 §5)."""
    first = await _deploy(
        client, approval_agent, version_of(approval_agent, status=VersionStatus.APPROVED), creator_token
    )
    assert first.json()["api_key"].startswith("dpl_")

    second = await _deploy(
        client, approval_agent, version_of(approval_agent, status=VersionStatus.APPROVED), creator_token
    )

    assert second.json()["api_key"] is None
    assert (await _read_deployment(client, approval_agent, creator_token)).json().get("api_key") is None


async def test_publishing_a_newer_version_moves_the_same_deployment(
    client, creator_token, approval_agent, version_of
) -> None:
    first = version_of(approval_agent, status=VersionStatus.APPROVED)
    published = (await _deploy(client, approval_agent, first, creator_token)).json()
    newer = version_of(approval_agent, status=VersionStatus.APPROVED)

    response = await _deploy(client, approval_agent, newer, creator_token)

    assert response.json()["id"] == published["id"]
    assert response.json()["agent_version_id"] == newer.id


async def test_rolling_back_is_the_same_write_and_the_old_version_answers_again(
    client, creator_token, approval_agent, version_of, scripted_chat_model_factory
) -> None:
    """User story 45, and the whole of ADR-0013 §1 in one test.

    A creator whose new release misbehaves points the Deployment back at the last
    version that worked, and the next chat turn is answered by that version. It
    goes through `PUT /deployed-version` — the same request a fresh publish makes
    — because there is no rollback endpoint and there is not meant to be one: a
    second name for one write is a second place for the rules to be stated, and
    one of them to be stated wrongly.

    Read off the *stream* rather than off the row, because "the pointer says 4"
    and "version 4 is what answers people" are two claims and only the second is
    the one a creator is rolling back to get.
    """
    good = version_of(approval_agent, status=VersionStatus.APPROVED, step_id="ban-cu")
    broken = version_of(approval_agent, status=VersionStatus.APPROVED, step_id="ban-moi")
    await _deploy(client, approval_agent, good, creator_token)
    await _deploy(client, approval_agent, broken, creator_token)
    assert await _step_ids_from_chatting(client, approval_agent, creator_token, scripted_chat_model_factory) == [
        "ban-moi"
    ]

    response = await _deploy(client, approval_agent, good, creator_token)

    assert response.status_code == 200, response.text
    assert response.json()["agent_version_id"] == good.id
    assert await _step_ids_from_chatting(client, approval_agent, creator_token, scripted_chat_model_factory) == [
        "ban-cu"
    ]


async def test_deploying_a_version_nobody_approved_is_refused(
    client, creator_token, approval_agent, version_of
) -> None:
    """The refusal is the lifecycle's own, word for word — no route restates the rule (#14 comment)."""
    unapproved = version_of(approval_agent, status=VersionStatus.SANDBOX_PASSED)

    response = await _deploy(client, approval_agent, unapproved, creator_token)

    assert response.status_code == 409
    assert "đã duyệt" in response.json()["detail"]
    assert (await _read_deployment(client, approval_agent, creator_token)).status_code == 404


async def test_an_unpublished_agent_has_no_deployment_to_read(client, creator_token, approval_agent) -> None:
    """404 rather than an empty object: "not published" is an absence, not a state."""
    response = await _read_deployment(client, approval_agent, creator_token)

    assert response.status_code == 404
    assert "chưa được publish" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Turning it off, and back on
# --------------------------------------------------------------------------- #


async def test_disabling_hides_the_agent_from_everyone_else(
    client, creator_token, second_creator, second_creator_token, seeded_creator, deployed_agent_of
) -> None:
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))
    await _set_access(client, agent, "selected_users", [second_creator.email], creator_token)

    await _set_status(client, agent, "disabled", creator_token)

    assert (
        await client.get(f"/api/v1/agents/{agent.id}", headers=auth_header(second_creator_token))
    ).status_code == 404


async def test_disabling_does_not_forget_which_version_was_running(
    client, creator_token, seeded_creator, deployed_agent_of
) -> None:
    """ADR-0029 §2: coming back is one write, not "now choose a version again"."""
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))
    running = (await _read_deployment(client, agent, creator_token)).json()["agent_version_id"]

    disabled = await _set_status(client, agent, "disabled", creator_token)

    assert disabled.json()["agent_version_id"] == running


async def test_re_enabling_restores_access_immediately(
    client, creator_token, second_creator, second_creator_token, seeded_creator, deployed_agent_of
) -> None:
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))
    await _set_access(client, agent, "selected_users", [second_creator.email], creator_token)
    await _set_status(client, agent, "disabled", creator_token)

    await _set_status(client, agent, "active", creator_token)

    assert (
        await client.get(f"/api/v1/agents/{agent.id}", headers=auth_header(second_creator_token))
    ).status_code == 200


async def test_the_owner_still_reaches_their_own_paused_deployment(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """Pausing must not lock an owner out of the thing they paused."""
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))
    await _set_status(client, agent, "disabled", creator_token)

    served = await _step_ids_from_chatting(client, agent, creator_token, scripted_chat_model_factory)

    assert served == ["phuc-vu"]


# --------------------------------------------------------------------------- #
# Who may talk to it
# --------------------------------------------------------------------------- #


async def test_a_private_deployment_answers_only_its_owner(
    client, second_creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """The break from ADR-0013: published is no longer the same as public (ADR-0029 §3)."""
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))

    scripted_chat_model_factory([[AIMessage(content="xong")]])
    response = await chat_with_agent(client, agent, "chào", second_creator_token)

    assert response.status_code == 404


async def test_a_named_user_can_chat_with_a_selected_users_deployment(
    client,
    creator_token,
    second_creator,
    second_creator_token,
    seeded_creator,
    deployed_agent_of,
    scripted_chat_model_factory,
) -> None:
    """ "Người dùng cuối trò chuyện được với agent đang publish" — the ticket's whole point."""
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))
    await _set_access(client, agent, "selected_users", [second_creator.email], creator_token)

    served = await _step_ids_from_chatting(client, agent, second_creator_token, scripted_chat_model_factory)

    assert served == ["phuc-vu"]


async def test_taking_somebody_off_the_list_closes_the_door(
    client,
    creator_token,
    second_creator,
    second_creator_token,
    seeded_creator,
    deployed_agent_of,
    scripted_chat_model_factory,
) -> None:
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))
    await _set_access(client, agent, "selected_users", [second_creator.email], creator_token)

    await _set_access(client, agent, "selected_users", [], creator_token)

    scripted_chat_model_factory([[AIMessage(content="xong")]])
    assert (await chat_with_agent(client, agent, "chào", second_creator_token)).status_code == 404


async def test_an_unknown_email_is_refused_by_name_and_changes_nothing(
    client, creator_token, second_creator, seeded_creator, deployed_agent_of
) -> None:
    """An allowlist its owner believes is longer than it is would be worse than a 422."""
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))

    response = await _set_access(
        client, agent, "selected_users", [second_creator.email, "nobody@example.com"], creator_token
    )

    assert response.status_code == 422
    assert "nobody@example.com" in response.json()["detail"]
    assert (await _read_deployment(client, agent, creator_token)).json()["allowed_users"] == []


async def test_the_running_list_hides_a_deployment_the_reader_is_not_on(
    client, creator_token, second_creator, second_creator_token, seeded_creator, deployed_agent_of
) -> None:
    """ADR-0029's own consequence: `GET /agents/running` needs the same narrowing as the door."""
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))

    listed = await client.get("/api/v1/agents/running", headers=auth_header(second_creator_token))

    assert agent.id not in [row["id"] for row in listed.json()]

    await _set_access(client, agent, "selected_users", [second_creator.email], creator_token)
    listed_again = await client.get("/api/v1/agents/running", headers=auth_header(second_creator_token))

    assert agent.id in [row["id"] for row in listed_again.json()]


async def test_the_running_list_says_whose_agent_it_is_and_which_door_answers(
    client, creator_token, seeded_creator, deployed_agent_of
) -> None:
    """Two facts the chat screen needs and used to guess.

    `owner_id`: whether the reader may press the approval buttons was decided
    by comparing against a field this list did not carry, so the creator
    opening their own agent from the dashboard saw "người tạo sẽ quyết định"
    about themselves. `runs_workflow`: a V1 pipeline answers at `/chat`, a
    workflow at `/deployed-workflow-chat`, and the screen learned which from
    a 409.
    """
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))

    listed = await client.get("/api/v1/agents/running", headers=auth_header(creator_token))

    row = next(entry for entry in listed.json() if entry["id"] == agent.id)
    assert row["owner_id"] == seeded_creator.id
    assert row["runs_workflow"] is False  # `_spec_saying` is a V1 pipeline


async def test_the_running_list_drops_a_paused_deployment(
    client, creator_token, seeded_creator, deployed_agent_of
) -> None:
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))
    assert agent.id in [
        row["id"] for row in (await client.get("/api/v1/agents/running", headers=auth_header(creator_token))).json()
    ]

    await _set_status(client, agent, "disabled", creator_token)

    listed = await client.get("/api/v1/agents/running", headers=auth_header(creator_token))
    assert agent.id not in [row["id"] for row in listed.json()]


def _workflow_with_disclaimer(disclaimer: str) -> dict[str, Any]:
    """A minimal valid graph (`AgentLlm -> End`), carrying `metadata.disclaimer` — RAG-14 / #100."""
    return {
        "start_node_id": "answer",
        "nodes": [
            {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời.", "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [{"id": "e1", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"}],
        "metadata": {"disclaimer": disclaimer},
    }


async def test_the_running_list_carries_the_deployed_versions_disclaimer(
    client,
    creator_token,
    seeded_creator,
    solution_spec_version: Callable[..., AgentVersion],
) -> None:
    """RAG-14 / #100: a stranger's chat window has no `OwnedAgent` door to read the workflow through."""
    version = solution_spec_version(
        seeded_creator,
        {
            "goal": "Trả lời câu hỏi pháp luật",
            "users": ["Khách"],
            "data": [],
            "actions": ["Trả lời"],
            "expected_outcome": ["Một câu trả lời"],
            "success_criteria": [{"text": "Đúng", "category": "correctness"}],
            "prohibitions": ["Không tự bịa"],
        },
        status=VersionStatus.APPROVED,
        workflow=_workflow_with_disclaimer(
            "Thông tin mang tính tham khảo, không thay thế tư vấn pháp lý chuyên nghiệp"
        ),
    )
    published = await client.put(
        f"/api/v1/agents/{version.agent_id}/deployed-version",
        json={"version_id": version.id},
        headers=auth_header(creator_token),
    )
    assert published.status_code == 200, published.text

    listed = await client.get("/api/v1/agents/running", headers=auth_header(creator_token))

    row = next(row for row in listed.json() if row["id"] == version.agent_id)
    assert row["disclaimer"] == "Thông tin mang tính tham khảo, không thay thế tư vấn pháp lý chuyên nghiệp"


async def test_the_running_list_has_no_disclaimer_when_the_agent_set_none(
    client, creator_token, seeded_creator, deployed_agent_of
) -> None:
    """A V1 agent's version carries no `workflow` at all — reads as `""`, not a 500."""
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))

    listed = await client.get("/api/v1/agents/running", headers=auth_header(creator_token))

    row = next(row for row in listed.json() if row["id"] == agent.id)
    assert row["disclaimer"] == ""


async def test_only_the_owner_may_change_who_can_reach_it(
    client, second_creator_token, second_creator, seeded_creator, deployed_agent_of
) -> None:
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))

    response = await _set_access(client, agent, "selected_users", [second_creator.email], second_creator_token)

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# The API channel
# --------------------------------------------------------------------------- #


async def test_the_api_key_reaches_the_same_deployment_the_chat_window_does(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """ADR-0029 §5: both channels, always, against one active Deployment."""
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))
    scripted_chat_model_factory([[AIMessage(content="xong")]])

    response = await _invoke_with_key(client, agent, "chào", DEPLOYMENT_API_KEY)

    assert response.status_code == 200, response.text
    assert [step["step_id"] for step in response.json()["steps"]] == ["phuc-vu"]


async def test_a_wrong_api_key_is_the_same_404_as_a_missing_agent(
    client, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """A distinct "bad key" would tell a prober this agent is worth guessing at."""
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))
    scripted_chat_model_factory([[AIMessage(content="xong")]])

    response = await _invoke_with_key(client, agent, "chào", "dpl_not-the-key")

    assert response.status_code == 404


async def test_regenerating_the_key_invalidates_the_old_one_at_once(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))

    regenerated = await _regenerate_key(client, agent, creator_token)

    assert regenerated.status_code == 200, regenerated.text
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    assert (await _invoke_with_key(client, agent, "chào", DEPLOYMENT_API_KEY)).status_code == 404
    scripted_chat_model_factory([[AIMessage(content="xong")]])
    assert (await _invoke_with_key(client, agent, "chào", regenerated.json()["api_key"])).status_code == 200


async def test_the_api_key_stops_working_while_the_deployment_is_paused(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory
) -> None:
    """An integration that kept working through a pause would make the pause a lie."""
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))
    await _set_status(client, agent, "disabled", creator_token)

    scripted_chat_model_factory([[AIMessage(content="xong")]])
    assert (await _invoke_with_key(client, agent, "chào", DEPLOYMENT_API_KEY)).status_code == 404


async def test_invoking_with_no_credential_at_all_is_a_401(client, seeded_creator, deployed_agent_of) -> None:
    """Not a 404: nothing was claimed, so nothing is being hidden — the caller has to say who they are."""
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"))

    response = await client.post(f"/api/v1/agents/{agent.id}/invoke", json={"message": "chào"})

    assert response.status_code == 401
    assert "X-API-Key" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Many end-users, one Deployment
# --------------------------------------------------------------------------- #


async def test_two_end_users_sharing_one_deployment_do_not_share_a_conversation(
    client,
    creator_token,
    second_creator: User,
    second_creator_token,
    seeded_creator,
    deployed_agent_of,
    scripted_chat_model_factory,
) -> None:
    """Session isolation is by caller, not by the id being hard to guess (#50).

    The second caller sends the *same* `conversation_id` the first one used. If
    the thread were addressed by id alone they would be reading each other's
    history; addressed per caller, they are simply two empty conversations that
    happen to share a name.
    """
    agent = deployed_agent_of(seeded_creator, _spec_saying("phuc-vu"), access_mode=DeploymentAccessMode.SELECTED_USERS)
    await _set_access(client, agent, "selected_users", [second_creator.email], creator_token)
    shared_id = "cuoc-tro-chuyen-chung"

    scripted_chat_model_factory([[AIMessage(content="xong")]])
    first = await chat_with_agent(client, agent, "bí mật của tôi là 42", creator_token, conversation_id=shared_id)
    assert first.status_code == 200, first.text

    _, models = scripted_chat_model_factory([[AIMessage(content="xong")]])
    second = await chat_with_agent(client, agent, "tôi vừa nói gì?", second_creator_token, conversation_id=shared_id)

    assert second.status_code == 200, second.text
    seen = "\n".join(str(message.content) for batch in models[0].received_message_batches for message in batch)
    assert "bí mật của tôi là 42" not in seen
