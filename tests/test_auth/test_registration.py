"""UC1-01 (#117), FR-01/FR-02: `register_user`'s own decisions, at the service seam.

FR-03's bootstrap account is deliberately *not* this module's concern — it is
seeded by migration `d13a1d1e5b78`, not decided at registration time. See
`src.auth.registration`'s module docstring for why a runtime "am I the first
account" check was tried and reverted, and `tests/test_db/test_auth_bootstrap.py`
for that seeded row proven against a real, fully migrated database.
"""

from collections.abc import Generator
from typing import Any

import pytest
from sqlalchemy import delete, func
from sqlalchemy.orm import Session

from src.auth.registration import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    EmailTakenError,
    EmployeeIdTakenError,
    InvalidRegistrationError,
    Registration,
    register_user,
)
from src.db.models import User, UserRole, UserStatus

_PASSWORD = "a-perfectly-fine-password"


def _registration(**overrides: Any) -> Registration:
    defaults: dict[str, Any] = {
        "email": "newcomer@example.com",
        "password": _PASSWORD,
        "role": UserRole.CREATOR,
        "job_title": "Nhân viên kinh doanh",
        "department": "SALES",
    }
    return Registration(**{**defaults, **overrides})


#: Every address this module registers, and the exact set its teardown removes.
_REGISTERED_EMAILS = [
    "newcomer@example.com",
    "someone-else@example.com",
    "wants-admin@example.com",
]


@pytest.fixture(autouse=True)
def _clean_up_registered_accounts(db_session: Session) -> Generator[None, None, None]:
    yield
    # Compared case-insensitively, not matched by a `%@example.com` wildcard.
    # The lowering keeps what the wildcard's `ilike` was for: a mixed-case
    # address (`test_the_address_is_case_insensitive`'s own
    # "Newcomer@Example.com") stored verbatim would otherwise survive this
    # cleanup and permanently occupy that case-insensitively-unique slot for
    # every test that runs afterwards.
    #
    # What the wildcard added on top of that was reach into accounts this
    # module never created — including ones other modules had already given a
    # `uc1_agent_spec_versions` row to. Deleting one of those raises
    # `ForeignKeyViolation` in teardown, which poisons the shared session and
    # fails every test after it in this file, none of which had anything wrong
    # with them. The list is the point: teardown cleans up after *this* module.
    db_session.execute(delete(User).where(func.lower(User.email).in_(_REGISTERED_EMAILS)))
    db_session.commit()


def test_a_normal_signup_is_always_pending_with_the_requested_role(db_session: Session) -> None:
    """No exceptions, including a request for `agent_admin` itself — see the module docstring."""
    user = register_user(db_session, _registration(role=UserRole.PM))
    assert user.role is UserRole.PM
    assert user.status is UserStatus.PENDING

    admin_request = register_user(
        db_session, _registration(email="wants-admin@example.com", role=UserRole.AGENT_ADMIN)
    )
    assert admin_request.role is UserRole.AGENT_ADMIN
    assert admin_request.status is UserStatus.PENDING


def test_employee_id_defaults_to_e_plus_zero_padded_id(db_session: Session) -> None:
    user = register_user(db_session, _registration())

    assert user.employee_id == f"E{user.id:03d}"


def test_an_explicit_employee_id_is_honoured(db_session: Session) -> None:
    user = register_user(db_session, _registration(employee_id="E999"))

    assert user.employee_id == "E999"


def test_a_duplicate_explicit_employee_id_is_refused(db_session: Session) -> None:
    register_user(db_session, _registration(employee_id="E777"))

    with pytest.raises(EmployeeIdTakenError):
        register_user(db_session, _registration(email="someone-else@example.com", employee_id="E777"))


def test_a_duplicate_email_is_refused(db_session: Session) -> None:
    register_user(db_session, _registration())

    with pytest.raises(EmailTakenError):
        register_user(db_session, _registration())


def test_a_password_below_the_fr02_floor_is_refused(db_session: Session) -> None:
    with pytest.raises(InvalidRegistrationError, match=str(MIN_PASSWORD_LENGTH)):
        register_user(db_session, _registration(password="short7"))


def test_an_overlong_password_is_refused(db_session: Session) -> None:
    with pytest.raises(InvalidRegistrationError, match=str(MAX_PASSWORD_LENGTH)):
        register_user(db_session, _registration(password="x" * (MAX_PASSWORD_LENGTH + 1)))


def test_a_blank_job_title_is_refused(db_session: Session) -> None:
    with pytest.raises(InvalidRegistrationError):
        register_user(db_session, _registration(job_title="   "))


def test_a_blank_department_is_refused(db_session: Session) -> None:
    with pytest.raises(InvalidRegistrationError):
        register_user(db_session, _registration(department=""))


def test_a_malformed_email_is_refused(db_session: Session) -> None:
    with pytest.raises(InvalidRegistrationError):
        register_user(db_session, _registration(email="not-an-address"))
