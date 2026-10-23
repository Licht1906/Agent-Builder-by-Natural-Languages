"""Talking OAuth 2.0 to Google — the wire half of `UserConnection`.

This module knows three HTTP conversations and nothing else: building the
consent URL, exchanging an authorization code, and refreshing an access token.
Who the tokens belong to, where they are stored, and what they may be used for
is `src.workflow.user_connections`' business — keeping the wire protocol here
means a test scripts Google with one `httpx.MockTransport` and the storage
logic never has to fake an HTTP reply.

PKCE (RFC 7636) on top of the client secret, not instead of it: the platform is
a confidential client and sends its secret at the exchange, but PKCE also binds
the exchange to the browser session that started it, so a leaked callback URL
with a stolen `code` is useless without the verifier this server generated.

**`refresh_token` is the crown jewel.** Google hands it out once, on the first
consent (`prompt=consent access_type=offline` forces that even for a returning
account), and it opens the account until revoked. It goes straight to the
encrypted secret store and never into a log, an exception message, or a model
context.
"""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from src.config import get_settings

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 — a URL, not a secret
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

#: What the Gmail & Google Drive connections need, requested in one consent so
#: the person signs once, not once per connection. `drive.file` and not `drive`:
#: writes reach only files this platform created — "full quyền" theo nghĩa đủ
#: để làm việc, không phải chìa khoá cả ổ đĩa. Reads stay whole-account via the
#: two `.readonly` scopes, which is what search needs.
GOOGLE_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
)


class GoogleOAuthError(RuntimeError):
    """Google refused, or answered with something that is not a token reply.

    The message is safe to show a person: it names what failed and what to do,
    never a token. `invalid_grant` — the refresh token was revoked or expired —
    gets its own wording because the fix ("kết nối lại tài khoản Google") is the
    person's to do, unlike every other failure here.
    """


@dataclass(frozen=True, slots=True)
class TokenReply:
    """What one successful call to the token endpoint said.

    `refresh_token` is empty on a plain refresh — Google only rotates it
    sometimes — and the caller keeps the old one in that case rather than
    overwriting a good credential with nothing.
    """

    access_token: str
    expires_in: int
    refresh_token: str = ""
    scope: str = ""


def mint_verifier() -> str:
    """A fresh PKCE code verifier — RFC 7636 §4.1's charset via base64url."""
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")


