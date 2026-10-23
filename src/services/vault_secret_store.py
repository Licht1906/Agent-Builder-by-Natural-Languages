"""HashiCorp Vault KV v2 — UC1-08 (#122), spec.md §6.2.

Chosen over `src.services.secret_store.EncryptedDbSecretStore` (ADR-0045) for
the reason spec.md §6.2 states explicitly: that store holds BYODB credentials
an admin writes ahead of time through `PUT /admin/secrets/{key}`. A UC-1
connection's credential arrives from a creator, at runtime, typed into a
browser form — Vault KV v2 is what that flow is built for; SOPS+age (the
other candidate) is for secrets baked into a deployment artifact before it
ships, not one a person types in during setup. §6.2 records both as
considered and this as the one chosen. The two stores are not interchangeable
and this module does not try to unify them.

`credential_ref` (`src.agentspec.spec.Connection.credential_ref`) is
`vault://<path>` — `<path>` is this store's key, one KV v2 secret per
connection, under a fixed mount (`settings.vault_kv_mount`). Raw REST calls,
not the `hvac` SDK, the same call `src.services.connection_probe` already made
for Qdrant: one HTTP dependency this platform already has, rather than a
second one whose retry/redirect behaviour this module would not control.

Every method fails loudly. Vault unreachable, refusing the token, or a 5xx
is never "no secret" — `VaultUnavailableError` — the same distinction
`SecretUnreadableError` draws from `SecretNotFoundError` in
`src.services.secret_store`: an incident is not a missing reference.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import Depends

from src.config import get_settings

_SECRET_FIELD = "value"

#: `(vault_path) -> secret`. Named the way this repo names every other
#: single-callable seam (`HttpGet`, `ViewLister`, `PEPClient`) — what
#: `src.compiler.tools.default_tool_implementations` takes as its own
#: `resolve_credential`.
ResolveCredential = Callable[[str], str]


class VaultUnavailableError(RuntimeError):
    """Vault could not be reached, refused the token, or answered with something unusable."""


class VaultSecretNotFoundError(LookupError):
    """`path` names no secret Vault currently holds. `str(self)` is only ever the path — never a value."""


@dataclass(frozen=True, slots=True)
class VaultKvStore:
    """One KV v2 mount, reached over Vault's own REST API.

    Holds only connection facts (`base_url`, `token`, `mount`) — no state of
    its own — the same shape `EncryptedDbSecretStore` holds a bare `Session`,
    so building one around a short-lived request never risks a plaintext
    value outliving the call that fetched it.
    """

    base_url: str
    token: str
    mount: str
    timeout_seconds: float = 5.0

    def put(self, path: str, value: str) -> None:
        """Write `value` as the KV v2 secret at `path`, creating a new version.

        Overwrites silently if `path` already has a value — `put` is how a
        connection's credential is set or replaced (a creator retyping it
        after a typo), and KV v2 keeps every prior version in Vault's own
        history regardless.
        """
        url = f"{self.base_url}/v1/{self.mount}/data/{path}"
        try:
            response = httpx.put(
                url, json={"data": {_SECRET_FIELD: value}}, headers=self._headers(), timeout=self.timeout_seconds
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise VaultUnavailableError(f"Vault từ chối ghi secret tại {path!r}: {exc}") from exc

    def get(self, path: str) -> str:
        """The plaintext value at `path`'s latest KV v2 version.

        Raises `VaultSecretNotFoundError` if nobody `put` this path yet,
        `VaultUnavailableError` for every other way this can fail — unreachable,
        a non-2xx that is not 404, or a 200 whose body this store cannot read
        as a secret it wrote.
        """
        url = f"{self.base_url}/v1/{self.mount}/data/{path}"
        try:
            response = httpx.get(url, headers=self._headers(), timeout=self.timeout_seconds)
        except httpx.HTTPError as exc:
            raise VaultUnavailableError(f"Không kết nối được Vault để đọc {path!r}: {exc}") from exc
        if response.status_code == 404:
            raise VaultSecretNotFoundError(path)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise VaultUnavailableError(f"Vault từ chối đọc secret tại {path!r}: {exc}") from exc
        return _secret_value_of(response.json(), path)

    def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": self.token}


def _secret_value_of(body: Any, path: str) -> str:
    try:
        return str(body["data"]["data"][_SECRET_FIELD])
    except (KeyError, TypeError) as exc:
        raise VaultUnavailableError(f"Vault trả về dữ liệu không đọc được cho {path!r}") from exc


def get_vault_store() -> VaultKvStore:
    """FastAPI dependency: the one place a request builds the real Vault client.

    Declared the way `get_secret_store`/`get_pep_client` are — a test aims
    `POST /connections` at a fake by overriding this, and the real KV v2
    client is the default rather than a branch on a flag.
    """
    settings = get_settings()
    return VaultKvStore(base_url=settings.vault_url, token=settings.vault_token, mount=settings.vault_kv_mount)


VaultStoreDep = Annotated[VaultKvStore, Depends(get_vault_store)]


def resolve_credential_via(vault: VaultKvStore) -> ResolveCredential:
    """A `ResolveCredential` bound to `vault`, degrading a *missing* secret to
    `""` — and never an unreachable one.

    Shared by every caller of `default_tool_implementations` (sandbox chat,
    the golden-set runner) for the reason `src.api.sandbox_routes`'s own
    original copy of this gave: a stub or real tool implementation only needs
    to *prove* its pointer resolves through Vault at call time, and a
    connection nobody has wired up yet (a fresh sandbox run, most tests) must
    not fail the whole turn over it. That argument covers
    `VaultSecretNotFoundError`, and only it.

    `VaultUnavailableError` propagates. Catching it here collapsed exactly the
    distinction this module's own header draws — "an incident is not a missing
    reference" — and did so at the one seam every tool implementation reads
    credentials through, which is the worst possible place to lose it: an
    unreachable Vault arrived at `src.compiler.tools`' ERP tools looking
    identical to "no connection configured", they reported it as an empty
    result set, and the agent told a user there were no overdue receivables.
    A caller that must not fail its whole turn over this catches it and says
    which of the two happened — see `_unreadable` there.
    """

    def _resolve(vault_path: str) -> str:
        try:
            return vault.get(vault_path)
        except VaultSecretNotFoundError:
            return ""

    return _resolve
