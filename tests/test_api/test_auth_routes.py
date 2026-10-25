"""Login: a correct password gets a role-bearing token, a wrong one gets nothing.

Ticket #4. The seam is HTTP — these tests only ever see what a client sees.
"""

import pytest

from src.config import get_settings
from src.db.models import UserRole

pytestmark = pytest.mark.asyncio


async def test_login_with_correct_credentials_returns_a_token(client) -> None:
    settings = get_settings()

    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": settings.seed_creator_email,
            "password": settings.seed_creator_password,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["role"] == UserRole.CREATOR
    assert body["access_token"]


async def test_login_with_wrong_password_is_rejected(client) -> None:
    settings = get_settings()

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": settings.seed_creator_email, "password": "not-the-password"},
    )

    assert response.status_code == 401
    assert "access_token" not in response.json()
