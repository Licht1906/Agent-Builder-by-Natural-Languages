"""HashiCorp Vault KV v2 — the exported package's `secrets.backend: vault` (DS-13).

A client, not a service the platform runs. The platform itself reads every
credential through `src.services.secret_store.SecretStore` and no longer
ships a Vault (DS-20 / ADR-0059 §9); this stays for a company that keeps its
own secrets in Vault and points its package at it (`src.standalone.config`).

Raw REST calls, not the `hvac` SDK: one HTTP dependency this platform already
has, rather than a second one whose retry/redirect behaviour this module
would not control.

Every method fails loudly. Vault unreachable, refusing the token, or a 5xx
is never "no secret" — `VaultUnavailableError` — the same distinction
`SecretUnreadableError` draws from `SecretNotFoundError` in
`src.services.secret_store`: an incident is not a missing reference.
"""

from dataclasses import dataclass
from typing import Any

import httpx

_SECRET_FIELD = "value"

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
