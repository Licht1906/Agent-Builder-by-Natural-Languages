"""Password hashing.

UC1-01 (#117), FR-02 requires Argon2id — deliberately not bcrypt, whose 72-byte
truncation and lack of a memory-hardness knob are exactly what Argon2id was
designed to fix. `hash_password` only ever produces an Argon2id hash now.

Bcrypt does not disappear from this module, though: every account this
platform seeded or self-registered before this ticket (`src.auth.seed`'s two
demo accounts, and anyone who signed up under the old V2 flow) holds a bcrypt
hash in `users.password_hash`, and there is no migration that can turn a hash
back into the plaintext it came from to re-hash it. `verify_password`
therefore dispatches on the hash's own prefix — `$argon2` or bcrypt's `$2`ish
range — so both generations of account keep logging in, and every hash
written from today onward is Argon2id. This is the standard "upgrade on
verify" pattern; it does not upgrade a bcrypt hash to Argon2id in place, since
verification never sees the plaintext for long enough to need to.
"""

from typing import TYPE_CHECKING

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

if TYPE_CHECKING:
    from src.db.models import User

#: bcrypt hashes at most 72 bytes and rejects anything longer. Only the legacy
#: verify path needs this — Argon2id has no equivalent hard ceiling.
_BCRYPT_MAX_BYTES = 72

_argon2_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash `password` with Argon2id, fresh salt, default cost parameters."""
    return _argon2_hasher.hash(password)


def _looks_like_argon2(password_hash: str) -> bool:
    return password_hash.startswith("$argon2")


def _verify_bcrypt(password: str, password_hash: str) -> bool:
    """Legacy path: verify against a pre-existing bcrypt hash. Never used to write new ones."""
    encoded = password.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except ValueError:
        # A stored hash bcrypt cannot parse — treat as no match rather than a 500.
        return False


def verify_password(password: str, password_hash: str) -> bool:
    """Whether `password` matches `password_hash`, whichever algorithm produced it."""
    if _looks_like_argon2(password_hash):
        try:
            return _argon2_hasher.verify(password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False
    return _verify_bcrypt(password, password_hash)


#: A real Argon2id hash of a value nobody can log in with. `verify_password` runs
#: against it when an email does not exist, so a missing account costs the same
#: time as a wrong password and login cannot be timed to enumerate accounts.
DUMMY_HASH = hash_password("no-account-with-this-hash")


def local_password_matches(user: "User | None", password: str) -> bool:
    """Whether `password` is `user`'s local password — `False`, safely, if `user` is `None`.

    Shared by `POST /auth/login` and `POST /auth/ldap-bind`'s local fallback
    (`src.auth.ldap.authenticate`), so the one property that makes login safe
    against account enumeration — hashing against `DUMMY_HASH` when the account
    doesn't exist, so a missing account costs the same time as a wrong password —
    is enforced in one place rather than reproduced at every caller.
    """
    password_hash = user.password_hash if user is not None else DUMMY_HASH
    return verify_password(password, password_hash)
