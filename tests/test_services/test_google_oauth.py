"""`src.services.google_oauth` — the wire half of `UserConnection`.

Google is scripted with `httpx.MockTransport`, the module's own seam: these
tests prove what *we* send (PKCE binding, offline access, the exact form fields
of an exchange) and how we read what comes back — never that Google works.
"""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.services.google_oauth import (
    GoogleOAuthError,
    TokenReply,
    authorization_url,
    challenge_of,
    check_access_token,
    exchange_code,
    mint_verifier,
    refresh_access_token,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_verifier_is_fresh_and_urlsafe() -> None:
    """Two mints never collide, and the charset survives a query string raw."""
    one, two = mint_verifier(), mint_verifier()
    assert one != two
    assert all(c.isalnum() or c in "-_" for c in one)


def test_challenge_is_s256_of_verifier() -> None:
    verifier = mint_verifier()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge_of(verifier) == expected


def test_authorization_url_requests_offline_access_with_forced_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    """`access_type=offline` + `prompt=consent` together are what produce a
    refresh token — lose either and the connection dies within the hour."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-123")
    _fresh_settings()
    url = authorization_url(state="the-state", verifier="the-verifier")
    query = parse_qs(urlparse(url).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["state"] == ["the-state"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [challenge_of("the-verifier")]


def test_authorization_url_refuses_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """No client id → a sentence naming the missing configuration, not a URL
    that fails mysteriously at Google's side."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    _fresh_settings()
    with pytest.raises(GoogleOAuthError, match="GOOGLE_CLIENT_ID"):
        authorization_url(state="s", verifier="v")


def test_exchange_sends_verifier_and_reads_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-123")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-456")
    _fresh_settings()
    seen: dict[str, str] = {}

    def google(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        seen.update({key: values[0] for key, values in form.items()})
        return httpx.Response(
            200,
            json={"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3599, "scope": "a b"},
        )

    reply = exchange_code("the-code", verifier="the-verifier", client=_client(google))
    assert seen["code"] == "the-code"
    assert seen["code_verifier"] == "the-verifier"
    assert seen["grant_type"] == "authorization_code"
    assert reply == TokenReply(access_token="at-1", expires_in=3599, refresh_token="rt-1", scope="a b")


def test_refresh_keeps_missing_refresh_token_empty() -> None:
    """A plain refresh usually carries no new refresh token — the reply says so
    honestly (empty), and the caller keeps the old one."""

    def google(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3599})

    reply = refresh_access_token("rt-old", client=_client(google))
    assert reply.access_token == "at-2"
    assert reply.refresh_token == ""


def test_invalid_grant_reads_as_reconnect_not_system_failure() -> None:
    """`invalid_grant` is the person's revoked consent, and the sentence must
    send them to reconnect — not to an operator who can do nothing about it."""

    def google(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(GoogleOAuthError, match="kết nối lại"):
        refresh_access_token("rt-revoked", client=_client(google))


def test_reply_without_access_token_is_refused() -> None:
    def google(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(GoogleOAuthError, match="access_token"):
        refresh_access_token("rt-1", client=_client(google))


def _fresh_settings() -> None:
    """Settings are cached process-wide; a test that changes the env re-reads them."""
    from src.config import get_settings

    get_settings.cache_clear()


# ── check_access_token: four outcomes the old code reported as one ────────────
#
# The paste-a-token path used `account_email` as its validator. That function is
# best-effort by design and answers "" for a network blip, a userinfo outage, a
# token lacking `userinfo.email`, and a dead token alike — so all four reached
# the person as *"Google không nhận access token này"*, which is true of one of
# them. These pin the four apart.


def _tokeninfo(status: int, body: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/tokeninfo")
        return httpx.Response(status, json=body)

    return _client(handler)


def test_a_live_token_with_a_gmail_scope_is_accepted() -> None:
    check = check_access_token(
        "tok",
        client=_tokeninfo(200, {"scope": "https://www.googleapis.com/auth/gmail.send", "expires_in": "3200"}),
    )

    assert check.ok
    assert check.expires_in == 3200
    assert check.scopes == ("https://www.googleapis.com/auth/gmail.send",)
    assert check.reason == ""


def test_the_full_mailbox_scope_is_accepted_as_a_gmail_scope() -> None:
    """`https://mail.google.com/` is the parent of every Gmail scope — what the
    OAuth Playground mints for "Gmail" — and a token carrying it was refused
    as having no usable permission because it does not start with `/auth/gmail`."""
    check = check_access_token(
        "tok", client=_tokeninfo(200, {"scope": "https://mail.google.com/", "expires_in": "3480"})
    )

    assert check.ok, check.reason
    assert check.scopes == ("https://mail.google.com/",)


def test_a_token_without_userinfo_scope_is_still_accepted() -> None:
    """The regression. `gmail.send` alone cannot name its account, and that is
    not a reason to refuse it — it is a reason to show no name."""
    check = check_access_token("tok", client=_tokeninfo(200, {"scope": "https://www.googleapis.com/auth/drive.file"}))

    assert check.ok


def test_google_refusing_the_token_says_so_and_mentions_the_hour() -> None:
    check = check_access_token(
        "tok", client=_tokeninfo(400, {"error": "invalid_token", "error_description": "Invalid Value"})
    )

    assert not check.ok
    assert "Invalid Value" in check.reason
    assert "một giờ" in check.reason


def test_a_live_token_with_no_usable_scope_is_refused_by_scope_not_by_validity() -> None:
    check = check_access_token("tok", client=_tokeninfo(200, {"scope": "openid", "expires_in": "3600"}))

    assert not check.ok
    assert "không có quyền nào dùng được" in check.reason
    assert "openid" in check.reason  # says what it *does* have
    assert "từ chối" not in check.reason  # never blames the token's validity


def test_google_not_answering_is_not_reported_as_a_bad_token() -> None:
    """A network blip must not send somebody off to re-copy a token that is fine."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    check = check_access_token("tok", client=_client(handler))

    assert not check.ok
    assert "Không hỏi được Google" in check.reason
    assert "từ chối" not in check.reason


def test_an_empty_paste_is_named_as_such() -> None:
    assert check_access_token("   ").reason == "Chưa dán access token"
