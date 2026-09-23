"""Creating an account — UC1-01 (#117), spec.md §3/§4.1, FR-01..FR-03.

Every self-registered account starts `pending`, whatever role it asked for,
and stays unusable (`POST /auth/login` refuses it — see `src.api.auth_routes`)
until an `agent_admin` calls `POST /admin/users/{id}/activate`. No exceptions,
including a literal first-ever call to `POST /auth/register`: a signup form
that could mint a working `agent_admin`/`sales_manager` session by itself
would be the approval queue with no gate on its own front door.

FR-03's "tài khoản đầu tiên được seed làm agent_admin khi cài đặt" (the first
account is *seeded* as agent_admin at install time) is deliberately not this
module's job — it reads as an install-time fact, not a runtime decision, and
migration `d13a1d1e5b78` seeds that account the same way `1135e617defc`
(issue #4) already seeds the V2 `creator`/`admin` pair. A runtime "is this the
first account" check was tried and reverted: a real install's own migrations
already seed two V2 accounts before anyone calls this endpoint, so the check
could never truthfully fire against a real deployment, and the only role that
mattered — "some admin-equivalent account must already exist to activate
whoever registers" — is exactly what the seeded row is already for.

This supersedes the V2 `ADMIN_SIGNUP_CODE` invite-code mechanism (issue #4):
that scheme answered "how does an admin account get minted" with a shared
secret; this one answers it with "seeded once at install, then only ever
activated forward" — no secret to leak, rotate, or forget to set.

Nothing here writes a password hash. `src.auth.passwords` is still the only
module that touches Argon2id; this one decides whether there is going to be
an account, and if so, what it starts out able to do.
"""

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.auth.passwords import hash_password
from src.db.models import User, UserRole, UserStatus

#: Deliberately permissive. This is a shape check, not a proof the address
#: exists — only sending mail to it would be that, and refusing an unusual but
#: legitimate address is a worse failure than accepting an unreachable one.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: FR-02: "mật khẩu ≥ 12 ký tự" — the floor is a policy decision, stated once.
MIN_PASSWORD_LENGTH = 12

#: An outer bound with no security meaning of its own (Argon2id has no
#: bcrypt-style byte ceiling) — purely so a request cannot make this module
#: hash megabytes of text.
MAX_PASSWORD_LENGTH = 256


class RegistrationError(Exception):
    """A signup that cannot go ahead, carrying the sentence the person will read.

    One base class with subclasses, because the route answers each with a
    different status and a caller catching "email taken" must not also catch
    "employee id taken".
    """


class InvalidRegistrationError(RegistrationError):
    """The details themselves are wrong — a malformed address, too short a password, a blank field."""


class EmailTakenError(RegistrationError):
    """Somebody already registered this address."""


class EmployeeIdTakenError(RegistrationError):
    """The caller-supplied `employee_id` already names a different account."""


@dataclass(frozen=True, slots=True)
class Registration:
    """One signup as the domain sees it — FR-01's four required fields, plus
    the one optional one (`employee_id`) a demo seed script can pin explicitly
    so it lines up with `crm_sales_assignment.sales_rep_id`.
    """

    email: str
    password: str
    role: UserRole
    job_title: str
    department: str
    employee_id: str | None = None


def normalised_email(email: str) -> str:
    """`email` as it will be stored and logged in with.

    Lowercased, because addresses are not case-sensitive in any way a person
    experiences — a platform where `Anna@` and `anna@` are two accounts is a
    platform where somebody cannot log in and cannot see why.
    """
    return email.strip().lower()


def register_user(db: Session, registration: Registration) -> User:
    """Create the account `registration` asks for, `pending`, or refuse and say why."""
    email = normalised_email(registration.email)
    _refuse_bad_details(email, registration)
    _refuse_taken_email(db, email)
    _refuse_taken_employee_id(db, registration.employee_id)

    user = User(
        email=email,
        password_hash=hash_password(registration.password),
        role=registration.role,
        status=UserStatus.PENDING,
        job_title=registration.job_title,
        department=registration.department,
    )
    return _insert(db, user, registration.employee_id)


def _refuse_bad_details(email: str, registration: Registration) -> None:
    """Refuse an address nothing could deliver to, a password outside FR-02's bounds, or a blank required field."""
    if not _EMAIL_PATTERN.match(email):
        raise InvalidRegistrationError("Email không hợp lệ")
    if len(registration.password) < MIN_PASSWORD_LENGTH:
        raise InvalidRegistrationError(f"Mật khẩu phải có ít nhất {MIN_PASSWORD_LENGTH} ký tự")
    if len(registration.password) > MAX_PASSWORD_LENGTH:
        raise InvalidRegistrationError(f"Mật khẩu không được dài quá {MAX_PASSWORD_LENGTH} ký tự")
    if not registration.job_title.strip():
        raise InvalidRegistrationError("Chức vụ không được để trống")
    if not registration.department.strip():
        raise InvalidRegistrationError("Phòng ban không được để trống")


def _refuse_taken_email(db: Session, email: str) -> None:
    if db.execute(select(User.id).where(User.email == email)).scalar_one_or_none() is not None:
        raise EmailTakenError("Email này đã có tài khoản")


def _refuse_taken_employee_id(db: Session, employee_id: str | None) -> None:
    if employee_id is None:
        return
    taken = db.execute(select(User.id).where(User.employee_id == employee_id)).scalar_one_or_none()
    if taken is not None:
        raise EmployeeIdTakenError(f"Mã nhân viên {employee_id!r} đã được dùng")


def _insert(db: Session, user: User, employee_id: str | None) -> User:
    """Add `user`, then commit — two separate `IntegrityError` windows because
    they mean two different things to report: a race on `email` at flush time
    (the pre-checks above already handled the non-racing case), and a race on
    `employee_id`, only knowable once `user.id` exists to default it from.
    """
    db.add(user)
    try:
        # Flushed (not committed) first so `user.id` exists to build the
        # default employee_id from — a caller-supplied one is already known.
        db.flush()
    except IntegrityError:
        db.rollback()
        raise EmailTakenError("Email này đã có tài khoản") from None

    user.employee_id = employee_id or f"E{user.id:03d}"
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise EmployeeIdTakenError(f"Mã nhân viên {user.employee_id!r} đã được dùng") from None
    db.refresh(user)
    return user
