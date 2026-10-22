"""Knowledge Base CRUD, and which agents may search which shelves — #90 / RAG-09 (#95).

The acceptance criteria this file exists to prove:

- create two Knowledge Bases, assign one of them to two different agents, and
  the many-to-many comes back right for both;
- deleting a Knowledge Base cascades its Documents away in Postgres, and
  removes its Qdrant points too (RAG-09) — the delete route wiring only; the
  ordering/refusal logic itself is `tests/test_workflow/test_knowledge_bases.py`'s.
"""

import io
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Agent, AgentVersion, Document, KnowledgeBase, User, VersionStatus
from src.main import app
from src.services.qdrant import get_qdrant_client
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


def _agent(db_session: Session, owner_id: int, name: str) -> Agent:
    agent = Agent(owner_id=owner_id, name=name)
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


def _confirmed_workflow_referencing(kb_id: str) -> dict[str, Any]:
    return {
        "start_node_id": "search",
        "nodes": [
            {"id": "search", "type": "knowledge_search", "config": {"source_ids": [kb_id]}},
            {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời.", "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "search", "source_port": "found", "target_node_id": "answer"},
            {"id": "e2", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {},
    }


async def test_creating_a_knowledge_base_hands_back_its_row(client: AsyncClient, creator_token: str) -> None:
    response = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Luật lao động", "description": "Nghị định và thông tư"},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Luật lao động"
    assert body["mode"] == "managed"


async def test_a_fresh_knowledge_base_appears_in_the_owners_list(client: AsyncClient, creator_token: str) -> None:
    created = await client.post(
        "/api/v1/knowledge-bases", json={"name": "Tủ vừa tạo, danh sách"}, headers=auth_header(creator_token)
    )

    listed = await client.get("/api/v1/knowledge-bases", headers=auth_header(creator_token))

    assert listed.status_code == 200
    assert created.json()["id"] in {kb["id"] for kb in listed.json()}


async def test_one_creator_never_sees_anothers_knowledge_base(
    client: AsyncClient, creator_token: str, second_creator_token: str
) -> None:
    created = await client.post("/api/v1/knowledge-bases", json={"name": "Riêng tư"}, headers=auth_header(creator_token))
    kb_id = created.json()["id"]

    response = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=auth_header(second_creator_token))

    assert response.status_code == 404


async def test_deleting_a_knowledge_base_cascades_its_documents_and_its_qdrant_points(
    client: AsyncClient, creator_token: str, db_session: Session, _no_live_ingestion_backends
) -> None:
    created = await client.post("/api/v1/knowledge-bases", json={"name": "Sẽ xoá"}, headers=auth_header(creator_token))
    kb_id = created.json()["id"]
    handbook = b"Nghi phep\n\nNhan vien duoc nghi 12 ngay phep mot nam.\n"
    await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", io.BytesIO(handbook), "text/markdown")},
        headers=auth_header(creator_token),
    )
    points_before, _ = _no_live_ingestion_backends.scroll("documents_kb", limit=100)
    assert any(point.payload["kb_id"] == str(kb_id) for point in points_before), "arrange step: ingest must have run"

    deleted = await client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=auth_header(creator_token))

    assert deleted.status_code == 204
    assert db_session.get(KnowledgeBase, kb_id) is None
    remaining = db_session.execute(select(Document).where(Document.kb_id == kb_id)).scalars().all()
    assert remaining == []
    points_after, _ = _no_live_ingestion_backends.scroll("documents_kb", limit=100)
    assert not any(point.payload["kb_id"] == str(kb_id) for point in points_after)


async def test_deleting_someone_elses_knowledge_base_is_a_404(
    client: AsyncClient, creator_token: str, second_creator_token: str
) -> None:
    created = await client.post("/api/v1/knowledge-bases", json={"name": "Không phải của bạn"}, headers=auth_header(creator_token))
    kb_id = created.json()["id"]

    response = await client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=auth_header(second_creator_token))

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# RAG-09 (#95): a linked agent warns, a confirmed graph blocks outright
# --------------------------------------------------------------------------- #


async def test_deleting_a_kb_linked_to_an_agent_is_refused_without_confirmation(
    client: AsyncClient, creator_token: str, seeded_creator, db_session: Session
) -> None:
    kb = await client.post("/api/v1/knowledge-bases", json={"name": "Đang dùng"}, headers=auth_header(creator_token))
    kb_id = kb.json()["id"]
    agent = _agent(db_session, seeded_creator.id, "Agent đang dùng tủ")
    await client.put(
        f"/api/v1/agents/{agent.id}/knowledge-bases",
        json={"knowledge_base_ids": [kb_id]},
        headers=auth_header(creator_token),
    )

    response = await client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=auth_header(creator_token))

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "Agent đang dùng tủ" in detail["message"]
    assert detail["affected_agents"] == ["Agent đang dùng tủ"]
    assert detail["blocking"] is False
    assert db_session.get(KnowledgeBase, kb_id) is not None


