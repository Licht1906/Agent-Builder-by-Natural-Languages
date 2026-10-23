"""`UserConnection` — whose Google account a run borrows, and how it stays fresh.

The missing half ADR-0023 §8 named: the built-in Gmail/Drive connections are
`CredentialKind.OAUTH_PER_USER`, and until now their invoker refused with "cần
một UserConnection". This module is that piece, in three movements:

1. **Connect** (`start_google_connect` / `finish_google_connect`) — the browser
   round-trip. State is a signed, five-minute JWT carrying who started the flow
   and the PKCE verifier, so the callback needs no server-side session and a
   forged callback fails signature verification rather than a lookup.
2. **Spend** (`google_access_token`) — what a run calls, mid-flight, to get a
   live access token for an agent owner. Access tokens are cached in-process
   until shortly before expiry; refresh tokens live only in the secret store.
3. **Break** (`disconnect_google`) — revoke at Google best-effort, forget the
   secret, drop the row.

**Whose account?** The *agent owner's* — the same decision the platform's other
architecture settled on 2026-08-14: an agent acts with the credential of the
person who built it, never a shared service account. `google_access_token`
therefore takes a `user_id` that callers resolve from `agents.owner_id`.
"""

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.tokens import ALGORITHM
from src.config import get_settings
from src.db.base import SessionLocal
from src.db.models import User, UserConnection, UserConnectionStatus
from src.services import google_oauth
from src.services.google_oauth import GoogleOAuthError
from src.services.secret_store import EncryptedDbSecretStore, SecretNotFoundError, SecretUnreadableError
from src.workflow.faults import (
    EXPIRED_PASTE_MESSAGE,
    MISSING_REFRESH_MESSAGE,
    NOT_CONNECTED_MESSAGE,
    UNDECRYPTABLE_REFRESH_MESSAGE,
    UNREADABLE_PASTE_MESSAGE,
    PlatformFault,
)

GOOGLE = "google"

#: The two values `UserConnection.token_kind` takes — see the column's own note
#: for why the difference is recorded rather than guessed.
REFRESH_TOKEN = "refresh_token"
ACCESS_TOKEN = "access_token"  # noqa: S105 — a discriminator, not a secret

#: Where a Google refresh token lives in the secret store, per person. The
#: prefix keeps these keys out of any namespace an admin PUT ever writes.
_SECRET_REF = "user-connection/google/{user_id}"

#: How long the browser has between "Kết nối" and Google's redirect back.
_STATE_TTL = timedelta(minutes=5)

#: Refresh this many seconds before Google says the access token dies, so a
#: token handed to a run cannot expire in the middle of the call it was for.
_EXPIRY_MARGIN_SECONDS = 120


class UserConnectionError(RuntimeError):
    """The connection cannot be used, in words the person who owns it can act on.

    Every instance is a **platform fault** (ADR-0061): the run reached a tool
    holding no usable authority, so whatever transcript came out is about this
    platform's credential state and not about the agent. The attribute is what
    `src.workflow.faults.fault_of_exception` reads through the `raise … from …`
    chain, including where `google_tools` re-raises this as a `GoogleToolError`.
    """

    platform_fault = PlatformFault.CREDENTIAL


@dataclass
class _CachedToken:
    access_token: str
    good_until: float  # time.monotonic() deadline


#: Access tokens by user id. In-process on purpose: an access token is an
#: hour-long convenience, and putting it in the database would store the one
#: thing this design keeps deliberately unstored.
_token_cache: dict[int, _CachedToken] = {}
_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Connect
# --------------------------------------------------------------------------- #


def start_google_connect(user: User) -> str:
    """The URL to send this person's browser to. Minting it is stateless —
    everything the callback will need rides inside the signed `state`."""
    verifier = google_oauth.mint_verifier()
    state = jwt.encode(
        {
            "purpose": "google-oauth-state",
            "user_id": user.id,
            "verifier": verifier,
            "exp": datetime.now(UTC) + _STATE_TTL,
        },
        get_settings().jwt_secret,
        algorithm=ALGORITHM,
    )
    return google_oauth.authorization_url(state=state, verifier=verifier)


