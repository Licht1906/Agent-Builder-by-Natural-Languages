"""The guards every protected endpoint sits behind.

`get_current_user` answers *who is calling*; `require_admin` answers *may they*.
Both resolve the caller against the database rather than trusting the token's
claims, so revoking or demoting an account takes effect on the next request
instead of whenever an already-issued token happens to expire.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.auth.tokens import InvalidTokenError, decode_access_token
from src.db.base import get_db
from src.db.models import ADMIN_EQUIVALENT_ROLES, PM_CONTROL_ROOM_ROLES, User

#: `auto_error=False` so a missing header reaches us and becomes a 401. FastAPI's
#: own error for that case is a 403, which reads as "you are known but not
#: allowed" — the opposite of what an anonymous request means.
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Cần đăng nhập",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User | None:
    """The account behind the request's bearer token, or `None` if there is no usable one.

    Split out of `get_current_user` by #50. The API channel of a Deployment
    authenticates with a key rather than a session (ADR-0029 §5), so on that one
    door "nobody is signed in" has to be an *answer* the endpoint can act on
    rather than a refusal raised underneath it.

    All three ways of having no session — no header, an unreadable token, a token
    naming an account that no longer exists — collapse to the same `None`. They
    are one fact from the caller's point of view, and every one of them means the
    request has to prove itself some other way or be refused.
    """
    if credentials is None:
        return None
    try:
        claims = decode_access_token(credentials.credentials)
    except InvalidTokenError:
        return None
    # A correctly signed token naming an account that no longer exists is no
    # session at all — resolved against the database, so revoking bites at once.
    return db.get(User, claims.user_id)


def get_current_user(user: Annotated[User | None, Depends(get_optional_user)]) -> User:
    """The account behind the request's bearer token. 401 if there isn't one."""
    if user is None:
        raise _unauthenticated()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]

#: For the one endpoint that has a second way in. Everything else takes
#: `CurrentUser`, so "signed in" stays the default and being optional is a
#: decision a route has to make on purpose.
OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def require_admin(user: CurrentUser) -> User:
    """The caller, if they hold an admin-equivalent role (`ADMIN_EQUIVALENT_ROLES`). 403 otherwise."""
    if user.role not in ADMIN_EQUIVALENT_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chỉ admin mới truy cập được",
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def require_pm(user: CurrentUser) -> User:
    """The caller, if they may open the PM control room (`PM_CONTROL_ROOM_ROLES`). 403 otherwise.

    Wider than `require_admin` by exactly one role (`pm`) and used by exactly
    one router (`src.api.pm_routes`): the PM's levers are theirs by job, the
    admin's remaining doors (accounts, config, dossiers) are not.
    """
    if user.role not in PM_CONTROL_ROOM_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chỉ PM hoặc admin mới vào được phòng điều hành PM",
        )
    return user


PmUser = Annotated[User, Depends(require_pm)]
