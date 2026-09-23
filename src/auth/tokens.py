"""Signing and reading the access token.

The token carries the role so a frontend can render the right screens without a
second round trip. It is **not** what authorisation reads: `dependencies.py`
takes the role from the database row, so a role changed after a token was issued
takes effect immediately instead of at token expiry.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from src.config import get_settings
from src.db.models import User, UserRole

#: Not a setting: ADR-0002 fixes the algorithm, and an algorithm a deployment can
#: change is an algorithm an attacker can try to talk it into changing.
ALGORITHM = "HS256"


class InvalidTokenError(Exception):
    """The token is missing, malformed, expired, or not signed by us."""


@dataclass(frozen=True)
class TokenClaims:
    """What a verified token asserts.

    `employee_id`/`department` are UC1-01 (#117), FR-04's other two required
    claims, alongside `sub` and `role`/`roles` — §9.2 reads `employee_id` and
    `department` straight off the caller's token to set the RLS GUCs, so a
    caller that decoded a token but couldn't get at either claim would have
    nothing to set them from.
    """

    user_id: int
    role: UserRole
    employee_id: str | None
    department: str | None


def create_access_token(user: User) -> str:
    """Sign a token identifying `user`, valid for `settings.jwt_expire_minutes`.

    FR-04: `sub`, `employee_id`, `roles`, `department`. `roles` is a list
    for the wire contract even though a `User` carries exactly one role
    today — the claim name is plural because a caller reading it (§9.2's
    runtime, a future multi-role account) should not have to special-case
    "one role" as a different shape from "several".  `role` (singular) stays
    alongside it: `decode_access_token`'s `TokenClaims.role` and every
    existing dependency read that one, and duplicating the same value under
    two keys is cheaper than deciding today which of two call sites should
    change.
    """
    settings = get_settings()
    issued_at = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "role": user.role.value,
        "roles": [user.role.value],
        "employee_id": user.employee_id,
        "department": user.department,
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> TokenClaims:
    """Verify `token` and return its claims. Raises `InvalidTokenError` for anything off."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
        return TokenClaims(
            user_id=int(payload["sub"]),
            role=UserRole(payload["role"]),
            employee_id=payload.get("employee_id"),
            department=payload.get("department"),
        )
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise InvalidTokenError(str(exc)) from exc