def finish_google_connect(db: Session, *, state: str, code: str) -> UserConnection:
    """The callback's whole job: verify state, trade the code, store the tokens.

    Upserts on `(user_id, 'google')` — reconnecting replaces the credential
    rather than accumulating accounts, which is also what un-wedges a row stuck
    in `NEEDS_RECONNECT`.
    """
    try:
        claims = jwt.decode(state, get_settings().jwt_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise UserConnectionError(
            "Phiên kết nối Google không hợp lệ hoặc đã quá hạn — bạn mở lại màn hình Connections và bấm Kết nối lần nữa"
        ) from exc
    if claims.get("purpose") != "google-oauth-state":
        raise UserConnectionError("State không phải của luồng kết nối Google — không xử lý")
    user_id = int(claims["user_id"])

    reply = google_oauth.exchange_code(code, verifier=str(claims["verifier"]))
    if not reply.refresh_token:
        # `prompt=consent access_type=offline` makes this near-impossible, but a
        # connection stored without a refresh token would die within the hour and
        # look like a platform bug when it did.
        raise UserConnectionError("Google không cấp refresh token — thử kết nối lại và chấp thuận toàn bộ quyền")

    secret_ref = _SECRET_REF.format(user_id=user_id)
    EncryptedDbSecretStore(db=db).put(secret_ref, reply.refresh_token)

    email = google_oauth.account_email(reply.access_token)
    row = db.execute(
        select(UserConnection).where(UserConnection.user_id == user_id, UserConnection.provider == GOOGLE)
    ).scalar_one_or_none()
    if row is None:
        row = UserConnection(user_id=user_id, provider=GOOGLE, secret_ref=secret_ref)
        db.add(row)
    row.secret_ref = secret_ref
    row.account_email = email
    row.scopes = reply.scope.split() if reply.scope else list(google_oauth.GOOGLE_SCOPES)
    row.token_kind = REFRESH_TOKEN
    row.expires_at = None  # minted on demand from now on — nothing here expires
    row.status = UserConnectionStatus.CONNECTED
    db.commit()
    db.refresh(row)

    with _cache_lock:
        _token_cache[user_id] = _CachedToken(reply.access_token, _deadline(reply.expires_in))
    return row


def connect_google_with_token(db: Session, user: User, access_token: str) -> UserConnection:
    """Store a pasted Google access token — Agent Builder's own credential path.

    Offered beside `start_google_connect` rather than instead of it, and the two
    are not equivalent: an access token expires within the hour and there is
    nothing here to refresh it from, so this connection will stop working and
    the person will have to paste again. That is the deal Agent Builder makes
    and some people want it — a service account minted elsewhere, a token from a
    script, an account this platform must not hold a refresh token for.

    The token is validated against Google's `tokeninfo` endpoint, which answers
    the two questions that decide whether storing it is worth anything: is it
    alive, and does it carry a scope this platform can use. Everything else
    matches the sign-in path exactly — same row, same secret store, same status.

    **The email is a label, not the verdict.** An earlier version used
    `account_email` as the validator and refused whatever it could not name —
    which meant a token minted for `gmail.send` alone (no `userinfo.email`
    scope) was rejected with a sentence blaming the token, as were a userinfo
    outage and a network blip. `account_email` says so about itself in its own
    docstring; it was the caller that read it wrong. Now a token Google
    confirms is stored even when its account cannot be named, and the screen
    shows the scopes instead.
    """
    check = google_oauth.check_access_token(access_token)
    if not check.ok:
        raise UserConnectionError(check.reason)
    token = access_token.strip()
    # Best-effort, exactly as advertised: a token without `userinfo.email` is
    # perfectly usable and simply has no name to show.
    email = google_oauth.account_email(token)

    secret_ref = _SECRET_REF.format(user_id=user.id)
    EncryptedDbSecretStore(db=db).put(secret_ref, token)
    row = google_connection_of(db, user.id)
    if row is None:
        row = UserConnection(user_id=user.id, provider=GOOGLE, secret_ref=secret_ref)
        db.add(row)
    lifetime = check.expires_in or 3600
    row.secret_ref = secret_ref
    row.account_email = email
    row.token_kind = ACCESS_TOKEN
    row.scopes = list(check.scopes)
    # The row remembers when its token dies, so every screen and every run can
    # say "hết hạn" instead of discovering it from a 401.
    row.expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=lifetime)
    row.status = UserConnectionStatus.CONNECTED
    db.commit()
    db.refresh(row)

    with _cache_lock:
        # Google's own `expires_in` when it gave one, rather than a flat hour.
        # The guess was harmless but it was still a guess, and `tokeninfo`
        # already told us the truth on the way in.
        _token_cache[user.id] = _CachedToken(token, _deadline(lifetime))
    return row


def google_connection_of(db: Session, user_id: int) -> UserConnection | None:
    """This person's Google link, or `None` — what the Connections screen shows."""
    return db.execute(
        select(UserConnection).where(UserConnection.user_id == user_id, UserConnection.provider == GOOGLE)
    ).scalar_one_or_none()


def disconnect_google(db: Session, user: User) -> None:
    """Forget the link: revoke at Google (best-effort), delete the secret, drop the row."""
    row = google_connection_of(db, user.id)
    if row is None:
        return
    store = EncryptedDbSecretStore(db=db)
    try:
        google_oauth.revoke(store.get(row.secret_ref))
    except (SecretNotFoundError, SecretUnreadableError):
        pass  # nothing to revoke — the disconnect below is the part that matters
    store.delete(row.secret_ref)
    db.delete(row)
    db.commit()
    with _cache_lock:
        _token_cache.pop(user.id, None)


# --------------------------------------------------------------------------- #
# Spend
# --------------------------------------------------------------------------- #


