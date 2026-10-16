"""The Deployment's API key — the credential on the API channel (#50, ADR-0029 §5).

One key per Deployment, not one per caller or per integration: the requirement
doc's MVP framing asks for a working API door, not key management, and
regenerating gives the owner a revoke path without a second table.

**SHA-256 and not bcrypt** (ADR-0036 §4), which is the one decision in this file
worth arguing.
`src.auth.passwords` hashes *passwords* — short, low-entropy, chosen by people —
and pays a deliberate cost factor so guessing them is slow. An API key is 32
bytes from `secrets`, so there is nothing to guess and nothing to slow down; what
there is, is a hash computed on **every API call**, where bcrypt's cost would be
paid by the caller on each request. The unsalted digest is also what lets the
stored form be compared directly rather than re-derived per candidate.

The plaintext exists in exactly one place: the response that minted it. Nothing
stores it, so "show me my key again" has no answer other than making a new one —
which is the same button as revoking the old one, and deliberately so.
"""

import hashlib
import hmac
import secrets

#: Marks a string as this platform's deployment key. A key that turns up in a log
#: or a config file is then recognisable — and therefore revocable — without
#: anyone having to work out what system issued it.
API_KEY_PREFIX = "dpl_"

#: 32 bytes of `secrets` entropy, which is well past anything brute-forceable and
#: short enough to paste into an environment variable.
_KEY_BYTES = 32


def generate_api_key() -> str:
    """A fresh key, in plaintext. The only moment this value ever exists."""
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(_KEY_BYTES)}"


def hash_api_key(api_key: str) -> str:
    """What `Deployment.api_key_hash` stores for `api_key`."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_api_key(api_key: str, api_key_hash: str) -> bool:
    """Whether `api_key` is the key behind `api_key_hash`.

    `compare_digest` rather than `==`: the comparison runs on every API call, and
    an early-exit comparison of a secret is the textbook shape of a timing oracle.

    An empty key is answered by the same comparison rather than short-circuited —
    a missing header is simply a key that is not the right one.
    """
    return hmac.compare_digest(hash_api_key(api_key), api_key_hash)
