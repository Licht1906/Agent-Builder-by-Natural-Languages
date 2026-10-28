"""One `SecretStore` for the platform — DS-20 / ADR-0059 §9.

`resolve_credential_via` is the seam every UC-1 tool reads its credential
through; it moved off Vault with its contract intact: a key nobody wrote is
`""` (a connection not wired up yet), a key that exists but no longer
decrypts is an incident and propagates. And the platform's own app no longer
loads a Vault client at all — only the exported package does, for a company
whose secrets live in its own Vault.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from src.services.secret_store import SecretNotFoundError, SecretUnreadableError, resolve_credential_via
from tests.test_api.test_connection_routes import _FakeSecretStore


class _Unreadable(_FakeSecretStore):
    def get(self, key: str) -> str:
        raise SecretUnreadableError(key)


def test_a_written_credential_resolves_to_its_value() -> None:
    store = _FakeSecretStore()
    store.put("conn/erp_readonly", "postgresql://ro@erp/db")

    assert resolve_credential_via(store)("conn/erp_readonly") == "postgresql://ro@erp/db"


def test_a_credential_nobody_wrote_is_empty_not_an_error() -> None:
    assert resolve_credential_via(_FakeSecretStore())("conn/never_written") == ""


def test_a_credential_that_no_longer_decrypts_is_an_incident() -> None:
    with pytest.raises(SecretUnreadableError):
        resolve_credential_via(_Unreadable())("conn/erp_readonly")
    assert not issubclass(SecretUnreadableError, SecretNotFoundError)


def test_the_platform_app_never_loads_a_vault_client() -> None:
    probe = "import sys, src.main; print('src.services.vault_secret_store' in sys.modules)"
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=root, check=True)

    assert result.stdout.strip().splitlines()[-1] == "False"
