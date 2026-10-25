"""UC1-01 (#117), FR-03: a pending account cannot log in until an `agent_admin` activates it.

The seam is HTTP — `POST /admin/users/{id}/activate` (`src.api.admin_routes`),
gated by the same `require_admin` guard `GET /auth/users` sits behind
(`tests/test_api/test_admin_only.py`), which since UC1-01 also accepts the
`agent_admin` role, not just V2's `admin`.
"""

from collections.abc import Generator

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.db.models import User
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

_EMAIL = "pending-hire@example.com"
_SECOND_EMAIL = "second-pending-hire@example.com"
_PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def _clean_up_registered_accounts(db_session: Session) -> Generator[None, None, None]:
    yield
    # Only the accounts *this file* registers, named one by one rather than
    # matched by a `%@example.com` wildcard. The wildcard reached accounts other
    # `tests/test_api` modules had already given a `uc1_agent_spec_versions` row
    # to, and deleting one of those raises `ForeignKeyViolation` in teardown —
    # which poisons the shared session and fails every test after it in this
    # file, none of which had anything wrong with them.
    db_session.execute(delete(User).where(User.email.in_([_EMAIL, _SECOND_EMAIL])))
    db_session.commit()


async def _register_pending(client: AsyncClient) -> int:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": _EMAIL,
            "password": _PASSWORD,
            "role": "sales_rep",
            "job_title": "Nhân viên kinh doanh",
            "department": "SALES",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"
    return response.json()["id"]


async def test_a_pending_account_cannot_log_in(client: AsyncClient) -> None:
    await _register_pending(client)

    response = await client.post("/api/v1/auth/login", json={"email": _EMAIL, "password": _PASSWORD})

    assert response.status_code == 403
    assert "access_token" not in response.json()


async def test_a_non_admin_cannot_activate_an_account(client: AsyncClient, creator_token: str) -> None:
    user_id = await _register_pending(client)

    response = await client.post(f"/api/v1/admin/users/{user_id}/activate", headers=auth_header(creator_token))

    assert response.status_code == 403


async def test_an_admin_can_activate_a_pending_account_and_it_can_then_log_in(
    client: AsyncClient, admin_token: str
) -> None:
    user_id = await _register_pending(client)

    activation = await client.post(f"/api/v1/admin/users/{user_id}/activate", headers=auth_header(admin_token))
    assert activation.status_code == 200, activation.text
    assert activation.json()["status"] == "active"

    login = await client.post("/api/v1/auth/login", json={"email": _EMAIL, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    assert login.json()["access_token"]


async def test_activating_an_already_active_account_is_idempotent(client: AsyncClient, admin_token: str) -> None:
    user_id = await _register_pending(client)
    first = await client.post(f"/api/v1/admin/users/{user_id}/activate", headers=auth_header(admin_token))
    assert first.status_code == 200

    second = await client.post(f"/api/v1/admin/users/{user_id}/activate", headers=auth_header(admin_token))

    assert second.status_code == 200
    assert second.json()["status"] == "active"


async def test_activating_an_unknown_account_is_404(client: AsyncClient, admin_token: str) -> None:
    response = await client.post("/api/v1/admin/users/999999999/activate", headers=auth_header(admin_token))

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# The queue the refused login points at: `GET /admin/users?status=pending`.
# Without it an admin told to activate somebody has no way to learn their id.
# --------------------------------------------------------------------------- #


async def test_a_non_admin_cannot_read_the_user_directory(client: AsyncClient, creator_token: str) -> None:
    response = await client.get("/api/v1/admin/users", headers=auth_header(creator_token))

    assert response.status_code == 403


async def test_the_pending_filter_returns_the_account_waiting_to_be_activated(
    client: AsyncClient, admin_token: str
) -> None:
    user_id = await _register_pending(client)

    response = await client.get("/api/v1/admin/users?status=pending", headers=auth_header(admin_token))

    assert response.status_code == 200, response.text
    rows = response.json()
    mine = next(row for row in rows if row["id"] == user_id)
    assert mine["email"] == _EMAIL
    assert mine["created_at"]
    assert {row["status"] for row in rows} == {"pending"}
    # The queue is a list of *other people* — it must not carry anything that
    # would let the reader act as one of them.
    assert "password_hash" not in mine


async def test_an_activated_account_leaves_the_pending_queue(client: AsyncClient, admin_token: str) -> None:
    user_id = await _register_pending(client)

    await client.post(f"/api/v1/admin/users/{user_id}/activate", headers=auth_header(admin_token))
    response = await client.get("/api/v1/admin/users?status=pending", headers=auth_header(admin_token))

    assert response.status_code == 200
    assert user_id not in [row["id"] for row in response.json()]


async def test_an_unfiltered_call_lists_active_accounts_too(client: AsyncClient, admin_token: str) -> None:
    user_id = await _register_pending(client)

    response = await client.get("/api/v1/admin/users", headers=auth_header(admin_token))

    assert response.status_code == 200
    ids = [row["id"] for row in response.json()]
    assert user_id in ids
    # The seeded admin is `active`, so an unfiltered call is strictly wider
    # than the pending one — this is what stops the screen from mistaking the
    # directory for the queue.
    assert len(ids) > 1


async def test_the_queue_is_oldest_first(client: AsyncClient, admin_token: str) -> None:
    first = await _register_pending(client)
    second = await client.post(
        "/api/v1/auth/register",
        json={
            "email": _SECOND_EMAIL,
            "password": _PASSWORD,
            "role": "sales_rep",
            "job_title": "Nhân viên kinh doanh",
            "department": "SALES",
        },
    )
    assert second.status_code == 201, second.text

    response = await client.get("/api/v1/admin/users?status=pending", headers=auth_header(admin_token))

    ids = [row["id"] for row in response.json()]
    assert ids.index(first) < ids.index(second.json()["id"])


async def test_an_unknown_status_is_refused_rather_than_ignored(client: AsyncClient, admin_token: str) -> None:
    """A typo'd filter must not quietly hand back the whole directory."""
    response = await client.get("/api/v1/admin/users?status=pendign", headers=auth_header(admin_token))

    assert response.status_code == 422
