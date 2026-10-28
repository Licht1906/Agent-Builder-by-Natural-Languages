"""Signing up: UC1-01 (#117), spec.md §3/§4.1, FR-01..FR-03.

The seam is HTTP — these tests only ever see what a signup form sees. Every
account they create is deleted afterwards, because `users` is shared by the
whole suite and a leftover row is a stranger the ownership tests have to
account for.

Bootstrap ("account #1 becomes agent_admin") is covered at the service seam
in `tests/test_auth/test_registration.py`, not here — by the time any test in
this module runs, the seeded creator and admin already exist, so a signup
through this suite is never actually the installation's first account.
"""

from collections.abc import Generator

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.auth.registration import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from src.db.models import User
from tests.conftest import seeded_emails

pytestmark = pytest.mark.asyncio

_EMAIL = "newcomer@example.com"
_PASSWORD = "a-long-enough-password"
_JOB_TITLE = "Nhân viên kinh doanh"
_DEPARTMENT = "SALES"


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "email": _EMAIL,
        "password": _PASSWORD,
        "role": "creator",
        "job_title": _JOB_TITLE,
        "department": _DEPARTMENT,
    }
    return {**body, **overrides}


@pytest.fixture(autouse=True)
def _clean_up_registered_accounts(db_session: Session) -> Generator[None, None, None]:
    """Remove anything a test in this module signed up, however the test ended.

    `ilike`, not `like`: `test_the_address_is_case_insensitive` registers
    "Newcomer@Example.com" verbatim, and a case-sensitive pattern would leave
    that row behind to occupy `newcomer@example.com`'s case-insensitively
    unique slot for every test that runs afterwards.
    """
    yield
    db_session.execute(delete(User).where(User.email.ilike("%@example.com"), User.email.notin_(seeded_emails())))
    db_session.commit()


async def test_registering_creates_a_pending_account_and_returns_no_token(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/register", json=_payload())

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["role"] == "creator"
    assert body["status"] == "pending"
    assert body["job_title"] == _JOB_TITLE
    assert body["department"] == _DEPARTMENT
    assert body["employee_id"]
    assert "access_token" not in body


async def test_the_address_is_case_insensitive(client: AsyncClient) -> None:
    """Registering as `Newcomer@` claims `newcomer@` too — one account, not two."""
    await client.post("/api/v1/auth/register", json=_payload(email="Newcomer@Example.com"))

    response = await client.post("/api/v1/auth/register", json=_payload(email=_EMAIL))

    assert response.status_code == 409


async def test_an_address_that_is_already_taken_is_refused(client: AsyncClient) -> None:
    await client.post("/api/v1/auth/register", json=_payload())

    response = await client.post("/api/v1/auth/register", json=_payload())

    assert response.status_code == 409
    assert "access_token" not in response.json()


async def test_a_short_password_is_refused_with_the_reason(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/register", json=_payload(password="short"))

    assert response.status_code == 422
    assert str(MIN_PASSWORD_LENGTH) in response.json()["detail"]


async def test_an_overlong_password_is_refused(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/register", json=_payload(password="x" * (MAX_PASSWORD_LENGTH + 1)))

    assert response.status_code == 422


async def test_a_malformed_address_is_refused(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/register", json=_payload(email="not-an-address"))

    assert response.status_code == 422


@pytest.mark.parametrize("missing_field", ["role", "job_title", "department"])
async def test_a_missing_required_field_is_refused(client: AsyncClient, missing_field: str) -> None:
    body = _payload()
    del body[missing_field]

    response = await client.post("/api/v1/auth/register", json=body)

    assert response.status_code == 422


async def test_a_blank_job_title_is_refused(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/register", json=_payload(job_title="   "))

    assert response.status_code == 422


async def test_an_explicit_employee_id_is_honoured(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/register", json=_payload(employee_id="E555"))

    assert response.status_code == 201, response.text
    assert response.json()["employee_id"] == "E555"


async def test_a_duplicate_employee_id_is_refused(client: AsyncClient) -> None:
    await client.post("/api/v1/auth/register", json=_payload(employee_id="E556"))

    response = await client.post(
        "/api/v1/auth/register", json=_payload(email="second@example.com", employee_id="E556")
    )

    assert response.status_code == 409
