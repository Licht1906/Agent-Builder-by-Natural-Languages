"""UC1-02 (#118), FR-05 (Should) — `POST /auth/ldap-bind`.

A second door beside `/login`: LDAP/AD authenticates the account when the
directory is configured, local Argon2id otherwise. The seam is HTTP, same as
`/login`'s own suite — and the claim under test is specifically that turning
LDAP off leaves the local flow unchanged (§4.1 AC), so every "off" test below
asserts against the *same* behaviour `/login` already has.
"""

from collections.abc import Generator

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.auth.ldap import get_ldap_authenticator
from src.config import get_settings
from src.db.models import User, UserRole
from src.main import app
from tests.conftest import seeded_emails

pytestmark = pytest.mark.asyncio

_PENDING_EMAIL = "ldap-pending@example.com"
_PENDING_PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def _clean_up_registered_accounts(db_session: Session) -> Generator[None, None, None]:
    yield
    db_session.execute(delete(User).where(User.email.like("%@example.com"), User.email.notin_(seeded_emails())))
    db_session.commit()


@pytest.fixture
def fake_directory() -> Generator[dict[tuple[str, str], bool], None, None]:
    """Point the LDAP seam at a scripted directory: `{(email, password): bind_ok}`.

    An (email, password) pair this dict does not mention fails to bind, the way
    a real directory refuses an account or password it does not recognise.
    """
    outcomes: dict[tuple[str, str], bool] = {}
    app.dependency_overrides[get_ldap_authenticator] = lambda: (
        lambda email, password: outcomes.get((email, password), False)
    )
    yield outcomes
    app.dependency_overrides.pop(get_ldap_authenticator, None)


@pytest.fixture
def ldap_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ldap_server_url", "ldap://dc.corp.local")
    monkeypatch.setattr(settings, "ldap_user_dn_template", "uid={email},ou=people,dc=corp,dc=local")


async def test_ldap_off_authenticates_with_the_local_password(client: AsyncClient) -> None:
    """Same account, same password, same result `/login` would give — LDAP off is a no-op."""
    settings = get_settings()

    response = await client.post(
        "/api/v1/auth/ldap-bind",
        json={"email": settings.seed_creator_email, "password": settings.seed_creator_password},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["role"] == UserRole.CREATOR
    assert body["access_token"]


async def test_ldap_off_rejects_the_wrong_local_password(client: AsyncClient) -> None:
    settings = get_settings()

    response = await client.post(
        "/api/v1/auth/ldap-bind",
        json={"email": settings.seed_creator_email, "password": "not-the-password"},
    )

    assert response.status_code == 401
    assert "access_token" not in response.json()


async def test_ldap_on_authenticates_a_real_directory_account(
    client: AsyncClient, ldap_configured: None, fake_directory: dict[tuple[str, str], bool]
) -> None:
    """AC: "bật LDAP thì đăng nhập bằng tài khoản AD thật thành công" — even with
    the wrong local password, because LDAP replaces that check entirely once on."""
    settings = get_settings()
    fake_directory[(settings.seed_creator_email, "whatever-the-ad-password-is")] = True

    response = await client.post(
        "/api/v1/auth/ldap-bind",
        json={"email": settings.seed_creator_email, "password": "whatever-the-ad-password-is"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


async def test_ldap_on_rejects_a_failed_bind_even_with_the_right_local_password(
    client: AsyncClient, ldap_configured: None, fake_directory: dict[tuple[str, str], bool]
) -> None:
    """LDAP on means LDAP decides — the local hash is not consulted as a fallback per request."""
    settings = get_settings()

    response = await client.post(
        "/api/v1/auth/ldap-bind",
        json={"email": settings.seed_creator_email, "password": settings.seed_creator_password},
    )

    assert response.status_code == 401


async def test_ldap_on_with_no_matching_local_account_is_rejected_not_crashed(
    client: AsyncClient, ldap_configured: None, fake_directory: dict[tuple[str, str], bool]
) -> None:
    fake_directory[("nobody@corp.local", "any-password")] = True

    response = await client.post(
        "/api/v1/auth/ldap-bind",
        json={"email": "nobody@corp.local", "password": "any-password"},
    )

    assert response.status_code == 401


async def test_a_pending_account_is_still_refused_through_ldap_bind(
    client: AsyncClient, ldap_configured: None, fake_directory: dict[tuple[str, str], bool]
) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={
            "email": _PENDING_EMAIL,
            "password": _PENDING_PASSWORD,
            "role": "sales_rep",
            "job_title": "Nhân viên kinh doanh",
            "department": "SALES",
        },
    )
    fake_directory[(_PENDING_EMAIL, "ad-password")] = True

    response = await client.post(
        "/api/v1/auth/ldap-bind",
        json={"email": _PENDING_EMAIL, "password": "ad-password"},
    )

    assert response.status_code == 403
