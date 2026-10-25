"""UC1-18 (#134) — `src.agentspec.roles`, run for real against a freshly migrated
database so `information_schema` answers for a role `CREATE ROLE`/`GRANT`
actually touched, not a mock of one.
"""

import time

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from src.agentspec.roles import (
    AgentRoleGrantError,
    assert_agent_role_can_read,
    missing_connection_reason,
    provision_agent_role,
    suspend_agent_role,
)
from src.agentspec.spec import AgentSpec
from tests.test_agentspec.conftest import spec_payload
from tests.test_db.conftest import upgrade_to_head


@pytest.fixture
def migrated_database_url(empty_database_url: str) -> str:
    upgrade_to_head(empty_database_url)
    return empty_database_url


def _spec(agent_id: str, allowed_objects: list[str]) -> AgentSpec:
    return AgentSpec.model_validate(
        spec_payload(
            metadata={**spec_payload()["metadata"], "id": agent_id},
            connections=[
                {
                    "id": "conn_erp",
                    "type": "postgres",
                    "credential_ref": "vault://conn/erp_readonly",
                    "allowed_objects": allowed_objects,
                    "read_only": True,
                }
            ],
        )
    )


def _grants(cur: psycopg.Cursor, role: str) -> set[tuple[str, str]]:
    cur.execute(
        "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
        "WHERE grantee = %s AND table_schema = 'public'",
        (role,),
    )
    return {(row[0], row[1]) for row in cur.fetchall()}


def test_two_agents_deploy_into_two_roles_with_disjoint_grants(migrated_database_url: str) -> None:
    """AC-18: "hai agent khác nhau deploy ra hai role Postgres khác nhau, không thừa quyền lẫn nhau"."""
    spec_a = _spec("agt_uc1_role_a", ["v_ar_open"])
    spec_b = _spec("agt_uc1_role_b", ["v_customer_safe"])

    with psycopg.connect(migrated_database_url, autocommit=False) as conn:
        provision_agent_role(conn, spec_a)
        provision_agent_role(conn, spec_b)

        with conn.cursor() as cur:
            grants_a = _grants(cur, "agt_uc1_role_a")
            grants_b = _grants(cur, "agt_uc1_role_b")

    # `crm_sales_assignment` is a table-level grant every view needs (both join
    # through it) — the column-level grants `v_ar_open`/`v_customer_safe`'s own
    # `security_invoker` also requires on `fin_invoice`/`customers`/`orders`
    # never show up in `role_table_grants` at all (that's `column_privileges`'
    # territory, see `scripts/verify_uc1_data_surface.py`), so they are proven
    # below by actually querying through the view instead.
    assert grants_a == {("v_ar_open", "SELECT"), ("crm_sales_assignment", "SELECT")}
    assert grants_b == {("v_customer_safe", "SELECT"), ("crm_sales_assignment", "SELECT")}
    assert grants_a - {("crm_sales_assignment", "SELECT")} != grants_b - {("crm_sales_assignment", "SELECT")}


def test_a_provisioned_role_can_actually_select_through_its_own_view(migrated_database_url: str) -> None:
    """UC1-36 (#148): `security_invoker` views check the *querying* role's own
    privileges on the underlying tables, not the view owner's — a role that
    only ever got `GRANT SELECT ON v_ar_open` (this module's own behavior
    before this fix) could never actually read a row through it. Nothing
    caught that until a real caller queried through a deployed agent's own
    role rather than the fixed `agt_ar_assistant_ro` every other caller uses.
    """
    with psycopg.connect(migrated_database_url, autocommit=False) as conn:
        provision_agent_role(conn, _spec("agt_uc1_role_can_read", ["v_ar_open", "v_customer_safe"]))

        with conn.cursor() as cur:
            cur.execute('SET LOCAL ROLE "agt_uc1_role_can_read"')
            cur.execute("SET LOCAL app.scope = 'all'")
            # Both selects must succeed without raising — an `InsufficientPrivilege`
            # here is exactly the bug this test exists to catch.
            cur.execute("SELECT * FROM v_ar_open")
            cur.execute("SELECT * FROM v_customer_safe")


