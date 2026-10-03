"""An admin-only endpoint refuses a creator's token.

Ticket #4. `GET /auth/users` is the first endpoint behind `require_admin` — the
same guard the approval flow (#14) and the admin config screen (#12) will use,
which is why the rejection direction is tested here and not left to those tickets.
"""

import pytest

from src.config import get_settings
from src.db.models import UserRole
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


async def test_admin_can_list_accounts(client, admin_token) -> None:
    settings = get_settings()

    response = await client.get("/api/v1/auth/users", headers=auth_header(admin_token))

    assert response.status_code == 200
    accounts = {user["email"]: user["role"] for user in response.json()}
    assert accounts[settings.seed_creator_email] == UserRole.CREATOR
    assert accounts[settings.seed_admin_email] == UserRole.ADMIN


async def test_creator_token_is_forbidden(client, creator_token) -> None:
    response = await client.get("/api/v1/auth/users", headers=auth_header(creator_token))

    assert response.status_code == 403
