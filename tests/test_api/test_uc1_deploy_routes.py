"""UC1-18 (#134) — `POST /agents/{id}/versions/{v}/deploy` and `POST /agents/{id}/suspend`,
spec.md §19, AC-18. UC1-25 (#141) adds `POST /agents/{id}/rollback` — the same
role-provisioning wiring, so its tests share this file's fixtures.

Runs against the shared `app_dev` test database (`db_session`/`client`), which
`tests/conftest.py` resets with `Base.metadata.create_all` rather than
`schema.sql` — no `v_ar_open` there. `_uc1_role_test_view` stands in for it: a
real view this test creates and drops itself, so `provision_agent_role`'s
`GRANT SELECT` still runs against a real Postgres object.
`tests/test_db/test_uc1_roles.py` is what proves the grants line up with the
actual demo views, against a fully migrated database — this file's job is the
HTTP wiring: the lifecycle transition, the 404/409s, and the role's privileges
actually changing by the time the response comes back.
"""

import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from httpx import AsyncClient
from psycopg.errors import InsufficientPrivilege, UndefinedTable

from src.agentspec.store import get_deployed_spec_version
from src.config import get_settings
from src.db.models import UC1AgentSpecVersion
from tests.conftest import auth_header, user_id_of
from tests.test_agentspec.conftest import spec_payload

pytestmark = pytest.mark.asyncio

_TEST_VIEW = "uc1_role_test_view"
_TEST_VIEW_B = "uc1_role_test_view_b"


@pytest.fixture(autouse=True)
def _uc1_role_test_view() -> Iterator[None]:
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"CREATE OR REPLACE VIEW {_TEST_VIEW} AS SELECT 1 AS n")
        cur.execute(f"CREATE OR REPLACE VIEW {_TEST_VIEW_B} AS SELECT 1 AS n")
    yield
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn, conn.cursor() as cur:
        for role in (
            "agt_uc1_deploy_solo",
            "agt_uc1_deploy_a",
            "agt_uc1_deploy_b",
            "agt_uc1_deploy_draft",
            "agt_uc1_suspend",
            "agt_uc1_rollback_solo",
            "agt_uc1_rollback",
            "agt_uc1_rollback_draft",
        ):
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                continue
            cur.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{role}"')
            cur.execute(f'REVOKE ALL ON SCHEMA public FROM "{role}"')
            cur.execute(f'DROP ROLE "{role}"')
        cur.execute(f"DROP VIEW IF EXISTS {_TEST_VIEW}")
        cur.execute(f"DROP VIEW IF EXISTS {_TEST_VIEW_B}")


def _seed_spec_version(
    db_session,
    *,
    created_by_id: int,
    agent_id: str,
    version: int = 1,
    status_: str = "approved",
    allowed_objects: list[str] | None = None,
    **overrides: Any,
) -> None:
    payload = spec_payload(
        metadata={**spec_payload()["metadata"], "id": agent_id, "version": version, "status": status_},
        connections=[
            {
                "id": "conn_erp",
                "type": "postgres",
                "credential_ref": "vault://conn/erp_readonly",
                "allowed_objects": allowed_objects if allowed_objects is not None else [_TEST_VIEW],
                "read_only": True,
            }
        ],
        **overrides,
    )
    db_session.add(UC1AgentSpecVersion(agent_id=agent_id, version=version, spec=payload, created_by_id=created_by_id))
    db_session.commit()


def _version_status(db_session, *, agent_id: str, version: int) -> str:
    row = db_session.query(UC1AgentSpecVersion).filter_by(agent_id=agent_id, version=version).one()
    return row.spec["metadata"]["status"]


def _role_grants(role: str) -> set[tuple[str, str]]:
    with psycopg.connect(get_settings().database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = %s AND table_schema = 'public'",
            (role,),
        )
        return {(row[0], row[1]) for row in cur.fetchall()}


