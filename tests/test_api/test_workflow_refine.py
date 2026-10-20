"""Editing a workflow by chat, read through HTTP — the canvas's fourth call.

The claims here are the ones only the whole path can make, and the first of them
is an absence: **this endpoint stores nothing**. The graph comes back proposed,
the canvas applies it, and `PUT .../workflow` is still the only writer of the
column. A test that merely checked the response shape would pass just as happily
against an endpoint that had quietly saved, so the row is re-read and compared
after every call that matters.

The second is the stage gate. Chat is allowed exactly where the canvas is
allowed, because a sentence that could edit a confirmed graph would fork a
version on a screen offering no way to have asked for it.

Every model is a scripted double. What the edited graph says is the double's
business; whether it is stored, and what the creator is told when it cannot be
written at all, is the platform's.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from src.db.models import AgentVersion, User, VersionStatus
from src.services.model_document import MAX_ATTEMPTS
from tests.conftest import auth_header

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

SPEC: dict[str, Any] = {
    "goal": "Trả lời khiếu nại của khách",
    "users": ["Nhân viên chăm sóc khách hàng"],
    "data": [{"name": "Sổ tay", "kind": "uploaded_file", "ref": "blob://so-tay.pdf"}],
    "actions": ["Gửi thư trả lời"],
    "expected_outcome": ["Một thư trả lời"],
    "success_criteria": [{"text": "Nêu đúng mã đơn", "category": "correctness"}],
    "prohibitions": ["Không hứa hoàn tiền"],
}

CURRENT: dict[str, Any] = {
    "start_node_id": "soan",
    "nodes": [
        {
            "id": "soan",
            "type": "agent_llm",
            "config": {"prompt": "Soạn thư", "model": "gpt-4o-mini"},
            "position": {"x": 100, "y": 100},
        },
        {
            "id": "gui",
            "type": "mcp",
            "config": {"tool_id": "reg_7", "action": "send_mail"},
            "position": {"x": 400, "y": 100},
        },
        {"id": "xong", "type": "end", "config": {}, "position": {"x": 700, "y": 100}},
    ],
    "edges": [
        {"id": "e1", "source_node_id": "soan", "source_port": "done", "target_node_id": "gui"},
        {"id": "e2", "source_node_id": "gui", "source_port": "done", "target_node_id": "xong"},
    ],
    "metadata": {},
}

#: The same graph with an approval step spliced in before the mail goes out.
EDITED: dict[str, Any] = {
    "start_node_id": "soan",
    "nodes": [
        *CURRENT["nodes"][:1],
        {
            "id": "duyet",
            "type": "human_approval",
            "config": {"approver_role": "admin", "message_template": "Duyệt gửi thư?"},
        },
        *CURRENT["nodes"][1:],
    ],
    "edges": [
        {"id": "e1", "source_node_id": "soan", "source_port": "done", "target_node_id": "duyet"},
        {"id": "e3", "source_node_id": "duyet", "source_port": "approved", "target_node_id": "gui"},
        {"id": "e4", "source_node_id": "duyet", "source_port": "rejected", "target_node_id": "xong"},
        {"id": "e2", "source_node_id": "gui", "source_port": "done", "target_node_id": "xong"},
    ],
    "metadata": {},
}

ADD_APPROVAL = "thêm bước duyệt tay trước khi gửi mail"


@pytest.fixture
def drafting(solution_spec_version: Callable[..., AgentVersion], seeded_creator: User) -> Callable[..., AgentVersion]:
    """A version holding `CURRENT`, at whatever stage the test needs."""

    def _make(*, status: VersionStatus = VersionStatus.WORKFLOW_PENDING_CONFIRMATION) -> AgentVersion:
        return solution_spec_version(seeded_creator, SPEC, status=status, workflow=CURRENT)

    return _make


def replies(*graphs: dict[str, Any]) -> list[list[AIMessage]]:
    """One scripted model answering with `graphs` in order — the shape the fixture takes."""
    return [[AIMessage(content=json.dumps(graph, ensure_ascii=False)) for graph in graphs]]


def gibberish(times: int) -> list[list[AIMessage]]:
    """A model that never manages valid JSON."""
    return [[AIMessage(content="tôi chịu thôi") for _ in range(times)]]


async def refine(client: AsyncClient, token: str, row: AgentVersion, instruction: str) -> Any:
    return await client.post(
        f"/api/v1/agents/{row.agent_id}/versions/{row.id}/workflow/refine",
        json={"instruction": instruction},
        headers=auth_header(token),
    )


# --------------------------------------------------------------------------- #
# A turn that worked
# --------------------------------------------------------------------------- #


async def test_a_sentence_comes_back_as_a_graph_and_nothing_is_written(
    client: AsyncClient,
    creator_token: str,
    drafting: Callable[..., AgentVersion],
    scripted_chat_model_factory: Any,
    db_session: Session,
) -> None:
    """The whole contract in one request: a proposal out, the stored row untouched."""
    version = drafting()
    scripted_chat_model_factory(replies(EDITED))

    response = await refine(client, creator_token, version, ADD_APPROVAL)

    assert response.status_code == 200
    body = response.json()
    assert [node["id"] for node in body["workflow"]["nodes"]] == ["soan", "duyet", "gui", "xong"]

    db_session.expire_all()
    stored = db_session.get(AgentVersion, version.id)
    assert stored is not None
    assert stored.workflow == CURRENT
    assert stored.status is VersionStatus.WORKFLOW_PENDING_CONFIRMATION


async def test_the_response_carries_the_graph_alone_so_nothing_reads_as_saved(
    client: AsyncClient,
    creator_token: str,
    drafting: Callable[..., AgentVersion],
    scripted_chat_model_factory: Any,
) -> None:
    """No `version_id`, no `status`, no `problems`.

    Every one of those is a fact about a stored document, and carrying them here
    would answer about the old graph while showing the new one — the canvas asks
    for them with its ordinary save, which is the one call that knows.
    """
    version = drafting()
    scripted_chat_model_factory(replies(EDITED))

    body = (await refine(client, creator_token, version, ADD_APPROVAL)).json()

    assert set(body) == {"workflow"}


async def test_a_connection_the_model_rewrote_comes_back_as_the_creator_left_it(
    client: AsyncClient,
    creator_token: str,
    drafting: Callable[..., AgentVersion],
    scripted_chat_model_factory: Any,
) -> None:
    """ADR-0025 §5 surviving the whole round trip, not only the unit under it."""
    version = drafting()
    hijacked = json.loads(json.dumps(EDITED))
    for node in hijacked["nodes"]:
        if node["id"] == "gui":
            node["config"] = {"tool_id": "reg_999", "action": "delete_everything"}
    scripted_chat_model_factory(replies(hijacked))

    body = (await refine(client, creator_token, version, ADD_APPROVAL)).json()

    sent = next(node for node in body["workflow"]["nodes"] if node["id"] == "gui")
    # Every declared field comes back, not only the ones the model mentioned —
    # `_config_of` builds the config key by key so an invented value has nowhere
    # to land. `require_approval`, `action_mode` and `allowed_actions` are
    # therefore materialised at their defaults, which is what an `MCP` node that
    # never set them already meant.
    assert sent["config"] == {
        "tool_id": "reg_7",
        "action": "send_mail",
        "action_mode": "single",
        "allowed_actions": [],
        "require_approval": "auto",
    }


async def test_a_narrowed_mcp_node_does_not_widen_because_the_model_left_it_out(
    client: AsyncClient,
    creator_token: str,
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
    scripted_chat_model_factory: Any,
) -> None:
    """ADR-0025 §5 for the permission set, not only the resource reference.

    `allowed_actions` is what a `full`-mode node **may reach**, and empty means
    *every* action the connection offers. So a model that edits the graph and
    simply omits the field would hand it back blank — turning a node the creator
    had narrowed to one read-only action into one that may call anything the
    connection exposes. Omission is the whole attack; no invented value needed.
    """
    stored = json.loads(json.dumps(CURRENT))
    for node in stored["nodes"]:
        if node["id"] == "gui":
            node["config"] = {
                "tool_id": "reg_7",
                "action": "",
                "action_mode": "full",
                "allowed_actions": ["read_mail"],
                "require_approval": "auto",
            }
    version = solution_spec_version(
        seeded_creator, SPEC, status=VersionStatus.WORKFLOW_PENDING_CONFIRMATION, workflow=stored
    )
    # The model answers with the same node, saying nothing about what it may call.
    hijacked = json.loads(json.dumps(EDITED))
    for node in hijacked["nodes"]:
        if node["id"] == "gui":
            node["config"] = {"tool_id": "reg_7", "action": "send_mail"}
    scripted_chat_model_factory(replies(hijacked))

    body = (await refine(client, creator_token, version, ADD_APPROVAL)).json()

    sent = next(node for node in body["workflow"]["nodes"] if node["id"] == "gui")
    assert sent["config"]["allowed_actions"] == ["read_mail"]
    assert sent["config"]["action_mode"] == "full"


# --------------------------------------------------------------------------- #
# The refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status",
    [
        VersionStatus.WORKFLOW_CONFIRMED,
        VersionStatus.SANDBOX_PASSED,
        VersionStatus.PENDING_APPROVAL,
        VersionStatus.APPROVED,
        VersionStatus.REJECTED,
    ],
)
async def test_a_frozen_workflow_is_refined_without_forking_anything(
    client: AsyncClient,
    creator_token: str,
    drafting: Callable[..., AgentVersion],
    scripted_chat_model_factory: Any,
    db_session: Session,
    status: VersionStatus,
) -> None:
    """200 at every stage the canvas edits at, and still not one row written.

    These are the stages a save *forks* (`WORKFLOW_FORKABLE`), and that is
    exactly why refining at them is safe: this endpoint proposes and the save
    disposes. The fork arrives when the creator saves the proposal, on a screen
    that asked for it — which is the guarantee the old narrower gate was reaching
    for when the canvas had no way to ask.

    The version count is the real assertion. A refine that quietly forked would
    be the one edit path with no undo, and it would do it here rather than at the
    save the canvas is watching.
    """
    version = drafting(status=status)
    scripted_chat_model_factory(replies(EDITED))

    response = await refine(client, creator_token, version, ADD_APPROVAL)

    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.query(AgentVersion).filter_by(agent_id=version.agent_id).count() == 1


async def test_a_stage_with_no_graph_to_edit_still_refuses(
    client: AsyncClient,
    creator_token: str,
    drafting: Callable[..., AgentVersion],
    scripted_chat_model_factory: Any,
    db_session: Session,
) -> None:
    """409 where the canvas cannot edit either — the gate moved, it did not go.

    `spec_confirmed` is a version whose workflow has not been generated yet. The
    canvas is not open on it, so a sentence must not be able to change it: the
    rule is still that chat may do what the same screen's mouse may do, and no
    more.
    """
    version = drafting(status=VersionStatus.SPEC_CONFIRMED)
    scripted_chat_model_factory(replies(EDITED))

    response = await refine(client, creator_token, version, ADD_APPROVAL)

    assert response.status_code == 409
    db_session.expire_all()
    assert db_session.query(AgentVersion).filter_by(agent_id=version.agent_id).count() == 1


async def test_an_empty_instruction_is_a_422_in_the_creators_own_words(
    client: AsyncClient,
    creator_token: str,
    drafting: Callable[..., AgentVersion],
    scripted_chat_model_factory: Any,
) -> None:
    """Not pydantic's error list: an empty box and two spaces are the same mistake."""
    version = drafting()
    scripted_chat_model_factory(replies(EDITED))

    response = await refine(client, creator_token, version, "   ")

    assert response.status_code == 422
    assert "Chưa rõ bạn muốn sửa gì" in response.json()["detail"]


