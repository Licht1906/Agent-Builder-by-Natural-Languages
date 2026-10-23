"""The secret store over HTTP — who may write to it, and what it never echoes back (RAG-19 / #105, ADR-0045).

Every response is asserted to carry `key` and nothing else — the endpoint-level
half of "no response path can return a secret", the other half of which
`tests/test_workflow/test_registrations.py` covers for the `invoke` path.
"""

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from src.db.models import SecretStoreEntry
from src.services.secret_store import EncryptedDbSecretStore
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


def _key() -> str:
    return f"mcp/{uuid4().hex}"


@pytest.fixture(autouse=True)
def _no_entry_outlives_its_test(db_session: Session):
    yield
    for row in db_session.query(SecretStoreEntry).all():
        db_session.delete(row)
    db_session.commit()


async def test_admin_sets_a_secret_and_the_response_never_carries_the_value(client, admin_token) -> None:
    key = _key()

    response = await client.put(
        f"/api/v1/admin/secrets/{key}", json={"value": "sk-live-abc123"}, headers=auth_header(admin_token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"key": key}
    assert "sk-live-abc123" not in response.text


async def test_a_creator_may_not_set_a_secret(client, creator_token) -> None:
    response = await client.put(
        f"/api/v1/admin/secrets/{_key()}", json={"value": "sk-live-abc123"}, headers=auth_header(creator_token)
    )

    assert response.status_code == 403


async def test_put_twice_overwrites_the_earlier_value(client, admin_token, db_session: Session) -> None:
    key = _key()
    await client.put(f"/api/v1/admin/secrets/{key}", json={"value": "first"}, headers=auth_header(admin_token))
    await client.put(f"/api/v1/admin/secrets/{key}", json={"value": "second"}, headers=auth_header(admin_token))

    assert EncryptedDbSecretStore(db=db_session).get(key) == "second"


async def test_rotating_an_existing_secret_changes_its_value(client, admin_token, db_session: Session) -> None:
    key = _key()
    await client.put(f"/api/v1/admin/secrets/{key}", json={"value": "old"}, headers=auth_header(admin_token))

    response = await client.post(
        f"/api/v1/admin/secrets/{key}/rotate", json={"value": "new"}, headers=auth_header(admin_token)
    )

    assert response.status_code == 200
    assert response.json() == {"key": key}
    assert EncryptedDbSecretStore(db=db_session).get(key) == "new"


async def test_rotating_a_key_nobody_set_is_404_not_a_silent_create(client, admin_token) -> None:
    response = await client.post(
        f"/api/v1/admin/secrets/{_key()}/rotate", json={"value": "new"}, headers=auth_header(admin_token)
    )

    assert response.status_code == 404


async def test_deleting_a_secret_removes_it(client, admin_token, db_session: Session) -> None:
    key = _key()
    await client.put(f"/api/v1/admin/secrets/{key}", json={"value": "value"}, headers=auth_header(admin_token))

    response = await client.delete(f"/api/v1/admin/secrets/{key}", headers=auth_header(admin_token))

    assert response.status_code == 204
    assert db_session.get(SecretStoreEntry, key) is None


async def test_deleting_a_key_nobody_set_is_not_an_error(client, admin_token) -> None:
    response = await client.delete(f"/api/v1/admin/secrets/{_key()}", headers=auth_header(admin_token))

    assert response.status_code == 204
