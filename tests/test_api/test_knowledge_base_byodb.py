"""Creating a Knowledge Base in BYODB mode, and what that mode refuses — RAG-22 / #108.

RAG-20 (#106) built the `ExternalConnection` row and RAG-21 (#107) taught
retrieval to query through one, but nothing until now could *create* a shelf
in `byodb` mode over HTTP: `POST /knowledge-bases` always made a `managed`
one. The mode-picker screen this ticket is about needs three things this file
pins down —

1. a shelf can be created pointing at one of the caller's own connections,
   and comes back naming it (the list screen's "Kết nối ngoài" badge reads
   exactly those fields);
2. the two halves cannot disagree — `byodb` with no connection, or `managed`
   with one, is refused rather than stored as a row nobody can explain;
3. the warning the screen shows ("dữ liệu trong DB ngoài không được hệ thống
   index hay sửa") is true: uploading a Document onto a BYODB shelf is
   refused by the API, not merely discouraged by the UI.
"""

import io
from collections.abc import Generator

import httpx
import pytest
from httpx import AsyncClient
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from src.db.models import ExternalConnection, KnowledgeBase, User
from src.main import app
from src.services.connection_probe import get_connection_http_get
from src.services.qdrant import DENSE_VECTOR_SIZE
from src.services.reranker import RerankResult
from src.services.secret_store import EncryptedDbSecretStore
from src.workflow.knowledge import QdrantKnowledgeStore
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


def _connection_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "Qdrant của tôi",
        "provider": "qdrant",
        "connection_url": "https://8.8.8.8:6333",
        "secret_ref": "",
        "collection_name": "legal_docs",
        "embedding_dimension": 1024,
        "distance_metric": "cosine",
        "field_mapping": {},
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _no_dial_out_and_no_connection_outlives_its_test(db_session: Session) -> Generator[None, None, None]:
    """No test here reaches a real socket, and every one starts from an empty `external_connections` table.

    Both halves are `test_external_connections.py`'s own discipline: the probe
    transport refuses by default so a test that means to dial has to say so,
    and `list`-style claims are about rows this test made, never about history.
    """

    def _refuse(url: str, headers: dict[str, str] | None) -> httpx.Response:
        raise AssertionError("test must override get_connection_http_get to simulate a probe")

    app.dependency_overrides[get_connection_http_get] = lambda: _refuse
    yield
    app.dependency_overrides.pop(get_connection_http_get, None)
    # Shelves first, in their own transaction: a connection is only deletable
    # once nothing points at it (`knowledge_bases_external_connection_id_fkey`),
    # and the two deletes are not ordered by any relationship SQLAlchemy can see.
    for row in db_session.query(KnowledgeBase).filter(KnowledgeBase.external_connection_id.is_not(None)).all():
        db_session.delete(row)
    db_session.commit()
    for row in db_session.query(ExternalConnection).all():
        db_session.delete(row)
    db_session.commit()


async def _a_connection(client: AsyncClient, token: str, **overrides: object) -> int:
    created = await client.post(
        "/api/v1/external-connections", json=_connection_payload(**overrides), headers=auth_header(token)
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_a_byodb_shelf_comes_back_naming_the_connection_it_points_at(
    client: AsyncClient, creator_token: str
) -> None:
    connection_id = await _a_connection(client, creator_token, name="Qdrant nội bộ")

    response = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Tủ ngoài", "mode": "byodb", "external_connection_id": connection_id},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mode"] == "byodb"
    assert body["external_connection_id"] == connection_id
    assert body["external_connection_name"] == "Qdrant nội bộ"
    assert body["external_connection_status"] == "untested"


async def test_a_managed_shelf_still_names_no_connection(client: AsyncClient, creator_token: str) -> None:
    response = await client.post(
        "/api/v1/knowledge-bases", json={"name": "Tủ tự quản lý"}, headers=auth_header(creator_token)
    )

    body = response.json()
    assert body["mode"] == "managed"
    assert body["external_connection_id"] is None
    assert body["external_connection_name"] is None
    assert body["external_connection_status"] is None


async def test_byodb_without_a_connection_is_refused(client: AsyncClient, creator_token: str) -> None:
    response = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Tủ ngoài rỗng", "mode": "byodb"},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422, response.text


async def test_managed_with_a_connection_is_refused(client: AsyncClient, creator_token: str) -> None:
    connection_id = await _a_connection(client, creator_token)

    response = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Tủ lẫn lộn", "mode": "managed", "external_connection_id": connection_id},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422, response.text


