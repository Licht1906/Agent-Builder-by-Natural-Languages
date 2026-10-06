"""Asking for a workflow over HTTP, and reading the draft you got — see #43.

The ticket end to end: a confirmed spec goes in, a `workflow_pending_confirmation`
graph comes out, with the resources the creator attached already wired and the
wording the model wrote already in place. The model is a scripted double, so
what these tests measure is the platform's behaviour around it — which stage it
lets a request through at, whose workflow a caller may read, and what it refuses
to let a second generation do to work already in progress.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage

from src.db.models import AgentVersion, User, VersionStatus
from tests.conftest import ScriptedMetaAgent, auth_header, install_chat_model_factory

pytestmark = pytest.mark.asyncio

SPEC: dict[str, Any] = {
    "goal": "Trả lời khiếu nại của khách rồi gửi thư khi được duyệt",
    "users": ["Nhân viên chăm sóc khách hàng"],
    "data": [
        {"name": "Sổ tay xử lý khiếu nại", "kind": "uploaded_file", "ref": "blob://so-tay.pdf"},
        {"name": "Hộp thư hỗ trợ", "kind": "connected_account", "ref": "conn://gmail-1"},
    ],
    "actions": ["Tra cứu đơn hàng", "Gửi thư trả lời cho khách"],
    "expected_outcome": ["Một thư trả lời đã gửi"],
    "success_criteria": [{"text": "Thư nêu đúng mã đơn hàng", "category": "correctness"}],
    "prohibitions": ["Không tự ý hứa hoàn tiền"],
}


@pytest.fixture
def version(solution_spec_version: Callable[..., AgentVersion], seeded_creator: User) -> Callable[..., AgentVersion]:
    """A version holding `SPEC`, owned by the seeded creator unless a test says otherwise."""

    def _make(*, owner: User | None = None, status: VersionStatus = VersionStatus.SPEC_CONFIRMED) -> AgentVersion:
        return solution_spec_version(owner or seeded_creator, SPEC, status=status, name="Trợ lý khiếu nại")

    return _make


#: A graph answering `SPEC` with a person in front of the send — what the
#: composer draws when no template was named.
COMPOSED: dict[str, Any] = {
    "start_node_id": "tim",
    "nodes": [
        {"id": "tim", "type": "knowledge_search", "config": {}},
        {"id": "danhgia", "type": "agent_llm", "config": {"prompt": "Đọc khiếu nại", "model": "gpt-4.1"}},
        {
            "id": "duyet",
            "type": "human_approval",
            "config": {"approver_role": "manager", "message_template": "Duyệt thư?"},
        },
        {"id": "gui", "type": "mcp", "config": {}},
        {"id": "baocao", "type": "agent_llm", "config": {"prompt": "Báo lại kết quả", "model": "gpt-4.1-mini"}},
        {"id": "xong", "type": "end", "config": {}},
    ],
    "edges": [
        {"id": "e1", "source_node_id": "tim", "source_port": "found", "target_node_id": "danhgia"},
        {"id": "e2", "source_node_id": "danhgia", "source_port": "done", "target_node_id": "duyet"},
        {"id": "e3", "source_node_id": "duyet", "source_port": "approved", "target_node_id": "gui"},
        {"id": "e4", "source_node_id": "duyet", "source_port": "rejected", "target_node_id": "baocao"},
        {"id": "e5", "source_node_id": "gui", "source_port": "done", "target_node_id": "baocao"},
        {"id": "e6", "source_node_id": "baocao", "source_port": "done", "target_node_id": "xong"},
    ],
}


def replies(*documents: dict[str, Any]) -> None:
    """Point the model seam at a double answering with `documents`, in order."""
    install_chat_model_factory(
        ScriptedMetaAgent(*(AIMessage(content=json.dumps(document, ensure_ascii=False)) for document in documents))
    )


def composes() -> None:
    replies(COMPOSED)


def fills(**prose: dict[str, str]) -> None:
    """The one call the template path makes: the wording for a shape already chosen."""
    replies({"nodes": prose})


async def generate(client: AsyncClient, token: str, row: AgentVersion, **body: Any) -> Any:
    return await client.post(
        f"/api/v1/agents/{row.agent_id}/versions/{row.id}/workflow",
        headers=auth_header(token),
        json=body or None,
    )


async def test_a_confirmed_spec_yields_a_draft_workflow(client: AsyncClient, creator_token: str, version: Any) -> None:
    """No body at all is the common case, and it still has to work — see `WorkflowDraftRequest`."""
    composes()
    row = version()

    response = await generate(client, creator_token, row)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == VersionStatus.WORKFLOW_PENDING_CONFIRMATION.value
    assert body["workflow"]["metadata"]["template_id"] is None
    assert [node["id"] for node in body["workflow"]["nodes"]] == ["tim", "danhgia", "duyet", "gui", "baocao", "xong"]


async def test_builds_the_template_the_creator_asked_for(client: AsyncClient, creator_token: str, version: Any) -> None:
    """The gallery path — ADR-0041. The shape is the creator's choice, not a match score."""
    fills(answer={"prompt": "Trả lời dựa trên sổ tay"})
    row = version()

    response = await generate(client, creator_token, row, template_id="document_qa")

    assert response.status_code == 201, response.text
    assert response.json()["workflow"]["metadata"]["template_id"] == "document_qa"


async def test_refuses_a_template_that_is_no_longer_on_offer(
    client: AsyncClient, creator_token: str, version: Any
) -> None:
    composes()
    row = version()

    response = await generate(client, creator_token, row, template_id="mau_da_go")

    assert response.status_code == 409, response.text
    assert "mau_da_go" in response.json()["detail"]