async def test_confirm_true_deletes_a_kb_linked_to_an_agent(
    client: AsyncClient, creator_token: str, seeded_creator, db_session: Session
) -> None:
    kb = await client.post("/api/v1/knowledge-bases", json={"name": "Đang dùng"}, headers=auth_header(creator_token))
    kb_id = kb.json()["id"]
    agent = _agent(db_session, seeded_creator.id, "Agent")
    await client.put(
        f"/api/v1/agents/{agent.id}/knowledge-bases",
        json={"knowledge_base_ids": [kb_id]},
        headers=auth_header(creator_token),
    )

    response = await client.delete(
        f"/api/v1/knowledge-bases/{kb_id}", params={"confirm": "true"}, headers=auth_header(creator_token)
    )

    assert response.status_code == 204
    assert db_session.get(KnowledgeBase, kb_id) is None


async def test_a_dead_qdrant_refuses_the_kb_delete_with_a_503_and_keeps_the_row(
    client: AsyncClient, creator_token: str, db_session: Session
) -> None:
    kb = await client.post("/api/v1/knowledge-bases", json={"name": "Sẽ giữ lại"}, headers=auth_header(creator_token))
    kb_id = kb.json()["id"]

    class _DeadQdrant:
        def delete(self, *args: object, **kwargs: object) -> None:
            raise ConnectionError("Qdrant không phản hồi")

    app.dependency_overrides[get_qdrant_client] = lambda: _DeadQdrant()
    try:
        response = await client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=auth_header(creator_token))
    finally:
        app.dependency_overrides.pop(get_qdrant_client, None)

    assert response.status_code == 503
    assert db_session.get(KnowledgeBase, kb_id) is not None


async def test_one_knowledge_base_assigned_to_two_agents_is_a_real_many_to_many(
    client: AsyncClient, creator_token: str, seeded_creator, db_session: Session
) -> None:
    """The acceptance criterion, verbatim: 2 KB, one shared between 2 agents."""
    kb_a = await client.post("/api/v1/knowledge-bases", json={"name": "Tủ A"}, headers=auth_header(creator_token))
    kb_b = await client.post("/api/v1/knowledge-bases", json={"name": "Tủ B"}, headers=auth_header(creator_token))
    kb_a_id, kb_b_id = kb_a.json()["id"], kb_b.json()["id"]

    agent_one = _agent(db_session, seeded_creator.id, "Agent 1")
    agent_two = _agent(db_session, seeded_creator.id, "Agent 2")

    first = await client.put(
        f"/api/v1/agents/{agent_one.id}/knowledge-bases",
        json={"knowledge_base_ids": [kb_a_id, kb_b_id]},
        headers=auth_header(creator_token),
    )
    second = await client.put(
        f"/api/v1/agents/{agent_two.id}/knowledge-bases",
        json={"knowledge_base_ids": [kb_a_id]},
        headers=auth_header(creator_token),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert {kb["id"] for kb in first.json()} == {kb_a_id, kb_b_id}
    assert {kb["id"] for kb in second.json()} == {kb_a_id}

    listed_one = await client.get(f"/api/v1/agents/{agent_one.id}/knowledge-bases", headers=auth_header(creator_token))
    listed_two = await client.get(f"/api/v1/agents/{agent_two.id}/knowledge-bases", headers=auth_header(creator_token))
    assert {kb["id"] for kb in listed_one.json()} == {kb_a_id, kb_b_id}
    assert {kb["id"] for kb in listed_two.json()} == {kb_a_id}

    # Re-fetch the row Postgres actually holds: the many-to-many is real, not
    # an artefact of what the response happened to echo back.
    db_session.expire_all()
    refreshed_kb_a = db_session.get(KnowledgeBase, kb_a_id)
    assert refreshed_kb_a is not None
    assert {agent.id for agent in refreshed_kb_a.agents} == {agent_one.id, agent_two.id}


async def test_replacing_an_agents_knowledge_bases_drops_what_was_removed(
    client: AsyncClient, creator_token: str, seeded_creator, db_session: Session
) -> None:
    kb_a = await client.post("/api/v1/knowledge-bases", json={"name": "Tủ A"}, headers=auth_header(creator_token))
    kb_b = await client.post("/api/v1/knowledge-bases", json={"name": "Tủ B"}, headers=auth_header(creator_token))
    kb_a_id, kb_b_id = kb_a.json()["id"], kb_b.json()["id"]
    agent = _agent(db_session, seeded_creator.id, "Agent")
    await client.put(
        f"/api/v1/agents/{agent.id}/knowledge-bases",
        json={"knowledge_base_ids": [kb_a_id, kb_b_id]},
        headers=auth_header(creator_token),
    )

    response = await client.put(
        f"/api/v1/agents/{agent.id}/knowledge-bases",
        json={"knowledge_base_ids": [kb_b_id]},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 200
    assert {kb["id"] for kb in response.json()} == {kb_b_id}


# --------------------------------------------------------------------------- #
# RAG-13 (#99): the list screen's own fields — document_count, agent_names
# --------------------------------------------------------------------------- #


async def test_the_list_screen_sees_document_count_and_which_agents_use_it(
    client: AsyncClient, creator_token: str, seeded_creator: User, db_session: Session, _no_live_ingestion_backends
) -> None:
    created = await client.post("/api/v1/knowledge-bases", json={"name": "Tủ có tài liệu"}, headers=auth_header(creator_token))
    kb_id = created.json()["id"]
    handbook = b"Nghi phep\n\nNhan vien duoc nghi 12 ngay phep mot nam.\n"
    await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("a.md", io.BytesIO(handbook), "text/markdown")},
        headers=auth_header(creator_token),
    )
    agent = _agent(db_session, seeded_creator.id, "Agent dùng tủ này")
    await client.put(
        f"/api/v1/agents/{agent.id}/knowledge-bases",
        json={"knowledge_base_ids": [kb_id]},
        headers=auth_header(creator_token),
    )

    listed = await client.get("/api/v1/knowledge-bases", headers=auth_header(creator_token))
    got = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=auth_header(creator_token))

    row = next(kb for kb in listed.json() if kb["id"] == kb_id)
    assert row["document_count"] == 1
    assert row["agent_names"] == ["Agent dùng tủ này"]
    assert got.json()["document_count"] == 1
    assert got.json()["agent_names"] == ["Agent dùng tủ này"]


