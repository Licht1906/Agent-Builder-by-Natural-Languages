"""`VaultKvStore` — the exported package's `secrets.backend: vault` (DS-13) — against Vault's own KV v2 REST shape.

Seam: `httpx.put`/`httpx.get`, monkeypatched on the module object — the same
shape `tests/test_policy/test_pep.py` uses for `pep.httpx.post`. No live
Vault; a fake with a scripted response.
"""

from typing import Any

import httpx
import pytest

import src.services.vault_secret_store as vault_secret_store
from src.services.vault_secret_store import VaultKvStore, VaultSecretNotFoundError, VaultUnavailableError

_STORE = VaultKvStore(base_url="http://vault:8200", token="root-token", mount="secret")


def test_put_writes_kv_v2_shape_and_sends_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def _capture(url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float) -> httpx.Response:
        sent["url"] = url
        sent["json"] = json
        sent["headers"] = headers
        return httpx.Response(200, json={"data": {"version": 1}}, request=httpx.Request("PUT", url))

    monkeypatch.setattr(vault_secret_store.httpx, "put", _capture)

    _STORE.put("conn/erp_readonly", "postgresql://user:pw@host/db")

    assert sent["url"] == "http://vault:8200/v1/secret/data/conn/erp_readonly"
    assert sent["json"] == {"data": {"value": "postgresql://user:pw@host/db"}}
    assert sent["headers"]["X-Vault-Token"] == "root-token"


def test_get_reads_back_the_value_field(monkeypatch: pytest.MonkeyPatch) -> None:
    def _respond(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"data": {"value": "postgresql://user:pw@host/db"}, "metadata": {"version": 1}}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(vault_secret_store.httpx, "get", _respond)

    assert _STORE.get("conn/erp_readonly") == "postgresql://user:pw@host/db"


def test_get_of_an_unwritten_path_raises_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def _not_found(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        return httpx.Response(404, json={"errors": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(vault_secret_store.httpx, "get", _not_found)

    with pytest.raises(VaultSecretNotFoundError):
        _STORE.get("conn/never_written")


def test_transport_failure_raises_unavailable_never_a_fabricated_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refused(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(vault_secret_store.httpx, "get", _refused)

    with pytest.raises(VaultUnavailableError):
        _STORE.get("conn/erp_readonly")


def test_a_5xx_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _server_error(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        return httpx.Response(500, json={"errors": ["internal"]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(vault_secret_store.httpx, "get", _server_error)

    with pytest.raises(VaultUnavailableError):
        _STORE.get("conn/erp_readonly")