async def test_a_meta_agent_that_cannot_write_a_valid_graph_says_the_canvas_is_untouched(
    client: AsyncClient,
    creator_token: str,
    drafting: Callable[..., AgentVersion],
    scripted_chat_model_factory: Any,
    db_session: Session,
) -> None:
    """502, and the sentence has to be true — so the row is checked as well as read."""
    version = drafting()
    scripted_chat_model_factory(gibberish(MAX_ATTEMPTS + 1))

    response = await refine(client, creator_token, version, ADD_APPROVAL)

    assert response.status_code == 502
    assert "vẫn giữ nguyên" in response.json()["detail"]
    db_session.expire_all()
    stored = db_session.get(AgentVersion, version.id)
    assert stored is not None
    assert stored.workflow == CURRENT


async def test_somebody_elses_version_is_a_404_not_a_refusal(
    client: AsyncClient,
    creator_token: str,
    solution_spec_version: Callable[..., AgentVersion],
    seeded_admin: User,
    scripted_chat_model_factory: Any,
) -> None:
    """The house rule for every version-scoped route: existence is not leaked."""
    theirs = solution_spec_version(
        seeded_admin, SPEC, status=VersionStatus.WORKFLOW_PENDING_CONFIRMATION, workflow=CURRENT
    )
    scripted_chat_model_factory(replies(EDITED))

    response = await refine(client, creator_token, theirs, ADD_APPROVAL)

    assert response.status_code == 404