async def test_lists_the_templates_a_creator_can_start_from(client: AsyncClient, creator_token: str) -> None:
    """The gallery itself: the same catalog the Generator builds from, published."""
    response = await client.get("/api/v1/workflow/templates", headers=auth_header(creator_token))

    assert response.status_code == 200, response.text
    listed = {entry["id"]: entry for entry in response.json()}
    assert "document_qa" in listed
    assert listed["document_qa"]["node_count"] == 3
    assert listed["document_qa"]["required_data_kinds"] == ["uploaded_file"]


async def test_refuses_to_draft_from_a_spec_nobody_confirmed(
    client: AsyncClient, creator_token: str, version: Any
) -> None:
    composes()
    row = version(status=VersionStatus.SPEC_PENDING_CONFIRMATION)

    response = await generate(client, creator_token, row)

    assert response.status_code == 409, response.text
    assert "spec_confirmed" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# ADR-0052: the composer is advised by the spec heuristic, never refused by it;
# a fallback to a template is written on the draft, not only in a log
# --------------------------------------------------------------------------- #

#: `COMPOSED` with the approval taken out — what the composer draws for a
#: read-only task whose wording trips the effect word-list ("người gửi").
UNGUARDED: dict[str, Any] = {
    "start_node_id": "tim",
    "nodes": [
        {"id": "tim", "type": "knowledge_search", "config": {}},
        {"id": "danhgia", "type": "agent_llm", "config": {"prompt": "Đọc khiếu nại", "model": "gpt-4.1"}},
        {"id": "gui", "type": "mcp", "config": {}},
        {"id": "baocao", "type": "agent_llm", "config": {"prompt": "Báo lại kết quả", "model": "gpt-4.1-mini"}},
        {"id": "xong", "type": "end", "config": {}},
    ],
    "edges": [
        {"id": "e1", "source_node_id": "tim", "source_port": "found", "target_node_id": "danhgia"},
        {"id": "e2", "source_node_id": "danhgia", "source_port": "done", "target_node_id": "gui"},
        {"id": "e5", "source_node_id": "gui", "source_port": "done", "target_node_id": "baocao"},
        {"id": "e6", "source_node_id": "baocao", "source_port": "done", "target_node_id": "xong"},
    ],
}


async def test_the_spec_heuristic_advises_the_composer_but_no_longer_refuses_its_graph(
    client: AsyncClient, creator_token: str, version: Any
) -> None:
    """`SPEC` says *"Gửi thư trả lời"* — high risk by word-list — and the composer drew no approval.

    Before ADR-0052 that was three refusals and a template. Now the draft is
    accepted as drawn; the refusal that matters is confirmation's, which reads
    the risk of the action the creator actually binds.
    """
    replies(UNGUARDED)
    row = version()

    response = await generate(client, creator_token, row)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workflow"]["metadata"]["template_id"] is None
    assert body["workflow"]["metadata"]["fallback_reason"] is None
    assert [node["id"] for node in body["workflow"]["nodes"]] == ["tim", "danhgia", "gui", "baocao", "xong"]


async def test_a_fallback_to_a_template_says_so_on_the_draft(
    client: AsyncClient, creator_token: str, version: Any
) -> None:
    """Three unusable compositions → the closest template, carrying why, as a warning the canvas shows."""
    broken = {"start_node_id": "nowhere", "nodes": [], "edges": []}
    replies(broken, broken, broken, {"template_id": "document_qa"}, {"nodes": {"answer": {"prompt": "Trả lời"}}})
    row = version()

    response = await generate(client, creator_token, row)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workflow"]["metadata"]["template_id"] == "document_qa"
    assert body["workflow"]["metadata"]["fallback_reason"]
    [notice] = [w for w in body["warnings"] if "mẫu gần nhất" in w["message"]]
    assert notice["node_id"] is None
    assert "Lý do:" in notice["message"]


async def test_a_composed_graph_that_answers_with_raw_json_is_repaired_not_redrawn(
    client: AsyncClient, creator_token: str, version: Any
) -> None:
    """`mcp → end` is a warning on the canvas and a refusal in the composer (ADR-0052 round 2)
    — and since a live battery of compositions measured it as the dominant real failure, the
    fix is no longer "ask the model again": `composition._narrated` splices in the reporting
    node itself, before the graph is even judged.

    Two baseline agents ended on the action pre-repair: the chat's answer was `{"draft_id": …}`,
    and every case about "confirm what was sent" failed on it. Only **one** scripted reply here
    — ending on `gui` — because the point is that a second model call must never happen for this:
    every attempt costs money and a graph the platform can fix itself is not worth gambling on a
    redraw.
    """
    ends_on_action = {
        **UNGUARDED,
        "nodes": [node for node in UNGUARDED["nodes"] if node["id"] != "baocao"],
        "edges": [
            edge
            for edge in UNGUARDED["edges"]
            if edge["target_node_id"] != "baocao" and edge["source_node_id"] != "baocao"
        ]
        + [{"id": "e7", "source_node_id": "gui", "source_port": "done", "target_node_id": "xong"}],
    }
    model = ScriptedMetaAgent(AIMessage(content=json.dumps(ends_on_action, ensure_ascii=False)))
    install_chat_model_factory(model)
    row = version()

    response = await generate(client, creator_token, row)

    assert response.status_code == 201, response.text
    nodes = {node["id"]: node for node in response.json()["workflow"]["nodes"]}
    assert set(nodes) == {"tim", "danhgia", "gui", "xong", "gui_ke_lai"}
    assert nodes["gui_ke_lai"]["type"] == "agent_llm"
    # One call, not two — the whole point of repairing instead of redrawing.
    assert len(model.prompts) == 1
