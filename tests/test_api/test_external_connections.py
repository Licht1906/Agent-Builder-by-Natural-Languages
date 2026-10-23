"""The `/external-connections` API — RAG-20 / #106.

Every response is asserted to never carry `secret_ref` — the endpoint-level
half of "GET (mask mọi thứ nhạy cảm)". `get_connection_http_get` is
overridden by every test in this module, autouse and refusing by default:
nothing here reaches a real socket unless a test opts in by installing its
own fake, the same discipline `_no_live_ingestion_backends` uses for uploads.
"""

from collections.abc import Generator

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import ExternalConnection, KnowledgeBase, KnowledgeBaseMode
from src.deployments.rate_limit import SlidingWindowLimiter
from src.main import app
from src.services.connection_probe import get_connection_http_get
from src.services.connection_test_rate_limit import get_connection_test_rate_limiter
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "My Qdrant",
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
def _refuse_real_dial_out() -> Generator[None, None, None]:
    """No test in this module reaches a real vector database unless it installs its own fake."""

    def _refuse(url: str, headers: dict[str, str] | None) -> httpx.Response:
        raise AssertionError("test must override get_connection_http_get to simulate a probe")

    app.dependency_overrides[get_connection_http_get] = lambda: _refuse
    yield
    app.dependency_overrides.pop(get_connection_http_get, None)


@pytest.fixture(autouse=True)
def _no_connection_outlives_its_test(db_session: Session) -> Generator[None, None, None]:
    """Every test starts from an empty `external_connections` table — `list` claims read on rows, not on history."""
    yield
    for row in db_session.query(ExternalConnection).all():
        db_session.delete(row)
    db_session.commit()


@pytest.fixture
def fresh_connection_test_rate_limiter() -> Generator[SlidingWindowLimiter, None, None]:
    """Every test's own `/test` counters — `fresh_upload_rate_limiter`'s reason."""
    limiter = SlidingWindowLimiter()
    app.dependency_overrides[get_connection_test_rate_limiter] = lambda: limiter
    yield limiter
    app.dependency_overrides.pop(get_connection_test_rate_limiter, None)


def _matching_dimension_response(url: str, headers: dict[str, str] | None) -> httpx.Response:
    return httpx.Response(200, json={"result": {"config": {"params": {"vectors": {"size": 1024}}}}})