async def test_deploying_an_approved_version_provisions_its_own_role(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_uc1_deploy_solo")

    response = await client.post(
        "/api/v1/agents/agt_uc1_deploy_solo/versions/1/deploy", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "deployed"
    assert _role_grants("agt_uc1_deploy_solo") == {(_TEST_VIEW, "SELECT")}


async def test_two_deployed_agents_hold_disjoint_role_grants(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    """AC-18: "hai agent khác nhau deploy ra hai role Postgres khác nhau, không thừa quyền lẫn nhau"."""
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_uc1_deploy_a", allowed_objects=[_TEST_VIEW])
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_uc1_deploy_b", allowed_objects=[_TEST_VIEW_B])

    for agent_id in ("agt_uc1_deploy_a", "agt_uc1_deploy_b"):
        response = await client.post(
            f"/api/v1/agents/{agent_id}/versions/1/deploy", headers=auth_header(agent_admin_token)
        )
        assert response.status_code == 200, response.text

    assert _role_grants("agt_uc1_deploy_a") == {(_TEST_VIEW, "SELECT")}
    assert _role_grants("agt_uc1_deploy_b") == {(_TEST_VIEW_B, "SELECT")}


async def test_deploying_over_a_running_version_retires_it(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    """§13's "mỗi agent chỉ có tối đa một version chạy thật", on the upgrade path.

    The rollback test above happens to suspend v1 by hand before deploying v2,
    which is why it never caught this: deploying straight over a running version
    — the ordinary way an agent is upgraded — left *both* rows `deployed`.
    Nothing complained at deploy time. The damage showed up one layer down, at
    `get_deployed_spec_version`'s `scalar_one_or_none`, which raises
    `MultipleResultsFound` rather than picking one — so the production chat door
    and the kill switch, which both resolve their target through it, returned
    500 for an agent whose only sin was being upgraded twice.
    """
    agent_id = "agt_uc1_deploy_over_running"
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id=agent_id, version=1, allowed_objects=[_TEST_VIEW])
    _seed_spec_version(db_session, created_by_id=user_id, agent_id=agent_id, version=2, allowed_objects=[_TEST_VIEW_B])

    await client.post(f"/api/v1/agents/{agent_id}/versions/1/deploy", headers=auth_header(agent_admin_token))
    deploy_v2 = await client.post(f"/api/v1/agents/{agent_id}/versions/2/deploy", headers=auth_header(agent_admin_token))

    assert deploy_v2.status_code == 200, deploy_v2.text
    assert _version_status(db_session, agent_id=agent_id, version=1) == "suspended"
    assert _version_status(db_session, agent_id=agent_id, version=2) == "deployed"
    # The invariant stated as the caller actually consumes it, not just as two
    # status strings: exactly one row is resolvable as *the* deployed version.
    assert get_deployed_spec_version(db_session, agent_id).metadata.version == 2
    # The outgoing version's grants are gone, not merely its status — a retired
    # version that can still read is a kill switch that did not switch anything.
    assert _role_grants(agent_id) == {(_TEST_VIEW_B, "SELECT")}


async def test_redeploying_the_same_version_does_not_retire_it(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    """The guard against the retirement rule eating its own target: re-provisioning
    the version that is already serving must leave it serving, not suspend it
    on the way to deploying it.
    """
    agent_id = "agt_uc1_redeploy_same"
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id=agent_id, version=1, allowed_objects=[_TEST_VIEW])

    await client.post(f"/api/v1/agents/{agent_id}/versions/1/deploy", headers=auth_header(agent_admin_token))
    again = await client.post(f"/api/v1/agents/{agent_id}/versions/1/deploy", headers=auth_header(agent_admin_token))

    # `deploy` only moves `approved -> deployed`, so the second call is refused
    # as an invalid transition — the point being that it is refused *there*,
    # with v1 still deployed, rather than after this handler suspended it.
    assert again.status_code == 409, again.text
    assert _version_status(db_session, agent_id=agent_id, version=1) == "deployed"


async def test_deploying_a_draft_version_is_refused(client: AsyncClient, agent_admin_token: str, db_session) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_uc1_deploy_draft", status_="draft")

    response = await client.post(
        "/api/v1/agents/agt_uc1_deploy_draft/versions/1/deploy", headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 409, response.text
    assert any(problem["code"] == "invalid_transition" for problem in response.json()["detail"])


async def test_suspending_a_deployed_agent_revokes_role_privileges_fast(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_uc1_suspend")
    deploy_response = await client.post(
        "/api/v1/agents/agt_uc1_suspend/versions/1/deploy", headers=auth_header(agent_admin_token)
    )
    assert deploy_response.status_code == 200, deploy_response.text

    started = time.monotonic()
    response = await client.post("/api/v1/agents/agt_uc1_suspend/suspend", headers=auth_header(agent_admin_token))
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "suspended"
    # AC-18: "sau suspend, request tiếp theo bị từ chối trong < 1 giây đo bằng
    # đồng hồ thực" — measured end to end through the HTTP call itself, not
    # only at the `src.agentspec.roles` unit level (`test_uc1_roles.py`).
    assert elapsed < 1.0
    assert _role_grants("agt_uc1_suspend") == set()

    with psycopg.connect(get_settings().database_url, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute('SET LOCAL ROLE "agt_uc1_suspend"')
        # `InsufficientPrivilege` when the DB's `public` schema still grants ambient
        # `USAGE` to `PUBLIC` (name resolves, table access is denied);
        # `UndefinedTable` when it does not (the role can no longer even resolve
        # the unqualified name) — which of the two `public` schema privileges
        # `app_dev` itself carries is not what this test is about. Either way
        # the role can no longer read the view, which is the one guarantee FR-35
        # actually makes.
        with pytest.raises((InsufficientPrivilege, UndefinedTable)):
            cur.execute(f"SELECT * FROM {_TEST_VIEW}")


async def test_suspending_an_agent_with_no_deployed_version_is_a_404(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    response = await client.post("/api/v1/agents/agt_never_deployed/suspend", headers=auth_header(agent_admin_token))

    assert response.status_code == 404


async def test_rollback_deploys_an_approved_version_that_was_never_deployed_before(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_uc1_rollback_solo", allowed_objects=[_TEST_VIEW])

    response = await client.post(
        "/api/v1/agents/agt_uc1_rollback_solo/rollback", json={"version": 1}, headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "deployed", "version": 1}
    assert _role_grants("agt_uc1_rollback_solo") == {(_TEST_VIEW, "SELECT")}


async def test_rollback_reinstates_a_suspended_version_and_takes_down_the_current_one(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    """FR-51's own AC: "rollback đổi đúng con trỏ, không tạo version mới, không
    cần duyệt lại" — v1 goes deployed→suspended (a real deploy+suspend cycle,
    not seeded directly), v2 is deployed after it, then rollback moves the
    pointer back to v1: v1 deployed again with its own role grants restored,
    v2 suspended, and no version 3 was ever created.
    """
    agent_id = "agt_uc1_rollback"
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id=agent_id, version=1, allowed_objects=[_TEST_VIEW])
    _seed_spec_version(db_session, created_by_id=user_id, agent_id=agent_id, version=2, allowed_objects=[_TEST_VIEW_B])

    await client.post(f"/api/v1/agents/{agent_id}/versions/1/deploy", headers=auth_header(agent_admin_token))
    await client.post(f"/api/v1/agents/{agent_id}/suspend", headers=auth_header(agent_admin_token))
    deploy_v2 = await client.post(f"/api/v1/agents/{agent_id}/versions/2/deploy", headers=auth_header(agent_admin_token))
    assert deploy_v2.status_code == 200, deploy_v2.text

    rollback_response = await client.post(
        f"/api/v1/agents/{agent_id}/rollback", json={"version": 1}, headers=auth_header(agent_admin_token)
    )

    assert rollback_response.status_code == 200, rollback_response.text
    assert rollback_response.json() == {"status": "deployed", "version": 1}
    assert _version_status(db_session, agent_id=agent_id, version=1) == "deployed"
    assert _version_status(db_session, agent_id=agent_id, version=2) == "suspended"
    assert db_session.query(UC1AgentSpecVersion).filter_by(agent_id=agent_id).count() == 2  # no new version created
    assert _role_grants(agent_id) == {(_TEST_VIEW, "SELECT")}  # reset to v1's own connections, not v2's


async def test_rollback_is_refused_for_a_draft_target(client: AsyncClient, agent_admin_token: str, db_session) -> None:
    user_id = user_id_of(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id="agt_uc1_rollback_draft", status_="draft")

    response = await client.post(
        "/api/v1/agents/agt_uc1_rollback_draft/rollback", json={"version": 1}, headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 409, response.text
    assert any(problem["code"] == "invalid_transition" for problem in response.json()["detail"])


async def test_rollback_to_a_missing_version_is_a_404(client: AsyncClient, agent_admin_token: str) -> None:
    response = await client.post(
        "/api/v1/agents/agt_never_created/rollback", json={"version": 1}, headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 404