async def test_a_freshly_created_kb_has_zero_documents_and_no_agents(client: AsyncClient, creator_token: str) -> None:
    created = await client.post("/api/v1/knowledge-bases", json={"name": "Tủ trống"}, headers=auth_header(creator_token))

    assert created.json()["document_count"] == 0
    assert created.json()["agent_names"] == []


# --------------------------------------------------------------------------- #
# RAG-13 (#99): the confirmed-graph refusal's 409 body is structured too
# --------------------------------------------------------------------------- #


async def test_a_confirmed_graph_reference_refuses_with_a_blocking_structured_body(
    client: AsyncClient,
    creator_token: str,
    seeded_creator: User,
    solution_spec_version: Callable[..., AgentVersion],
) -> None:
    created = await client.post("/api/v1/knowledge-bases", json={"name": "Tủ có graph confirmed"}, headers=auth_header(creator_token))
    kb_id = created.json()["id"]
    version = solution_spec_version(
        seeded_creator,
        {
            "goal": "Trả lời câu hỏi",
            "users": ["Nhân viên"],
            "data": [{"name": "Tủ", "kind": "uploaded_file", "ref": None}],
            "actions": ["Tra tài liệu"],
            "expected_outcome": ["Câu trả lời"],
            "success_criteria": [{"text": "Đúng", "category": "source_groundedness"}],
            "prohibitions": ["Không bịa"],
        },
        status=VersionStatus.WORKFLOW_CONFIRMED,
        workflow=_confirmed_workflow_referencing(str(kb_id)),
    )

    response = await client.delete(f"/api/v1/knowledge-bases/{kb_id}", headers=auth_header(creator_token))

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["blocking"] is True
    assert detail["affected_agents"] == [f"{version.agent.name} (version {version.id})"]

    # `confirm=true` still cannot override this one — the graph is immutable.
    still_blocked = await client.delete(
        f"/api/v1/knowledge-bases/{kb_id}", params={"confirm": "true"}, headers=auth_header(creator_token)
    )
    assert still_blocked.status_code == 409
    assert still_blocked.json()["detail"]["blocking"] is True


async def test_assigning_someone_elses_knowledge_base_is_refused(
    client: AsyncClient, creator_token: str, second_creator_token: str, seeded_creator, db_session: Session
) -> None:
    other_kb = await client.post(
        "/api/v1/knowledge-bases", json={"name": "Của người khác"}, headers=auth_header(second_creator_token)
    )
    agent = _agent(db_session, seeded_creator.id, "Agent")

    response = await client.put(
        f"/api/v1/agents/{agent.id}/knowledge-bases",
        json={"knowledge_base_ids": [other_kb.json()["id"]]},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 404
