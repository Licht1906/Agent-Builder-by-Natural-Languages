"""UC1-08 (#122) — `POST /connections`, `POST /connections/{id}/probe`, spec.md §19.

`get_vault_store` is overridden with an in-process fake KV store (same shape
`app.dependency_overrides[get_pep_client]` uses for the PEP) — no live Vault
container in this suite. `probe_postgres_connection` itself is exercised
directly against seams in `tests/test_services/test_uc1_connection_probe.py`;
here the point is the HTTP contract: the credential never rides in a response,
and only `agent_admin` may reach this router at all.
"""

from dataclasses import dataclass, field

import pytest
from httpx import AsyncClient

from src.main import app
from src.services.uc1_connection_probe import ProbeStatus
from src.services.vault_secret_store import VaultSecretNotFoundError, get_vault_store
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


@dataclass
class _FakeVault:
    values: dict[str, str] = field(default_factory=dict)

    def put(self, path: str, value: str) -> None:
        self.values[path] = value

    def get(self, path: str) -> str:
        if path not in self.values:
            raise VaultSecretNotFoundError(path)
        return self.values[path]


@pytest.fixture
def fake_vault() -> _FakeVault:
    vault = _FakeVault()
    app.dependency_overrides[get_vault_store] = lambda: vault
    yield vault
    app.dependency_overrides.pop(get_vault_store, None)


async def test_creating_a_connection_never_echoes_the_credential(
    client: AsyncClient, agent_admin_token: str, fake_vault: _FakeVault
) -> None:
    response = await client.post(
        "/api/v1/connections",
        json={
            "name": "erp_readonly",
            "type": "postgres",
            "credential": "postgresql://ro:s3cr3t@erp.internal:5432/erp",
            "allowed_objects": ["v_ar_open", "v_customer_safe"],
            "read_only": True,
        },
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["credential_ref"] == "vault://conn/erp_readonly"
    assert "s3cr3t" not in response.text
    assert fake_vault.values["conn/erp_readonly"] == "postgresql://ro:s3cr3t@erp.internal:5432/erp"


async def test_a_duplicate_name_is_refused_rather_than_overwriting_vault(
    client: AsyncClient, agent_admin_token: str, fake_vault: _FakeVault
) -> None:
    payload = {
        "name": "erp_readonly_dup",
        "type": "postgres",
        "credential": "postgresql://ro:first@erp.internal:5432/erp",
        "allowed_objects": [],
        "read_only": True,
    }
    first = await client.post("/api/v1/connections", json=payload, headers=auth_header(agent_admin_token))
    assert first.status_code == 201, first.text

    second = await client.post(
        "/api/v1/connections",
        json={**payload, "credential": "postgresql://ro:second@erp.internal:5432/erp"},
        headers=auth_header(agent_admin_token),
    )
    assert second.status_code == 409
    assert fake_vault.values["conn/erp_readonly_dup"] == "postgresql://ro:first@erp.internal:5432/erp"


async def test_only_agent_admin_may_create_a_connection(
    client: AsyncClient, creator_token: str, fake_vault: _FakeVault
) -> None:
    response = await client.post(
        "/api/v1/connections",
        json={"name": "erp_readonly2", "type": "postgres", "credential": "x", "allowed_objects": [], "read_only": True},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 403


async def test_probe_dry_runs_the_connection_and_persists_the_verdict(
    client: AsyncClient, agent_admin_token: str, fake_vault: _FakeVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await client.post(
        "/api/v1/connections",
        json={
            "name": "erp_probe_target",
            "type": "postgres",
            "credential": "postgresql://ro:pw@erp.internal:5432/erp",
            "allowed_objects": ["v_ar_open", "v_customer_safe"],
            "read_only": True,
        },
        headers=auth_header(agent_admin_token),
    )
    connection_id = created.json()["id"]

    from src.services import uc1_connection_probe as probe_module

    monkeypatch.setattr(probe_module, "live_view_lister", lambda dsn: (lambda schema: ["v_ar_open", "v_customer_safe"]))
    monkeypatch.setattr(
        probe_module,
        "count_rows",
        lambda dsn, name: {"v_ar_open": 1084, "v_customer_safe": 200}[name],
    )

    response = await client.post(f"/api/v1/connections/{connection_id}/probe", headers=auth_header(agent_admin_token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["last_probe_status"] == ProbeStatus.CONNECTED.value
    assert "2 view" in body["last_probe_detail"]
    assert "1284" in body["last_probe_detail"]
    assert {o["object"]: o["row_count"] for o in body["last_probe_objects"]} == {
        "v_ar_open": 1084,
        "v_customer_safe": 200,
    }


async def test_probe_with_a_bad_credential_answers_clearly_rather_than_hanging(
    client: AsyncClient, agent_admin_token: str, fake_vault: _FakeVault
) -> None:
    created = await client.post(
        "/api/v1/connections",
        json={"name": "erp_no_secret", "type": "postgres", "credential": "x", "allowed_objects": [], "read_only": True},
        headers=auth_header(agent_admin_token),
    )
    connection_id = created.json()["id"]
    del fake_vault.values["conn/erp_no_secret"]  # simulate: the secret was never actually reachable

    response = await client.post(f"/api/v1/connections/{connection_id}/probe", headers=auth_header(agent_admin_token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["last_probe_status"] == ProbeStatus.FAILED.value
    assert body["last_probe_detail"]


async def test_probe_on_an_unsupported_connection_type_is_a_422_not_a_crash(
    client: AsyncClient, agent_admin_token: str, fake_vault: _FakeVault
) -> None:
    created = await client.post(
        "/api/v1/connections",
        json={"name": "smtp_internal", "type": "smtp", "credential": "x", "allowed_hosts": ["smtp.internal.corp"], "read_only": False},
        headers=auth_header(agent_admin_token),
    )
    connection_id = created.json()["id"]

    response = await client.post(f"/api/v1/connections/{connection_id}/probe", headers=auth_header(agent_admin_token))

    assert response.status_code == 422


async def test_list_connections_never_returns_a_credential(
    client: AsyncClient, agent_admin_token: str, fake_vault: _FakeVault
) -> None:
    await client.post(
        "/api/v1/connections",
        json={"name": "erp_list_check", "type": "postgres", "credential": "s3cr3t-value", "allowed_objects": [], "read_only": True},
        headers=auth_header(agent_admin_token),
    )

    response = await client.get("/api/v1/connections", headers=auth_header(agent_admin_token))

    assert response.status_code == 200
    assert "s3cr3t-value" not in response.text