def test_redeploying_resets_grants_to_the_new_specs_connections(migrated_database_url: str) -> None:
    """A version whose `allowed_objects` shrank on redeploy must not keep the old view's grant."""
    with psycopg.connect(migrated_database_url, autocommit=False) as conn:
        provision_agent_role(conn, _spec("agt_uc1_role_redeploy", ["v_ar_open", "v_customer_safe"]))
        provision_agent_role(conn, _spec("agt_uc1_role_redeploy", ["v_customer_safe"]))

        with conn.cursor() as cur:
            grants = _grants(cur, "agt_uc1_role_redeploy")

    assert grants == {("v_customer_safe", "SELECT"), ("crm_sales_assignment", "SELECT")}


def test_suspend_revokes_within_one_second_and_the_role_can_no_longer_select(migrated_database_url: str) -> None:
    """AC-18: "sau suspend, request tiếp theo bị từ chối trong < 1 giây đo bằng đồng hồ thực"."""
    with psycopg.connect(migrated_database_url, autocommit=False) as conn:
        provision_agent_role(conn, _spec("agt_uc1_role_suspend", ["v_ar_open"]))

        started = time.monotonic()
        suspend_agent_role(conn, "agt_uc1_role_suspend")
        elapsed = time.monotonic() - started
        assert elapsed < 1.0

        with conn.cursor() as cur:
            cur.execute("SAVEPOINT after_suspend")
            cur.execute('SET LOCAL ROLE "agt_uc1_role_suspend"')
            with pytest.raises(InsufficientPrivilege):
                cur.execute("SELECT * FROM v_ar_open")
            cur.execute("ROLLBACK TO SAVEPOINT after_suspend")
            cur.execute("RESET ROLE")


def _spec_without_connections(agent_id: str) -> AgentSpec:
    """`spec_payload`'s default already grants `erp.query_receivables` and names
    no connection — the exact shape the UC-1 meta-agent drafted in practice.
    """
    return AgentSpec.model_validate(spec_payload(metadata={**spec_payload()["metadata"], "id": agent_id}))


def test_a_view_reading_spec_with_no_connection_is_refused_before_anything_is_provisioned() -> None:
    """The empty-`connections` deploy this check exists to stop.

    Nothing downstream objected to it: `compile_spec` verifies FR-24 per
    declared connection so zero connections raised zero objections, the grant
    loop iterates the same empty list, and the version reached `deployed`
    answering only `permission denied for view v_ar_open`.
    """
    reason = missing_connection_reason(_spec_without_connections("agt_uc1_no_conn"))

    assert reason is not None
    assert "erp.query_receivables" in reason


def test_a_spec_that_declares_its_connection_is_not_refused() -> None:
    assert missing_connection_reason(_spec("agt_uc1_has_conn", ["v_ar_open"])) is None


def test_provision_refuses_to_report_success_when_the_role_still_cannot_read(migrated_database_url: str) -> None:
    """`_VIEW_BASE_TABLE_GRANTS`'s own note records this repo shipping grants
    that looked right in `information_schema` and still could not read a row,
    uncaught because every pre-deploy caller queries a different role. Reading
    as the role itself is what closes that, so revoking the base-table grant
    behind its back must be caught rather than reported as a good deploy.
    """
    spec = _spec("agt_uc1_role_unreadable", ["v_ar_open"])

    with psycopg.connect(migrated_database_url, autocommit=False) as conn:
        provision_agent_role(conn, spec)
        with conn.cursor() as cur:
            # `v_ar_open` is `security_invoker`, so losing a base table is
            # enough to make the view unreadable while its own grant remains.
            cur.execute('REVOKE ALL ON fin_invoice FROM "agt_uc1_role_unreadable"')
        conn.commit()

        with pytest.raises(AgentRoleGrantError, match="v_ar_open"):
            assert_agent_role_can_read(conn, spec)
