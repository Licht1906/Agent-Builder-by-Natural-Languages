"""UC1-26 (#142) — `POST /control-plane/templates/{template_id}/pull`, spec.md §19/§4.7.

`httpx.get` is monkeypatched on `src.services.control_plane` (the seam that
module's own tests already use) — no live control plane in this suite either.
"""

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import AsyncClient

import src.services.control_plane as control_plane
from src.config import get_settings
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


def _sign(private_key: Ed25519PrivateKey, content: dict) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(private_key.sign(canonical)).decode()


@pytest.fixture
def _control_plane_serving(monkeypatch: pytest.MonkeyPatch):
    private_key = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(private_key.public_key().public_bytes_raw()).decode()
    monkeypatch.setattr(get_settings(), "control_plane_url", "https://control-plane.vendor.test")
    monkeypatch.setattr(get_settings(), "control_plane_public_key", public_b64)

    def _get(url: str, *, timeout: float) -> httpx.Response:
        content = {"tools": ["erp.query_receivables"]}
        body = {"content": content, "signature": _sign(private_key, content), "build_id": "2026.08.15"}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(control_plane.httpx, "get", _get)


async def test_pulling_a_template_returns_its_verified_content(
    client: AsyncClient, agent_admin_token: str, _control_plane_serving
) -> None:
    response = await client.post(
        "/api/v1/control-plane/templates/uc1-ar-assistant/pull", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["template_id"] == "uc1-ar-assistant"
    assert body["build_id"] == "2026.08.15"
    assert body["content"] == {"tools": ["erp.query_receivables"]}


async def test_pulling_with_no_control_plane_configured_is_a_502(client: AsyncClient, agent_admin_token: str) -> None:
    response = await client.post(
        "/api/v1/control-plane/templates/uc1-ar-assistant/pull", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 502


async def test_only_agent_admin_may_pull_a_template(client: AsyncClient, creator_token: str) -> None:
    response = await client.post(
        "/api/v1/control-plane/templates/uc1-ar-assistant/pull", headers=auth_header(creator_token)
    )

    assert response.status_code == 403