def challenge_of(verifier: str) -> str:
    """The S256 code challenge for `verifier` — the only method Google accepts."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(*, state: str, verifier: str, scopes: tuple[str, ...] = GOOGLE_SCOPES) -> str:
    """Where to send the browser so the person can say yes.

    `access_type=offline` + `prompt=consent` together are what produce a refresh
    token — without the second, a person who ever consented before gets a reply
    with no refresh token in it, and the connection dies within the hour.
    """
    settings = get_settings()
    if not settings.google_client_id:
        raise GoogleOAuthError(
            "Chưa cấu hình GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET nên chưa kết nối được tài khoản Google"
        )
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_oauth_redirect_url,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge_of(verifier),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{AUTHORIZATION_ENDPOINT}?{query}"


def exchange_code(code: str, *, verifier: str, client: httpx.Client | None = None) -> TokenReply:
    """The authorization code, traded for the tokens it stands for."""
    settings = get_settings()
    return _token_call(
        {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": settings.google_oauth_redirect_url,
        },
        client=client,
    )


def refresh_access_token(refresh_token: str, *, client: httpx.Client | None = None) -> TokenReply:
    """A fresh access token from the long-lived refresh token."""
    settings = get_settings()
    return _token_call(
        {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        client=client,
    )


USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

#: Google's own answer to "is this token alive, and what may it do" — audience,
#: expiry and granted scopes, with no scope of its own required to ask.
TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"  # noqa: S105 — a URL, not a secret

#: The scopes a pasted token must carry for the built-in connections to work at
#: all. Deliberately **not** `GOOGLE_SCOPES`: that is what the sign-in button
#: asks for, and a token minted elsewhere for one job (send mail, read a folder)
#: is a legitimate thing to paste. What matters is that it can do *something*
#: this platform offers — a token with none of these is one whose every call
#: would 403 later, which is worth refusing now, in words.
#:
#: `https://mail.google.com/` is on the list because it is the *parent* of every
#: Gmail scope — full mailbox access, the scope the OAuth Playground hands out
#: for "Gmail" by default. A token carrying it can do everything `gmail.send`
#: or `gmail.readonly` can, and it was being refused as "không có quyền nào
#: dùng được" for not starting with the `/auth/gmail` prefix.
_USABLE_SCOPE_PREFIXES = (
    "https://www.googleapis.com/auth/gmail",
    "https://www.googleapis.com/auth/drive",
    "https://mail.google.com/",
)


@dataclass(frozen=True)
class TokenCheck:
    """What Google says about a pasted access token.

    Three outcomes rather than two, because "we could not ask" is not "the token
    is bad" and telling a person the second when the first happened sends them
    to re-copy a token that was fine all along.
    """

    #: True only when Google positively confirmed the token.
    ok: bool
    #: Scopes Google says it carries, empty when unknown.
    scopes: tuple[str, ...] = ()
    #: Seconds left, or `None` when Google did not say.
    expires_in: int | None = None
    #: A sentence for the person, present exactly when `ok` is False.
    reason: str = ""


def check_access_token(access_token: str, *, client: httpx.Client | None = None) -> TokenCheck:
    """Ask Google whether this pasted token is alive and can do anything here.

    Written because the paste-a-token path used `account_email` as its
    validator, and `account_email` is **best-effort by design** — it answers
    `""` for a network blip, a userinfo outage, a token without the
    `userinfo.email` scope, and a genuinely dead token alike. Turning all four
    into *"Google không nhận access token này"* blamed the person's token for
    three things that were not its fault, and the most common of those is a
    perfectly good token minted for `gmail.send` and nothing else.

    `tokeninfo` is the right question to ask: it needs no scope of its own, it
    reports expiry and granted scopes, and it distinguishes "Google says no"
    from "Google did not answer".
    """
    token = access_token.strip()
    if not token:
        return TokenCheck(ok=False, reason="Chưa dán access token")
    try:
        with _client_or(client) as http:
            reply = http.get(TOKENINFO_ENDPOINT, params={"access_token": token})
    except httpx.HTTPError as exc:
        return TokenCheck(
            ok=False,
            reason=f"Không hỏi được Google để kiểm tra token ({exc.__class__.__name__}) — thử lại sau ít phút",
        )

    body = _json_of(reply)
    fields = body if isinstance(body, dict) else {}
    if reply.status_code != 200:
        detail = str(fields.get("error_description") or fields.get("error") or "").strip()
        return TokenCheck(
            ok=False,
            reason=(
                f"Google từ chối token này: {detail}. Access token chỉ sống khoảng một giờ — "
                "nếu bạn lấy nó từ trước thì nhiều khả năng đã hết hạn."
                if detail
                else "Google từ chối token này — access token chỉ sống khoảng một giờ, nhiều khả năng nó đã hết hạn."
            ),
        )

    scopes = tuple(str(fields.get("scope", "")).split())
    expires_raw = fields.get("expires_in")
    try:
        expires_in = int(expires_raw) if expires_raw is not None else None
    except (TypeError, ValueError):
        expires_in = None

    if not any(scope.startswith(_USABLE_SCOPE_PREFIXES) for scope in scopes):
        return TokenCheck(
            ok=False,
            scopes=scopes,
            expires_in=expires_in,
            reason=(
                "Token còn hiệu lực nhưng không có quyền nào dùng được ở đây — "
                "cần ít nhất một quyền Gmail hoặc Drive. "
                f"Quyền hiện có: {', '.join(scopes) if scopes else '(không có)'}"
            ),
        )
    return TokenCheck(ok=True, scopes=scopes, expires_in=expires_in)


def account_email(access_token: str, *, client: httpx.Client | None = None) -> str:
    """Which Google account these tokens open — for the Connections screen.

    Best-effort: the email is a label, not a credential, and a userinfo endpoint
    having a bad day should not fail a connection whose tokens are already
    safely stored. Empty string when it cannot be learned.
    """
    try:
        with _client_or(client) as http:
            reply = http.get(USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError:
        return ""
    body = _json_of(reply)
    email = body.get("email") if isinstance(body, dict) else None
    return email if isinstance(email, str) else ""


def revoke(token: str, *, client: httpx.Client | None = None) -> None:
    """Tell Google the token is dead. Best-effort: the caller is deleting its
    own copy either way, and a revoke endpoint refusing does not change that."""
    try:
        with _client_or(client) as http:
            http.post(REVOKE_ENDPOINT, data={"token": token})
    except httpx.HTTPError:
        pass


def _token_call(form: dict[str, str], *, client: httpx.Client | None) -> TokenReply:
    try:
        with _client_or(client) as http:
            reply = http.post(TOKEN_ENDPOINT, data=form)
    except httpx.HTTPError as exc:
        raise GoogleOAuthError(f"Không gọi được máy chủ OAuth của Google: {exc}") from exc

    body = _json_of(reply)
    if reply.status_code >= 400:
        error = str(body.get("error", "")) if isinstance(body, dict) else ""
        if error == "invalid_grant":
            raise GoogleOAuthError(
                "Google không còn chấp nhận uỷ quyền này — người dùng cần kết nối lại tài khoản Google"
            )
        raise GoogleOAuthError(f"Google từ chối cấp token (HTTP {reply.status_code}: {error or 'không rõ lý do'})")

    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
        raise GoogleOAuthError("Google trả lời không có access_token — không dùng được")
    return TokenReply(
        access_token=body["access_token"],
        expires_in=int(body.get("expires_in", 0) or 0),
        refresh_token=str(body.get("refresh_token", "") or ""),
        scope=str(body.get("scope", "") or ""),
    )


class _client_or:  # noqa: N801 — a with-statement helper, named like the expression it is
    """`client` if the caller brought one (left open — it is theirs), else a
    short-lived one of our own (closed on exit)."""

    def __init__(self, client: httpx.Client | None) -> None:
        self._external = client
        self._owned: httpx.Client | None = None

    def __enter__(self) -> httpx.Client:
        if self._external is not None:
            return self._external
        self._owned = httpx.Client(timeout=20.0)
        return self._owned

    def __exit__(self, *exc_info: object) -> None:
        if self._owned is not None:
            self._owned.close()


def _json_of(reply: httpx.Response) -> object:
    try:
        return reply.json()
    except ValueError:
        return {}
