"""Every protected endpoint refuses a request that has no usable token.

Ticket #4. `/auth/me` stands in for "a protected endpoint" — the guard it uses is
the same dependency every endpoint written after this ticket will sit behind.
"""

import pytest

from src.config import get_settings
from src.db.models import UserRole
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


async def test_valid_token_identifies_the_caller(client, creator_token) -> None:
    settings = get_settings()

    response = await client.get("/api/v1/auth/me", headers=auth_header(creator_token))

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == settings.seed_creator_email
    assert body["role"] == UserRole.CREATOR


async def test_request_without_a_token_is_refused(client) -> None:
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401
