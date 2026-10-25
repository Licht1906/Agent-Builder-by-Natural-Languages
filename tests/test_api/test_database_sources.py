"""Hệ CSDL through HTTP — DS-02 / ADR-0059 §2.

The probe is real: the test Postgres holds UC-1's `schema.sql` (two views and
the base tables behind them), and its host is made trusted the way an
operator trusts a staging host. What these tests pin down is the platform's
own behaviour around that probe — the DSN never coming back, a base table
never being ticked, a database in use never being deleted.
"""

from collections.abc import Generator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.base import engine
from src.db.models import Agent, AgentVersion, DatabaseSource, SecretStoreEntry, User, VersionStatus
from src.deployments.rate_limit import SlidingWindowLimiter
from src.main import app
from src.services.connection_test_rate_limit import get_connection_test_rate_limiter
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

#: A password distinctive enough that finding it anywhere means it leaked — for
#: every test that never needs the DSN to actually connect.
MARKED_PASSWORD = "bi-mat-dsn-7731"

#: The real test database's credentials as they sit inside a DSN. Its password
#: is the ordinary word `postgres`, which also names the user, so a probe that
#: must really connect is checked for the `user:password@` run instead.
REAL_USERINFO = f"{engine.url.username}:{engine.url.password}@"


def _dsn(**overrides: Any) -> str:
    url: URL = engine.url.set(drivername="postgresql", **overrides)
    return url.render_as_string(hide_password=False)


@pytest.fixture(autouse=True)
def _trusted_test_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test database is local, so an operator would have to trust it — this does."""
    monkeypatch.setattr(get_settings(), "database_source_trusted_hosts", str(engine.url.host))


@pytest.fixture(autouse=True)
def _fresh_test_limiter() -> Generator[None, None, None]:
    """Every test's own `/test` counters, so one test's probes never spend another's allowance."""
    limiter = SlidingWindowLimiter()
    app.dependency_overrides[get_connection_test_rate_limiter] = lambda: limiter
    yield
    app.dependency_overrides.pop(get_connection_test_rate_limiter, None)


@pytest.fixture(autouse=True)
def _no_source_outlives_its_test(db_session: Session) -> Generator[None, None, None]:
    yield
    db_session.expire_all()
    for row in db_session.query(DatabaseSource).all():
        db_session.delete(row)
    db_session.query(SecretStoreEntry).filter(SecretStoreEntry.key.like("database_source:%")).delete(
        synchronize_session=False
    )
    db_session.commit()


async def _create(client: AsyncClient, token: str, *, dsn: str | None = None, name: str = "ERP staging") -> Any:
    return await client.post(
        "/api/v1/database-sources",
        json={"name": name, "dialect": "postgresql", "dsn": dsn or _dsn()},
        headers=auth_header(token),
    )


async def _tested(client: AsyncClient, token: str) -> dict[str, Any]:
    created = (await _create(client, token)).json()
    response = await client.post(f"/api/v1/database-sources/{created['id']}/test", headers=auth_header(token))
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# The DSN goes in and never comes out
# --------------------------------------------------------------------------- #


async def test_creating_a_source_never_echoes_the_dsn(client: AsyncClient, creator_token: str) -> None:
    response = await _create(client, creator_token, dsn=_dsn(password=MARKED_PASSWORD))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["has_dsn"] is True
    assert body["status"] == "untested"
    assert MARKED_PASSWORD not in response.text


async def test_the_dsn_is_kept_in_the_secret_store_not_in_the_row(
    client: AsyncClient, creator_token: str, db_session: Session
) -> None:
    created = (await _create(client, creator_token, dsn=_dsn(password=MARKED_PASSWORD))).json()

    row = db_session.get(DatabaseSource, created["id"])
    assert row is not None
    assert MARKED_PASSWORD not in repr(vars(row))
    entry = db_session.get(SecretStoreEntry, row.dsn_secret_ref)
    assert entry is not None
    assert MARKED_PASSWORD.encode() not in entry.ciphertext


async def test_no_response_on_the_way_carries_the_dsn(client: AsyncClient, creator_token: str) -> None:
    tested = await _tested(client, creator_token)
    listed = await client.get("/api/v1/database-sources", headers=auth_header(creator_token))
    one = await client.get(f"/api/v1/database-sources/{tested['id']}", headers=auth_header(creator_token))

    for response in (listed, one):
        assert response.status_code == 200
        assert REAL_USERINFO not in response.text


async def test_a_refused_request_does_not_echo_the_dsn_back(client: AsyncClient, creator_token: str) -> None:
    response = await client.post(
        "/api/v1/database-sources",
        json={"dialect": "postgresql", "dsn": _dsn(password=MARKED_PASSWORD)},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "name"]
    assert MARKED_PASSWORD not in response.text


