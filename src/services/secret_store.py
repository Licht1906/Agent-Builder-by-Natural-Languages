"""The secret store `ToolCredential.secret_ref` was always documented as pointing into — RAG-19 / ADR-0045.

`credential_secret_ref` on `ToolRegistration` (and, later, an `external_
connections` row) is a **reference**: a DB row holding it may leak, and the
worst that leaks is a name. `get(key)` is the only way from that name back to
the value it names, and it is this module's whole reason to exist — before
this ticket nothing answered it, and `src.workflow.registrations` refused
every `SECRET_REFERENCE` connection at run time and said so.

**One implementation, injectable like every other seam.** `EncryptedDbSecretStore`
is the "one obvious implementation for this deployment" ADR-0045 asks for: a
Postgres table (`SecretStoreEntry`), one row per key, holding AES-256-GCM
ciphertext rather than the value. A dump of that table alone decrypts nothing
— the key that opens it, `settings.secret_store_key`, is an environment
variable and never touches a row. That is what makes this the *one* mechanism
ADR-0045 chose over a second, encrypted-column path for BYODB: both callers
end up here, both get the same rotation story and the same masking rule.

**Not a second credential path with weaker guarantees.** `rotate` changes a
value in place — `src.workflow.registrations` never has to delete and
re-register a connection for its secret to change, which is the whole of what
"rotate without touching the connection" means.

**The platform's only store** (DS-20 / ADR-0059 §9). Every credential the
platform holds — BYODB, a registered tool, a Hệ CSDL's DSN — is read
through `SecretStore`. Vault is no longer a service the
platform runs; `src.services.vault_secret_store` remains only as the exported
package's `secrets.backend: vault` (DS-13), for a company that has its own.
"""

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.base import get_db
from src.db.models import SecretStoreEntry

#: AES-GCM's standard nonce width. 12 bytes, prepended to every ciphertext this
#: module writes so decryption needs nothing from outside the row itself but
#: the key.
_NONCE_SIZE = 12


class SecretNotFoundError(LookupError):
    """`key` names no secret this store holds — ADR-0045's Consequences.

    Distinct from the dead branch this ticket replaces: the old
    `ToolUnavailableError` meant "there is no store to ask". This means the
    store was asked and it does not have `key`, which is a creator or an
    operator's mistake, not a platform gap, and `str(self)` is only ever the
    key — never a value, because there is none to be wrong about.
    """


class SecretUnreadableError(RuntimeError):
    """`key` names a row, but it does not decrypt under today's `SECRET_STORE_KEY`.

    A third failure, distinct from `SecretNotFoundError`: the row *exists*, so
    this is not "wrong reference" — it is "the platform's own envelope key
    changed since this row was written" (a `SECRET_STORE_KEY` rotation done
    without re-encrypting what was already stored), an operator problem rather
    than a creator's typo. Collapsing it into `SecretNotFoundError` would tell
    a creator to fix a reference that was never wrong.
    """


class SecretStore(Protocol):
    """Where a secret reference actually resolves to a value.

    Four operations because that is everything a caller does with a secret
    across its life: read it to use it, write it once to create it, replace it
    without disturbing whatever names it (`rotate`), and remove it. Nothing
    here lists keys or their values — a method that could that would be a
    second response path out of the store, which is the thing ADR-0045 says
    must not exist.
    """

    def get(self, key: str) -> str:
        """The plaintext value `key` names.

        Raises `SecretNotFoundError` if `key` was never `put`, or
        `SecretUnreadableError` if it was but no longer decrypts under this
        deployment's current key — two different failures, ADR-0045's
        Consequences reason.
        """
        ...

    def put(self, key: str, value: str) -> None:
        """Create `key`, or overwrite it if it already exists."""
        ...

    def delete(self, key: str) -> None:
        """Remove `key`. A no-op if it was not there — deleting twice is not an error."""
        ...

    def rotate(self, key: str, value: str) -> None:
        """Replace the value `key` already names. Raises `SecretNotFoundError` if there is none.

        The distinction from `put` is deliberate: `put` is how a key is born,
        `rotate` is how its value changes without anything downstream — a
        `ToolRegistration` row, one day an `external_connections` row —
        needing to be touched. Refusing to rotate a key nobody created yet
        catches the operator who meant `put`.
        """
        ...


def _aes_key() -> bytes:
    """32 bytes for AES-256, derived from `settings.secret_store_key`.

    `SHA-256` rather than requiring the operator to hand-produce 32 raw bytes:
    the setting is a `min_length=32` string, the same shape `jwt_secret`
    already asks operators for, and a hash absorbs it into exactly the width
    AES-256 needs whatever its length beyond the floor.
    """
    return hashlib.sha256(get_settings().secret_store_key.encode("utf-8")).digest()


def _encrypt(value: str) -> bytes:
    """`value` as `nonce || ciphertext` — the only form this module ever writes to a row."""
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = AESGCM(_aes_key()).encrypt(nonce, value.encode("utf-8"), None)
    return nonce + ciphertext


def _decrypt(blob: bytes) -> str:
    """The inverse of `_encrypt`. Raises `InvalidTag` (uncaught) if `blob` was not written under today's key.

    Left to propagate rather than caught here: this function does not know
    which `key` the row belongs to, and `EncryptedDbSecretStore.get` is what
    turns `InvalidTag` into a `SecretUnreadableError` naming it.
    """
    nonce, ciphertext = blob[:_NONCE_SIZE], blob[_NONCE_SIZE:]
    return AESGCM(_aes_key()).decrypt(nonce, ciphertext, None).decode("utf-8")


@dataclass(frozen=True, slots=True)
class EncryptedDbSecretStore:
    """`SecretStore` over `secret_store_entries` — the store's one implementation for this deployment.

    Holds only a `Session`, `QdrantKnowledgeStore`'s reason: every method
    opens no state of its own, so a caller building this around a short-lived
    session (`registered_tool`'s pattern) never risks a plaintext value
    outliving the call that fetched it.
    """

    db: Session

    def get(self, key: str) -> str:
        row = self.db.get(SecretStoreEntry, key)
        if row is None:
            raise SecretNotFoundError(key)
        try:
            return _decrypt(row.ciphertext)
        except InvalidTag:
            raise SecretUnreadableError(key) from None

    def put(self, key: str, value: str) -> None:
        row = self.db.get(SecretStoreEntry, key)
        if row is None:
            self.db.add(SecretStoreEntry(key=key, ciphertext=_encrypt(value)))
        else:
            row.ciphertext = _encrypt(value)
        self.db.commit()

    def delete(self, key: str) -> None:
        row = self.db.get(SecretStoreEntry, key)
        if row is not None:
            self.db.delete(row)
            self.db.commit()

    def rotate(self, key: str, value: str) -> None:
        row = self.db.get(SecretStoreEntry, key)
        if row is None:
            raise SecretNotFoundError(key)
        row.ciphertext = _encrypt(value)
        row.rotated_at = datetime.now(UTC).replace(tzinfo=None)
        self.db.commit()


def get_secret_store(db: Annotated[Session, Depends(get_db)]) -> SecretStore:
    """FastAPI dependency: the one place a request builds the real store.

    Declared the way `get_knowledge_store` is, for the same reason: a test
    aims an admin route's secret management at a fake by overriding this, and
    the production wiring is the default rather than a branch on a flag.
    """
    return EncryptedDbSecretStore(db=db)
