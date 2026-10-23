"""A pasted Google access token knows when it dies, and says so everywhere it is read.

The field report that produced these: a creator pasted a Gmail token, built an
agent, and an hour later built a second one. Both screens said "đang nối"; the
MCP panel hid its paste box behind that sentence; the run answered with a 401
it could only call "tài khoản có thể vừa bị thu hồi". Nothing on the platform
knew the token had simply expired — the row had no expiry, the 401 flipped no
status, and the screen offered nothing to paste into.

Four facts fix that, and each has a test here:

- a paste records `expires_at` from Google's own `expires_in`;
- a run refuses an expired paste by name and flips the row to `needs_reconnect`;
- Google's 401 mid-run flips the row the same way;
- the Connections listing flips an expired paste before any run has to, and a
  fresh paste brings the row back to `connected`.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session

from src.db.models import Agent, SecretStoreEntry, User, UserConnection, UserConnectionStatus
from src.services.google_oauth import TokenCheck
from src.workflow import google_tools, user_connections
from src.workflow.google_tools import GoogleToolError
from src.workflow.user_connections import (
    ACCESS_TOKEN,
    EXPIRED_PASTE_MESSAGE,
    UserConnectionError,
    connect_google_with_token,
    google_access_token,
    google_connection_of,
)
from tests.conftest import auth_header, user_id_of

pytestmark = pytest.mark.asyncio

GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _google_answers(monkeypatch: pytest.MonkeyPatch):
    """Google, stubbed at the two seams the paste path talks to."""
    monkeypatch.setattr(
        user_connections.google_oauth,
        "check_access_token",
        lambda token: TokenCheck(ok=True, scopes=(GMAIL_SEND,), expires_in=3000, reason=""),
    )
    monkeypatch.setattr(user_connections.google_oauth, "account_email", lambda token: "")
    monkeypatch.setattr(user_connections.google_oauth, "revoke", lambda token: None)


@pytest.fixture(autouse=True)
def _nothing_outlives_its_test(db_session: Session):
    user_connections._token_cache.clear()
    yield
    user_connections._token_cache.clear()
    for row in db_session.query(UserConnection).all():
        db_session.delete(row)
    for row in db_session.query(SecretStoreEntry).all():
        db_session.delete(row)
    db_session.commit()


def _creator(db_session: Session, token: str) -> User:
    return db_session.get(User, user_id_of(token))


async def test_a_paste_records_when_its_token_dies(db_session: Session, creator_token: str):
    before = _now()
    row = connect_google_with_token(db_session, _creator(db_session, creator_token), "ya29.fresh")

    assert row.token_kind == ACCESS_TOKEN
    assert row.status is UserConnectionStatus.CONNECTED
    assert row.expires_at is not None
    # Google said 3000 seconds; the row says the same, give or take the call.
    assert timedelta(seconds=2990) <= row.expires_at - before <= timedelta(seconds=3010)


async def test_an_expired_paste_is_refused_by_name_and_the_row_says_so(db_session: Session, creator_token: str):
    user = _creator(db_session, creator_token)
    row = connect_google_with_token(db_session, user, "ya29.old")
    row.expires_at = _now() - timedelta(minutes=1)
    db_session.commit()
    # The paste-time cache dies with the token in real life; here the clock was
    # moved by hand, so the cache is emptied by hand to match.
    user_connections._token_cache.clear()

    with pytest.raises(UserConnectionError) as refused:
        google_access_token(user.id)

    assert str(refused.value) == EXPIRED_PASTE_MESSAGE
    db_session.refresh(row)
    assert row.status is UserConnectionStatus.NEEDS_RECONNECT


async def test_a_live_paste_is_handed_over(db_session: Session, creator_token: str):
    user = _creator(db_session, creator_token)
    connect_google_with_token(db_session, user, "ya29.live")
    user_connections._token_cache.clear()  # read it from the row, not the paste-time cache

    assert google_access_token(user.id) == "ya29.live"


async def test_googles_401_mid_run_flips_the_row(
    db_session: Session, creator_token: str, monkeypatch: pytest.MonkeyPatch
):
    user = _creator(db_session, creator_token)
    connect_google_with_token(db_session, user, "ya29.revoked")
    agent = Agent(owner_id=user.id, name="gửi mail")
    db_session.add(agent)
    db_session.commit()

    def refused(self, method, url, **kwargs):
        return httpx.Response(
            401, json={"error": {"message": "Invalid Credentials"}}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr(httpx.Client, "request", refused)

    with pytest.raises(GoogleToolError) as failed:
        google_tools.gmail_call("send_email", {"to": "a@b.c", "subject": "x", "body": "y"}, agent_id=agent.id)

    assert "dán token mới" in str(failed.value)
    db_session.expire_all()
    assert google_connection_of(db_session, user.id).status is UserConnectionStatus.NEEDS_RECONNECT
    # The cache no longer hands the refused token to the next run either.
    assert user.id not in user_connections._token_cache

    db_session.delete(agent)
    db_session.commit()


async def test_the_listing_flips_an_expired_paste_and_a_fresh_paste_brings_it_back(
    client, db_session: Session, creator_token: str
):
    user = _creator(db_session, creator_token)
    row = connect_google_with_token(db_session, user, "ya29.old")
    row.expires_at = _now() - timedelta(minutes=1)
    db_session.commit()

    listed = await client.get("/api/v1/user-connections", headers=auth_header(creator_token))
    assert listed.status_code == 200
    google = listed.json()[0]
    assert google["connected"] is True
    assert google["status"] == "needs_reconnect"
    assert google["token_kind"] == "access_token"
    assert google["expires_at"] is not None

    # The second agent's creator pastes again — the same row, alive again,
    # with a new expiry rather than the old one.
    pasted = await client.post(
        "/api/v1/user-connections/google/token",
        json={"access_token": "ya29.new"},
        headers=auth_header(creator_token),
    )
    assert pasted.status_code == 200
    assert pasted.json()["status"] == "connected"
    # `_now()` is the *column's* clock — naive, because that is what the row
    # stores. The wire's clock is not: `UtcTimestamp` labels every timestamp
    # leaving the API as the UTC it already is, so the browser converts instead
    # of reading it as local. Comparing the two needs the zone said out loud.
    assert datetime.fromisoformat(pasted.json()["expires_at"]) > _now().replace(tzinfo=UTC)
    db_session.expire_all()
    assert db_session.query(UserConnection).filter_by(user_id=user.id).count() == 1