async def test_the_dsn_never_reaches_the_log(
    client: AsyncClient, creator_token: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    created = (await _create(client, creator_token, dsn=_dsn(password="sai-mat-khau-5521"))).json()
    await client.post(f"/api/v1/database-sources/{created['id']}/test", headers=auth_header(creator_token))

    assert "sai-mat-khau-5521" not in caplog.text


# --------------------------------------------------------------------------- #
# Kiểm tra kết nối
# --------------------------------------------------------------------------- #


async def test_a_good_dsn_lists_the_views_with_their_row_counts(client: AsyncClient, creator_token: str) -> None:
    tested = await _tested(client, creator_token)

    assert tested["status"] == "connected"
    views = {view["name"]: view for view in tested["views"]}
    assert {"v_ar_open", "v_customer_safe"} <= set(views)
    assert isinstance(views["v_ar_open"]["row_count"], int)
    assert "days_overdue" in views["v_ar_open"]["columns"]
    assert "fin_invoice" in tested["base_tables"]


async def test_a_wrong_password_fails_with_a_reason_a_person_can_act_on(
    client: AsyncClient, creator_token: str
) -> None:
    created = (await _create(client, creator_token, dsn=_dsn(password="sai-mat-khau-5521"))).json()

    response = await client.post(f"/api/v1/database-sources/{created['id']}/test", headers=auth_header(creator_token))

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "failed"
    assert body["status_reason"] == "auth_rejected"
    assert "mật khẩu" in body["status_detail"]
    assert "sai-mat-khau-5521" not in response.text


async def test_an_internal_host_is_refused_unless_an_operator_trusts_it(
    client: AsyncClient, creator_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "database_source_trusted_hosts", "")
    created = (await _create(client, creator_token)).json()

    response = await client.post(f"/api/v1/database-sources/{created['id']}/test", headers=auth_header(creator_token))

    assert response.json()["status_reason"] == "blocked_address"


async def test_test_is_rate_limited(client: AsyncClient, creator_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "external_connection_test_rate_limit_per_user", 1)
    created = (await _create(client, creator_token)).json()
    url = f"/api/v1/database-sources/{created['id']}/test"

    first = await client.post(url, headers=auth_header(creator_token))
    second = await client.post(url, headers=auth_header(creator_token))

    assert first.status_code == 200
    assert second.status_code == 429


# --------------------------------------------------------------------------- #
# Ticking views
# --------------------------------------------------------------------------- #


async def test_ticked_views_are_kept(client: AsyncClient, creator_token: str) -> None:
    tested = await _tested(client, creator_token)

    response = await client.patch(
        f"/api/v1/database-sources/{tested['id']}",
        json={"allowed_views": ["v_ar_open", "v_customer_safe"]},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["allowed_views"] == ["v_ar_open", "v_customer_safe"]
    assert {view["name"] for view in response.json()["views"] if view["allowed"]} == {"v_ar_open", "v_customer_safe"}


async def test_ticking_a_base_table_is_refused(client: AsyncClient, creator_token: str) -> None:
    tested = await _tested(client, creator_token)

    response = await client.patch(
        f"/api/v1/database-sources/{tested['id']}",
        json={"allowed_views": ["v_ar_open", "fin_invoice"]},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422
    assert "fin_invoice là bảng gốc" in response.text


async def test_views_cannot_be_ticked_before_a_probe_has_seen_any(client: AsyncClient, creator_token: str) -> None:
    created = (await _create(client, creator_token)).json()

    response = await client.patch(
        f"/api/v1/database-sources/{created['id']}",
        json={"allowed_views": ["v_ar_open"]},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422
    assert "Kiểm tra kết nối" in response.text


async def test_a_probe_unticks_whatever_is_no_longer_a_view(
    client: AsyncClient, creator_token: str, db_session: Session
) -> None:
    """A row carried over from `uc1_connections` may name a base table; a probe must not launder it in."""
    created = (await _create(client, creator_token)).json()
    row = db_session.get(DatabaseSource, created["id"])
    assert row is not None
    row.allowed_views = ["v_ar_open", "fin_invoice", "v_da_bi_xoa"]
    db_session.commit()

    response = await client.post(f"/api/v1/database-sources/{created['id']}/test", headers=auth_header(creator_token))

    body = response.json()
    assert body["allowed_views"] == ["v_ar_open"]
    assert "fin_invoice" in body["status_detail"]
    assert "v_da_bi_xoa" in body["status_detail"]


# --------------------------------------------------------------------------- #
# A spec's `database` source names one of the creator's own
# --------------------------------------------------------------------------- #

SPEC = {
    "goal": "Trả lời câu hỏi công nợ",
    "users": ["Nhân viên kinh doanh"],
    "actions": ["Tra công nợ"],
    "expected_outcome": ["Câu trả lời có số liệu"],
    "success_criteria": [{"text": "Đúng số liệu", "category": "correctness"}],
    "prohibitions": ["Không xem khách của người khác"],
}


@pytest.fixture
def pending_spec_url(db_session: Session, seeded_creator: User) -> Generator[str, None, None]:
    """A version of the seeded creator's, its spec awaiting review — the one state a spec can be edited in."""
    agent = Agent(owner_id=seeded_creator.id, name="Trợ lý công nợ")
    db_session.add(agent)
    db_session.flush()
    version = AgentVersion(
        agent_id=agent.id,
        status=VersionStatus.SPEC_PENDING_CONFIRMATION,
        solution_spec={**SPEC, "data": [{"name": "ERP", "kind": "database", "ref": None}]},
    )
    db_session.add(version)
    db_session.commit()
    yield f"/api/v1/agents/{agent.id}/versions/{version.id}/solution-spec"
    db_session.query(AgentVersion).filter(AgentVersion.agent_id == agent.id).delete()
    db_session.delete(agent)
    db_session.commit()


async def test_a_spec_may_name_the_creators_own_database(
    client: AsyncClient, creator_token: str, pending_spec_url: str
) -> None:
    own = (await _create(client, creator_token)).json()

    response = await client.put(
        pending_spec_url,
        json={**SPEC, "data": [{"name": "ERP", "kind": "database", "ref": str(own["id"])}]},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 200, response.text


async def test_a_spec_may_not_name_someone_elses_database(
    client: AsyncClient, creator_token: str, second_creator_token: str, pending_spec_url: str
) -> None:
    theirs = (await _create(client, second_creator_token, name="ERP của người khác")).json()

    response = await client.put(
        pending_spec_url,
        json={**SPEC, "data": [{"name": "ERP", "kind": "database", "ref": str(theirs["id"])}]},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422
    assert str(theirs["id"]) in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Re-entering a DSN, renaming
# --------------------------------------------------------------------------- #


async def test_a_new_dsn_replaces_the_old_and_asks_for_a_fresh_probe(client: AsyncClient, creator_token: str) -> None:
    tested = await _tested(client, creator_token)

    response = await client.patch(
        f"/api/v1/database-sources/{tested['id']}",
        json={"dsn": _dsn(password="sai-mat-khau-5521"), "name": "ERP mới"},
        headers=auth_header(creator_token),
    )
    retested = await client.post(f"/api/v1/database-sources/{tested['id']}/test", headers=auth_header(creator_token))

    assert response.json()["status"] == "untested"
    assert response.json()["name"] == "ERP mới"
    assert retested.json()["status_reason"] == "auth_rejected"


async def test_a_source_without_a_dsn_says_so_instead_of_dialling(
    client: AsyncClient, creator_token: str, db_session: Session, seeded_creator: User
) -> None:
    source = DatabaseSource(owner_id=seeded_creator.id, name="Từ uc1_connections", dialect="postgresql")
    source.last_probe_status = "needs_dsn"  # type: ignore[assignment]
    db_session.add(source)
    db_session.commit()

    response = await client.post(f"/api/v1/database-sources/{source.id}/test", headers=auth_header(creator_token))

    assert response.json()["status"] == "needs_dsn"
    assert response.json()["has_dsn"] is False


# --------------------------------------------------------------------------- #
# Ownership and deletion
# --------------------------------------------------------------------------- #


async def test_someone_elses_source_is_not_found(
    client: AsyncClient, creator_token: str, second_creator_token: str
) -> None:
    created = (await _create(client, creator_token)).json()

    for response in (
        await client.get(f"/api/v1/database-sources/{created['id']}", headers=auth_header(second_creator_token)),
        await client.post(f"/api/v1/database-sources/{created['id']}/test", headers=auth_header(second_creator_token)),
        await client.delete(f"/api/v1/database-sources/{created['id']}", headers=auth_header(second_creator_token)),
    ):
        assert response.status_code == 404
    listed = await client.get("/api/v1/database-sources", headers=auth_header(second_creator_token))
    assert created["id"] not in [row["id"] for row in listed.json()]


async def test_deleting_a_source_removes_its_dsn_from_the_secret_store(
    client: AsyncClient, creator_token: str, db_session: Session
) -> None:
    created = (await _create(client, creator_token)).json()
    secret_ref = db_session.get(DatabaseSource, created["id"]).dsn_secret_ref  # type: ignore[union-attr]

    response = await client.delete(f"/api/v1/database-sources/{created['id']}", headers=auth_header(creator_token))

    assert response.status_code == 204
    db_session.expire_all()
    assert db_session.get(SecretStoreEntry, secret_ref) is None


async def test_deleting_a_source_an_agent_reads_from_is_blocked(
    client: AsyncClient, creator_token: str, db_session: Session, seeded_creator: User
) -> None:
    created = (await _create(client, creator_token)).json()
    agent = Agent(owner_id=seeded_creator.id, name="Trợ lý công nợ")
    db_session.add(agent)
    db_session.flush()
    db_session.add(
        AgentVersion(
            agent_id=agent.id,
            status=VersionStatus.SPEC_PENDING_CONFIRMATION,
            solution_spec={"data": [{"name": "ERP", "kind": "database", "ref": str(created["id"])}]},
        )
    )
    db_session.commit()
    try:
        response = await client.delete(f"/api/v1/database-sources/{created['id']}", headers=auth_header(creator_token))

        assert response.status_code == 409
        assert response.json()["detail"]["agents"] == ["Trợ lý công nợ"]
    finally:
        db_session.query(AgentVersion).filter(AgentVersion.agent_id == agent.id).delete()
        db_session.delete(agent)
        db_session.commit()
