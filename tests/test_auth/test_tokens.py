"""UC1-01 (#117), FR-04: the JWT carries `sub`, `employee_id`, `roles`, `department`; an expired one is refused."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from src.auth.tokens import ALGORITHM, InvalidTokenError, create_access_token, decode_access_token
from src.config import get_settings
from src.db.models import User, UserRole, UserStatus


def _user(**overrides: object) -> User:
    defaults: dict[str, object] = {
        "id": 42,
        "email": "e102@example.com",
        "password_hash": "irrelevant-here",
        "role": UserRole.CREATOR,
        "status": UserStatus.ACTIVE,
        "employee_id": "E102",
        "job_title": "Nhân viên kinh doanh",
        "department": "SALES",
    }
    return User(**{**defaults, **overrides})


def test_decoded_token_carries_all_four_fr04_claims() -> None:
    token = create_access_token(_user())
    settings = get_settings()

    payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])

    assert payload["sub"] == "42"
    assert payload["employee_id"] == "E102"
    assert payload["roles"] == ["creator"]
    assert payload["department"] == "SALES"


def test_decode_access_token_exposes_employee_id_and_department() -> None:
    claims = decode_access_token(create_access_token(_user()))

    assert claims.user_id == 42
    assert claims.role is UserRole.CREATOR
    assert claims.employee_id == "E102"
    assert claims.department == "SALES"


def test_an_expired_token_is_rejected() -> None:
    settings = get_settings()
    issued_at = datetime.now(UTC) - timedelta(hours=2)
    payload = {
        "sub": "42",
        "role": UserRole.CREATOR.value,
        "roles": [UserRole.CREATOR.value],
        "employee_id": "E102",
        "department": "SALES",
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=1),  # expired long before "now"
    }
    expired_token = jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)

    with pytest.raises(InvalidTokenError):
        decode_access_token(expired_token)


def test_a_token_signed_with_the_wrong_secret_is_rejected() -> None:
    forged = jwt.encode(
        {
            "sub": "42",
            "role": UserRole.CREATOR.value,
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        "not-the-real-secret-not-the-real-secret",
        algorithm=ALGORITHM,
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(forged)
