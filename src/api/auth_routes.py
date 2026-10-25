"""Auth endpoints: sign up, log in, and read who you are."""

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.dependencies import AdminUser, CurrentUser
from src.auth.ldap import (
    InvalidCredentialsError,
    LdapAuthenticator,
    PendingAccountError,
    authenticate,
    get_ldap_authenticator,
)
from src.auth.passwords import local_password_matches
from src.auth.registration import (
    EmailTakenError,
    EmployeeIdTakenError,
    InvalidRegistrationError,
    Registration,
    normalised_email,
    register_user,
)
from src.auth.tokens import create_access_token
from src.db.base import get_db
from src.db.models import User, UserStatus
from src.models.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])

#: One message for every way a login can fail on the credentials themselves.
#: Naming which half was wrong would turn this endpoint into a way to
#: discover who has an account here.
_INVALID_CREDENTIALS = "Email hoặc mật khẩu không đúng"

_PENDING_ACCOUNT = "Tài khoản đang chờ kích hoạt — liên hệ agent_admin"


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Annotated[Session, Depends(get_db)]) -> User:
    """Create an account. UC1-01 (#117), FR-01/FR-03 — spec.md §3, §4.1.

    Unlike the old V2 flow, signing up does **not** log you in: the account
    this creates is `pending` unless it happens to be the installation's very
    first one (`src.auth.registration.register_user` decides which), and a
    token for an account that cannot do anything yet is not a session worth
    handing out. `POST /login` is the only door, and it refuses a `pending`
    account with a distinct, honest status rather than pretending the account
    doesn't exist.

    Two refusals, two statuses, because the next move differs. A taken
    address (or `employee_id`, if the caller supplied one) is a 409 — somebody
    or something already claimed it, and the answer is to pick another one,
    not to fix a typo. Everything else — a malformed address, a password
    outside FR-02's bounds, a blank `job_title`/`department` — is a 422.

    The 409 does tell an anonymous caller that an address is registered here,
    which `POST /login` deliberately never does. A signup form cannot silently
    do nothing, so the choice is between saying so and pretending to create an
    account that already exists — and the second leaves the real owner's
    account looking compromised to whoever probed it.
    """
    try:
        user = register_user(
            db,
            Registration(
                email=payload.email,
                password=payload.password,
                role=payload.role,
                job_title=payload.job_title,
                department=payload.department,
                employee_id=payload.employee_id,
            ),
        )
    except (EmailTakenError, EmployeeIdTakenError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except InvalidRegistrationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return user


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Annotated[Session, Depends(get_db)]) -> TokenResponse:
    """Exchange email and password for an access token carrying the caller's role and claims (FR-04).

    The address is normalised the same way registration normalises it, so an
    account created as `Anna@Example.com` is one somebody can sign into by typing
    it back however they happen to type it.

    The `pending` check runs *after* the password check, on purpose: it is
    only reached once the caller has already proven they hold the right
    password, so it cannot be used to probe an account's status without
    knowing it — the same reasoning that keeps a wrong password and an
    unknown email indistinguishable.
    """
    user = db.execute(select(User).where(User.email == normalised_email(payload.email))).scalar_one_or_none()

    if user is None or not local_password_matches(user, payload.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS,
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.status == UserStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_PENDING_ACCOUNT)

    return TokenResponse(access_token=create_access_token(user), role=user.role)


@router.post("/ldap-bind", response_model=TokenResponse)
def ldap_bind(
    payload: LoginRequest,
    db: Annotated[Session, Depends(get_db)],
    ldap_authenticate: Annotated[LdapAuthenticator, Depends(get_ldap_authenticator)],
) -> TokenResponse:
    """UC1-02 (#118), FR-05 (Should) — spec.md §4.1, §19.

    A second door beside `/login`: authenticates via LDAP/AD when
    `settings.ldap_*` is configured, and falls back to the identical local
    Argon2id check `/login` runs when it is not. `/login` itself is untouched
    by this endpoint's existence — turning LDAP off changes nothing about it.
    """
    try:
        user = authenticate(db, payload.email, payload.password, ldap_authenticate=ldap_authenticate)
    except InvalidCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS,
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except PendingAccountError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_PENDING_ACCOUNT) from None
    return TokenResponse(access_token=create_access_token(user), role=user.role)


@router.get("/me", response_model=UserResponse)
def read_current_user(user: CurrentUser) -> User:
    """Who the caller is, according to their token."""
    return user


@router.get("/users", response_model=list[UserResponse])
def list_users(_: AdminUser, db: Annotated[Session, Depends(get_db)]) -> Sequence[User]:
    """Every account on the platform. Admin only — a creator has no business
    knowing who else builds agents here."""
    return db.execute(select(User).order_by(User.id)).scalars().all()