def google_access_token(user_id: int) -> str:
    """A live access token for `user_id`'s Google account, refreshed as needed.

    Called from tool invokers on whatever thread the runtime scheduled, long
    after any request ended — so it opens its own session, `_resolved_secret`'s
    precedent. Raises `UserConnectionError` with the person-facing reason when
    the account was never connected or needs reconnecting; the runtime reports
    that sentence on the run, which is exactly where the owner will read it.
    """
    with _cache_lock:
        cached = _token_cache.get(user_id)
        if cached and time.monotonic() < cached.good_until:
            return cached.access_token

    with SessionLocal() as session:
        row = google_connection_of(session, user_id)
        if row is None:
            raise UserConnectionError(NOT_CONNECTED_MESSAGE)
        if row.token_kind == ACCESS_TOKEN:
            # Pasted rather than minted: hand it over as it is — unless the row
            # already knows it is dead. There is no refresh token here to
            # recover with, so the only useful thing to do with an expired
            # paste is to say so, in the words the Connections screen uses,
            # and flip the row so that screen says it too.
            if pasted_token_expired(row):
                if row.status is not UserConnectionStatus.NEEDS_RECONNECT:
                    row.status = UserConnectionStatus.NEEDS_RECONNECT
                    session.commit()
                raise UserConnectionError(EXPIRED_PASTE_MESSAGE)
            try:
                pasted = EncryptedDbSecretStore(db=session).get(row.secret_ref)
            except (SecretNotFoundError, SecretUnreadableError) as exc:
                raise UserConnectionError(UNREADABLE_PASTE_MESSAGE) from exc
            remaining = _seconds_left(row) if row.expires_at else 3600
            with _cache_lock:
                _token_cache[user_id] = _CachedToken(pasted, _deadline(remaining))
            return pasted

        try:
            refresh_token = EncryptedDbSecretStore(db=session).get(row.secret_ref)
        except SecretNotFoundError as exc:
            raise UserConnectionError(MISSING_REFRESH_MESSAGE) from exc
        except SecretUnreadableError as exc:
            raise UserConnectionError(UNDECRYPTABLE_REFRESH_MESSAGE) from exc

        try:
            reply = google_oauth.refresh_access_token(refresh_token)
        except GoogleOAuthError as exc:
            if "kết nối lại" in str(exc):
                row.status = UserConnectionStatus.NEEDS_RECONNECT
                session.commit()
            raise UserConnectionError(str(exc)) from exc

        if reply.refresh_token and reply.refresh_token != refresh_token:
            # Google rotated it. The new one is the only one that works from now
            # on, so losing this write would strand the connection.
            EncryptedDbSecretStore(db=session).put(row.secret_ref, reply.refresh_token)
        if row.status is not UserConnectionStatus.CONNECTED:
            row.status = UserConnectionStatus.CONNECTED
        session.commit()

    with _cache_lock:
        _token_cache[user_id] = _CachedToken(reply.access_token, _deadline(reply.expires_in))
    return reply.access_token


# `EXPIRED_PASTE_MESSAGE` and its four siblings now live in
# `src.workflow.faults` and are imported above. They moved because ADR-0061
# needs one authority for each sentence: a transcript written before
# `SandboxTranscriptEntry.fault` existed is re-read by matching these strings,
# and a second spelling of any of them would be a fault the reader misses. The
# name is re-exported from here unchanged, so every existing importer — the
# Connections screen's copy included — keeps working.


def pasted_token_expired(row: UserConnection) -> bool:
    """True when `row` is a paste whose recorded lifetime has run out.

    A paste without `expires_at` (stored before the column existed) is not
    called expired here — nothing is known about it, and saying "hết hạn" about
    a token that might still work would be the same guess in the other direction.
    Google decides that one, with a 401 `mark_needs_reconnect` then records.
    """
    if row.token_kind != ACCESS_TOKEN or row.expires_at is None:
        return False
    return _seconds_left(row) <= 0


def _seconds_left(row: UserConnection) -> int:
    assert row.expires_at is not None
    return int((row.expires_at - datetime.now(UTC).replace(tzinfo=None)).total_seconds())


def mark_needs_reconnect(user_id: int) -> None:
    """Google refused this person's token mid-run: say so on the row, forget the cache.

    Called from the Gmail/Drive invokers on a 401. Until now that 401 was
    reported on the run and nowhere else — the row stayed `CONNECTED`, the
    screens kept saying "đang nối", and the next run hit the same wall. After
    this the Connections screen and the MCP panel both show "cần kết nối lại",
    which is where the person who can fix it will be looking.
    """
    with _cache_lock:
        _token_cache.pop(user_id, None)
    with SessionLocal() as session:
        row = google_connection_of(session, user_id)
        if row is None or row.status is UserConnectionStatus.NEEDS_RECONNECT:
            return
        row.status = UserConnectionStatus.NEEDS_RECONNECT
        session.commit()


def _deadline(expires_in: int) -> float:
    lifetime = max(expires_in - _EXPIRY_MARGIN_SECONDS, 60)
    return time.monotonic() + lifetime