async def test_create_returns_a_masked_response_never_a_raw_secret_ref(
    client: AsyncClient, creator_token: str
) -> None:
    response = await client.post(
        "/api/v1/external-connections",
        json=_payload(secret_ref="ext/some-key"),
        headers=auth_header(creator_token),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["has_secret"] is True
    assert "secret_ref" not in body
    assert "ext/some-key" not in response.text
    assert body["status"] == "untested"
    assert body["last_checked_at"] is None


async def test_create_masks_userinfo_embedded_in_the_connection_url(client: AsyncClient, creator_token: str) -> None:
    response = await client.post(
        "/api/v1/external-connections",
        json=_payload(connection_url="https://admin:hunter2@qdrant.internal:6333"),
        headers=auth_header(creator_token),
    )

    assert response.status_code == 201, response.text
    assert "hunter2" not in response.text
    assert response.json()["connection_url"] == "https://***@qdrant.internal:6333"


async def test_create_never_dials_out(client: AsyncClient, creator_token: str) -> None:
    """Creating a row is not a network call — `_refuse_real_dial_out` proves it by not tripping."""
    response = await client.post(
        "/api/v1/external-connections",
        json=_payload(connection_url="http://169.254.169.254:6333"),
        headers=auth_header(creator_token),
    )

    assert response.status_code == 201, response.text


async def test_list_is_scoped_to_the_caller(client: AsyncClient, creator_token: str, second_creator_token: str) -> None:
    await client.post("/api/v1/external-connections", json=_payload(name="mine"), headers=auth_header(creator_token))
    await client.post(
        "/api/v1/external-connections", json=_payload(name="theirs"), headers=auth_header(second_creator_token)
    )

    response = await client.get("/api/v1/external-connections", headers=auth_header(creator_token))

    assert response.status_code == 200
    assert [row["name"] for row in response.json()] == ["mine"]


async def test_getting_someone_elses_connection_is_404(
    client: AsyncClient, creator_token: str, second_creator_token: str
) -> None:
    created = await client.post(
        "/api/v1/external-connections", json=_payload(), headers=auth_header(creator_token)
    )
    connection_id = created.json()["id"]

    response = await client.get(
        f"/api/v1/external-connections/{connection_id}", headers=auth_header(second_creator_token)
    )

    assert response.status_code == 404


async def test_deleting_an_unused_connection_succeeds(client: AsyncClient, creator_token: str) -> None:
    created = await client.post(
        "/api/v1/external-connections", json=_payload(), headers=auth_header(creator_token)
    )
    connection_id = created.json()["id"]

    response = await client.delete(f"/api/v1/external-connections/{connection_id}", headers=auth_header(creator_token))

    assert response.status_code == 204


async def test_deleting_a_connection_used_by_a_knowledge_base_is_refused(
    client: AsyncClient, creator_token: str, db_session: Session, seeded_creator
) -> None:
    created = await client.post(
        "/api/v1/external-connections", json=_payload(), headers=auth_header(creator_token)
    )
    connection_id = created.json()["id"]
    kb = KnowledgeBase(
        owner_id=seeded_creator.id, name="BYODB Shelf", mode=KnowledgeBaseMode.BYODB, external_connection_id=connection_id
    )
    db_session.add(kb)
    db_session.commit()

    response = await client.delete(f"/api/v1/external-connections/{connection_id}", headers=auth_header(creator_token))

    assert response.status_code == 409
    assert "BYODB Shelf" in response.json()["detail"]["knowledge_bases"]

    db_session.delete(kb)
    db_session.commit()


async def test_test_endpoint_reports_connected_and_persists_it(client: AsyncClient, creator_token: str) -> None:
    created = await client.post(
        "/api/v1/external-connections", json=_payload(), headers=auth_header(creator_token)
    )
    connection_id = created.json()["id"]
    app.dependency_overrides[get_connection_http_get] = lambda: _matching_dimension_response

    response = await client.post(
        f"/api/v1/external-connections/{connection_id}/test", headers=auth_header(creator_token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "connected"
    assert body["last_checked_at"] is not None
    assert body["status_reason"] is None


async def test_a_failed_test_names_the_kind_of_failure_not_only_the_sentence(
    client: AsyncClient, creator_token: str
) -> None:
    """RAG-22 (#108): the connection form writes its own advice off `status_reason`, never off `status_detail`'s prose."""
    created = await client.post("/api/v1/external-connections", json=_payload(), headers=auth_header(creator_token))
    connection_id = created.json()["id"]

    def rejects_the_key(url: str, headers: dict[str, str] | None) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized")

    app.dependency_overrides[get_connection_http_get] = lambda: rejects_the_key

    response = await client.post(
        f"/api/v1/external-connections/{connection_id}/test", headers=auth_header(creator_token)
    )

    assert response.json()["status_reason"] == "auth_rejected"

    listed = await client.get(f"/api/v1/external-connections/{connection_id}", headers=auth_header(creator_token))
    assert listed.json()["status_reason"] == "auth_rejected", "the last verdict's kind outlives the /test response"


async def test_test_endpoint_reports_a_dimension_mismatch_without_crashing(
    client: AsyncClient, creator_token: str
) -> None:
    created = await client.post(
        "/api/v1/external-connections", json=_payload(embedding_dimension=1024), headers=auth_header(creator_token)
    )
    connection_id = created.json()["id"]

    def wrong_dimension(url: str, headers: dict[str, str] | None) -> httpx.Response:
        return httpx.Response(200, json={"result": {"config": {"params": {"vectors": {"size": 768}}}}})

    app.dependency_overrides[get_connection_http_get] = lambda: wrong_dimension

    response = await client.post(
        f"/api/v1/external-connections/{connection_id}/test", headers=auth_header(creator_token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert "768" in body["status_detail"]


async def test_test_endpoint_reports_connection_refused_without_crashing(
    client: AsyncClient, creator_token: str
) -> None:
    created = await client.post(
        "/api/v1/external-connections", json=_payload(), headers=auth_header(creator_token)
    )
    connection_id = created.json()["id"]

    def refused(url: str, headers: dict[str, str] | None) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    app.dependency_overrides[get_connection_http_get] = lambda: refused

    response = await client.post(
        f"/api/v1/external-connections/{connection_id}/test", headers=auth_header(creator_token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"


@pytest.mark.parametrize(
    "connection_url", ["http://169.254.169.254/latest/meta-data/", "http://localhost:6333", "http://127.0.0.1:6333"]
)
async def test_test_endpoint_blocks_internal_addresses_without_dialing_out(
    client: AsyncClient, creator_token: str, connection_url: str
) -> None:
    """The ticket's own three: metadata endpoint, `localhost`, and a loopback literal — all blocked, none a crash."""
    created = await client.post(
        "/api/v1/external-connections", json=_payload(connection_url=connection_url), headers=auth_header(creator_token)
    )
    connection_id = created.json()["id"]
    # `_refuse_real_dial_out` (autouse) is still installed — if the SSRF guard
    # failed to block before dialling, that fake raises and this test fails loudly.

    response = await client.post(
        f"/api/v1/external-connections/{connection_id}/test", headers=auth_header(creator_token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"


async def test_test_endpoint_blocks_a_hostname_that_resolves_to_loopback(
    client: AsyncClient, creator_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DNS-rebinding case: an external-looking hostname that resolves internal — RAG-20's SSRF checklist."""
    import socket

    created = await client.post(
        "/api/v1/external-connections",
        json=_payload(connection_url="http://evil-rebind.example:6333"),
        headers=auth_header(creator_token),
    )
    connection_id = created.json()["id"]

    def rebinding_getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_getaddrinfo)

    response = await client.post(
        f"/api/v1/external-connections/{connection_id}/test", headers=auth_header(creator_token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"


async def test_test_endpoint_is_rate_limited(
    client: AsyncClient,
    creator_token: str,
    monkeypatch: pytest.MonkeyPatch,
    fresh_connection_test_rate_limiter: SlidingWindowLimiter,
) -> None:
    monkeypatch.setattr(get_settings(), "external_connection_test_rate_limit_per_user", 1)
    created = await client.post(
        "/api/v1/external-connections", json=_payload(), headers=auth_header(creator_token)
    )
    connection_id = created.json()["id"]
    app.dependency_overrides[get_connection_http_get] = lambda: _matching_dimension_response

    first = await client.post(f"/api/v1/external-connections/{connection_id}/test", headers=auth_header(creator_token))
    second = await client.post(f"/api/v1/external-connections/{connection_id}/test", headers=auth_header(creator_token))

    assert first.status_code == 200
    assert second.status_code == 429


async def test_test_endpoint_of_someone_elses_connection_is_404(
    client: AsyncClient, creator_token: str, second_creator_token: str
) -> None:
    created = await client.post(
        "/api/v1/external-connections", json=_payload(), headers=auth_header(creator_token)
    )
    connection_id = created.json()["id"]

    response = await client.post(
        f"/api/v1/external-connections/{connection_id}/test", headers=auth_header(second_creator_token)
    )

    assert response.status_code == 404
