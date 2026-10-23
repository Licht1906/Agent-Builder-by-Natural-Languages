"""`/user-connections` — mỗi người nối tài khoản Google của chính mình.

The browser-facing half of `src.workflow.user_connections`. Three doors and a
callback:

- `GET    /user-connections`                 — what this person has connected
- `POST   /user-connections/google/start`    — mint the consent URL to open
- `GET    /user-connections/google/callback` — where Google sends the browser back
- `DELETE /user-connections/google`          — disconnect and revoke

The callback is deliberately **unauthenticated**: it arrives as a top-level
browser navigation from `accounts.google.com`, which carries no Authorization
header — who it belongs to is inside the signed `state` the `start` endpoint
minted, verified by `finish_google_connect`. It answers with a redirect to the
frontend either way, because the person standing there is mid-flow in a browser
tab, not a JSON client.

Like `connection_routes`, nothing the meta-agent's prompt touches imports this
module — tokens go browser → Google → here → secret store, never through a
model's context window.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.auth.dependencies import CurrentUser
from src.config import get_settings
from src.db.base import get_db
from src.db.models import UserConnection, UserConnectionStatus
from src.models.schemas import UtcTimestamp
from src.services.google_oauth import GoogleOAuthError
from src.workflow.user_connections import (
    UserConnectionError,
    connect_google_with_token,
    disconnect_google,
    finish_google_connect,
    google_connection_of,
    pasted_token_expired,
    start_google_connect,
)

router = APIRouter(prefix="/user-connections", tags=["user-connections"])


class UserConnectionResponse(BaseModel):
    """One provider's link as the Connections screen shows it."""

    provider: str
    connected: bool
    account_email: str = ""
    status: str = ""
    scopes: list[str] = []
    #: `refresh_token` (signed in — stays alive) or `access_token` (pasted —
    #: dies within the hour). The screen says which, because the two look
    #: identical once connected and only one of them will still work tomorrow.
    token_kind: str = ""
    #: When a pasted token dies (UTC); `None` for the sign-in path. The screen
    #: turns it into "còn N phút" / "đã hết hạn" next to the paste box.
    expires_at: UtcTimestamp | None = None


def _response_of(row: UserConnection) -> UserConnectionResponse:
    return UserConnectionResponse(
        provider=row.provider,
        connected=True,
        account_email=row.account_email,
        status=row.status.value,
        scopes=list(row.scopes),
        token_kind=row.token_kind,
        expires_at=row.expires_at,
    )


class PasteTokenRequest(BaseModel):
    """A Google access token pasted by hand — Agent Builder's own path.

    The token itself, not a reference: this is the one body on the platform
    that carries a live credential, and it goes straight to the secret store
    without being written to a row or a log.
    """

    access_token: str = Field(..., min_length=1, max_length=4000, description="Access token dán vào")


class ConnectStartResponse(BaseModel):
    """Where the frontend should send the browser."""

    url: str


@router.get("", response_model=list[UserConnectionResponse])
def list_user_connections(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> list[UserConnectionResponse]:
    """Every provider this platform can link, with this person's state on each.

    Always the full provider list rather than only the connected rows, so the
    screen can offer "Kết nối" for what is missing without hard-coding the
    provider set a second time in the frontend.
    """
    row = google_connection_of(db, user.id)
    if row is None:
        return [UserConnectionResponse(provider="google", connected=False)]
    if pasted_token_expired(row) and row.status is not UserConnectionStatus.NEEDS_RECONNECT:
        # The screen is the first place a person looks after the hour is up —
        # flip the row here, so it says "cần kết nối lại" before any run has to.
        row.status = UserConnectionStatus.NEEDS_RECONNECT
        db.commit()
        db.refresh(row)
    return [_response_of(row)]


@router.post("/google/start", response_model=ConnectStartResponse)
def start_google(user: CurrentUser) -> ConnectStartResponse:
    """Mint the Google consent URL for this person. The frontend opens it in the
    same tab; the callback below brings them home."""
    try:
        return ConnectStartResponse(url=start_google_connect(user))
    except GoogleOAuthError as exc:
        # Configuration, not user error — but the person clicking is the one who
        # needs to know why nothing happened.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/google/token", response_model=UserConnectionResponse)
def connect_google_by_token(
    payload: PasteTokenRequest, user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> UserConnectionResponse:
    """Connect Google by pasting an access token, beside the sign-in button.

    422 rather than 400 when Google refuses the token: the request is about a
    credential the caller supplied, and "this token does not work" is a fact
    about the body. The message says both ways out — check the token, or use
    the sign-in button, which is the one that keeps working tomorrow.
    """
    try:
        row = connect_google_with_token(db, user, payload.access_token)
    except UserConnectionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return _response_of(row)


@router.get("/google/callback")
def google_callback(
    db: Annotated[Session, Depends(get_db)],
    state: str = "",
    code: str = "",
    error: str = "",
) -> RedirectResponse:
    """Google's redirect back. Stores the tokens, then sends the browser to the
    Connections screen with the outcome in the query string — the flow's whole
    result readable where the person actually is."""
    home = get_settings().frontend_url.rstrip("/")
    if error or not code or not state:
        return RedirectResponse(f"{home}/?google=denied")
    try:
        finish_google_connect(db, state=state, code=code)
    except (UserConnectionError, GoogleOAuthError):
        return RedirectResponse(f"{home}/?google=failed")
    return RedirectResponse(f"{home}/?google=connected")


@router.delete("/google", status_code=204)
def delete_google(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> None:
    """Disconnect: revoke at Google best-effort, forget the secret, drop the row."""
    disconnect_google(db, user)