async def test_a_shelf_cannot_point_at_somebody_elses_connection(
    client: AsyncClient, creator_token: str, second_creator_token: str
) -> None:
    connection_id = await _a_connection(client, second_creator_token, name="Của người khác")

    response = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Tủ mượn kết nối", "mode": "byodb", "external_connection_id": connection_id},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 404, response.text


async def test_the_list_screen_can_tell_the_two_modes_apart(client: AsyncClient, creator_token: str) -> None:
    connection_id = await _a_connection(client, creator_token, name="Qdrant riêng")
    managed = await client.post(
        "/api/v1/knowledge-bases", json={"name": "Tủ nội bộ"}, headers=auth_header(creator_token)
    )
    byodb = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Tủ ngoài của riêng test này", "mode": "byodb", "external_connection_id": connection_id},
        headers=auth_header(creator_token),
    )

    listed = await client.get("/api/v1/knowledge-bases", headers=auth_header(creator_token))

    by_id = {kb["id"]: kb for kb in listed.json()}
    assert by_id[managed.json()["id"]]["mode"] == "managed"
    assert by_id[byodb.json()["id"]]["mode"] == "byodb"
    assert by_id[byodb.json()["id"]]["external_connection_name"] == "Qdrant riêng"


async def test_a_shelf_created_this_way_is_searchable_through_its_own_connection(
    client: AsyncClient, creator_token: str, db_session: Session, seeded_creator: User
) -> None:
    """The ticket's second acceptance line, end to end: connect, then "query thử qua agent trả kết quả".

    `/test` says `connected` and the *same* shelf, created through the route
    this ticket adds, then answers a search through `QdrantKnowledgeStore` —
    the two halves proven against one row rather than against two rows built
    to look alike. Both network seams are fakes (`get_connection_http_get`
    for the probe, `http_post` for the search), the discipline every BYODB
    test in this repo already follows.
    """
    created = await client.post(
        "/api/v1/external-connections",
        json=_connection_payload(embedding_dimension=DENSE_VECTOR_SIZE),
        headers=auth_header(creator_token),
    )
    connection_id = created.json()["id"]

    def reports_matching_dimension(url: str, headers: dict[str, str] | None) -> httpx.Response:
        return httpx.Response(200, json={"result": {"config": {"params": {"vectors": {"size": DENSE_VECTOR_SIZE}}}}})

    app.dependency_overrides[get_connection_http_get] = lambda: reports_matching_dimension
    tested = await client.post(
        f"/api/v1/external-connections/{connection_id}/test", headers=auth_header(creator_token)
    )
    assert tested.json()["status"] == "connected", tested.text

    shelf = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Tủ ngoài tra cứu được", "mode": "byodb", "external_connection_id": connection_id},
        headers=auth_header(creator_token),
    )
    assert shelf.json()["external_connection_status"] == "connected"

    def answers_one_hit(url: str, headers: dict[str, str] | None, json_body: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "points": [
                        {
                            "id": "p1",
                            "score": 0.9,
                            "payload": {"chunk_text": "Điều 12 quy định về giờ nghỉ trưa", "doc_name": "external.pdf"},
                        }
                    ]
                }
            },
        )

    vector = [1.0, 0.0] + [0.0] * (DENSE_VECTOR_SIZE - 2)
    store = QdrantKnowledgeStore(
        db=db_session,
        owner_id=seeded_creator.id,
        qdrant_client=QdrantClient(":memory:"),
        embedding_client=lambda texts: [vector for _ in texts],
        reranker_client=lambda query, documents: RerankResult(
            ranked_indices=list(range(len(documents))), scores=[1.0] * len(documents), reranked=True
        ),
        secret_store=EncryptedDbSecretStore(db=db_session),
        http_post=answers_one_hit,
    )

    passages = store.search("Điều 12 quy định gì", source_refs=[str(shelf.json()["id"])])

    assert [passage.text for passage in passages] == ["Điều 12 quy định về giờ nghỉ trưa"]
    assert passages[0].doc_name == "external.pdf"


async def test_uploading_onto_a_byodb_shelf_is_refused_in_words_a_creator_can_act_on(
    client: AsyncClient, creator_token: str
) -> None:
    """The screen's "we never index or change your data" warning, enforced rather than merely displayed."""
    connection_id = await _a_connection(client, creator_token)
    created = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Tủ ngoài không nhận upload", "mode": "byodb", "external_connection_id": connection_id},
        headers=auth_header(creator_token),
    )
    kb_id = created.json()["id"]

    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": ("chinh-sach.md", io.BytesIO(b"# Noi dung"), "text/markdown")},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422, response.text
    assert "database" in response.json()["detail"].lower()

    listed = await client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", headers=auth_header(creator_token))
    assert listed.json() == []
