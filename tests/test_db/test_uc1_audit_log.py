"""UC1-32 (#144) — `src.policy.audit`, run for real against a freshly
migrated database so `audit_log`'s own hash-chain trigger (schema.sql §6,
ported by `a4c2c2363959_uc1_ar_data_surface`) computes real hashes, not a
mock of one — the same reasoning `test_uc1_roles.py` already gives for using
`migrated_database_url` instead of a fake cursor.
"""

import os

import psycopg
import pytest

from src.agentspec.spec import AgentSpec, Scope
from src.config import get_settings
from src.policy.audit import audit_sink, record_decision
from src.policy.pep import Action, AuthzRequest, Decision, Effect, Principal, Provenance, Session
from tests.test_agentspec.conftest import spec_payload
from tests.test_db.conftest import upgrade_to_head


@pytest.fixture
def migrated_database_url(empty_database_url: str) -> str:
    upgrade_to_head(empty_database_url)
    return empty_database_url


def _spec() -> AgentSpec:
    payload = spec_payload(metadata={**spec_payload()["metadata"], "id": "agt_uc1_audit", "status": "deployed"})
    return AgentSpec.model_validate(payload)


def _request(*, tool: str = "erp.query_receivables", action_hash: str = "hash-1", args: dict | None = None) -> AuthzRequest:
    return AuthzRequest(
        spec=_spec(),
        principal=Principal(sub="u1", employee_id="E102", roles=["sales_rep"], department="SALES"),
        action=Action(
            hash=action_hash, tool=tool, scope=Scope.READ, mode="execute", connection_id="conn_erp", args=args or {}
        ),
        provenance=Provenance(from_untrusted_data=False),
        session=Session(tool_calls_used=0),
    )


def _decision(*, effect: Effect = Effect.ALLOW, reasons: list[str] | None = None) -> Decision:
    return Decision(
        effect=effect, reasons=reasons or [], row_filter={}, approver_roles=[], redact_fields=[], max_rows=100
    )


def _rows(conn: psycopg.Connection) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id, principal_id, tool, effect, reasons, action_hash, payload FROM audit_log ORDER BY seq"
        )
        return cur.fetchall()


def test_a_decision_is_recorded_with_every_documented_field(migrated_database_url: str) -> None:
    """§14's own minimal record: agent_id/principal_id/tool/effect/reasons/action_hash."""
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        record_decision(
            conn, "req-1", _request(action_hash="abc123", args={"days_overdue_gt": 30}), _decision(effect=Effect.DENY, reasons=["r1"])
        )
        rows = _rows(conn)

    assert len(rows) == 1
    agent_id, principal_id, tool, effect, reasons, action_hash, payload = rows[0]
    assert agent_id == "agt_uc1_audit"
    assert principal_id == "E102"
    assert tool == "erp.query_receivables"
    assert effect == "deny"
    assert reasons == ["r1"]
    assert action_hash == "abc123"
    assert payload == {"request_id": "req-1", "args": {"days_overdue_gt": 30}}


def test_every_effect_value_is_accepted(migrated_database_url: str) -> None:
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        for effect in Effect:
            record_decision(conn, f"req-{effect.value}", _request(action_hash=f"hash-{effect.value}"), _decision(effect=effect))
        rows = _rows(conn)

    assert {row[3] for row in rows} == {"allow", "deny", "require_approval"}


def test_v_audit_integrity_is_empty_for_an_untampered_chain(migrated_database_url: str) -> None:
    """AC-7, first half: a normally-written chain has zero broken links."""
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        for i in range(5):
            record_decision(conn, f"req-{i}", _request(action_hash=f"hash-{i}"), _decision())

        with conn.cursor() as cur:
            cur.execute("SELECT * FROM v_audit_integrity")
            assert cur.fetchall() == []


def test_v_audit_integrity_surfaces_a_hand_edited_row(migrated_database_url: str) -> None:
    """AC-7, second half: "sửa tay một dòng audit_log rồi query lại → trả ra đúng dòng gãy"."""
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        for i in range(3):
            record_decision(conn, f"req-{i}", _request(action_hash=f"hash-{i}"), _decision())

        with conn.cursor() as cur:
            cur.execute("SELECT seq FROM audit_log ORDER BY seq")
            seqs = [row[0] for row in cur.fetchall()]
            tampered_seq = seqs[1]
            # audit_no_update makes UPDATE a no-op — this module's own claim is
            # tamper *evidence*, not tamper prevention, so bypass the RULE the
            # honest way an attacker with table ownership would: rewrite the
            # column the RULE does not protect against a direct catalog write.
            # `v_audit_integrity` (schema.sql §6) only ever compares stored
            # `prev_hash`/`entry_hash` pointers to each other — it never
            # recomputes a hash from a row's own content — so the column that
            # actually has to change to trip it is `entry_hash` itself, the
            # one the *next* row's `prev_hash` was captured from at insert time.
            cur.execute("ALTER TABLE audit_log DISABLE RULE audit_no_update")
            cur.execute("UPDATE audit_log SET entry_hash = 'tampered' WHERE seq = %s", (tampered_seq,))
            cur.execute("ALTER TABLE audit_log ENABLE RULE audit_no_update")

            cur.execute("SELECT seq FROM v_audit_integrity")
            broken = cur.fetchall()

    assert broken == [(tampered_seq + 1,)]  # the *next* row's prev_hash no longer matches


def test_record_decision_never_supplies_prev_hash_or_entry_hash(migrated_database_url: str) -> None:
    """The trigger owns both columns — a caller-supplied value would only ever
    be silently overwritten, so this proves the insert never pretends to.
    """
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        record_decision(conn, "req-1", _request(), _decision())
        with conn.cursor() as cur:
            cur.execute("SELECT prev_hash, entry_hash FROM audit_log")
            prev_hash, entry_hash = cur.fetchone()

    assert prev_hash == "0" * 64
    assert len(entry_hash) == 64


def test_the_production_sink_reads_settings_fresh_per_call(migrated_database_url: str) -> None:
    """`audit_sink()` must read `get_settings().database_url` at call time, not
    at factory time — otherwise a caller built once at process start (every
    real call site's own shape) could never be pointed at a different
    database than whichever was current the moment it was constructed.
    """
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = migrated_database_url
    get_settings.cache_clear()
    try:
        sink = audit_sink()
        sink("req-1", _request(), _decision())
    finally:
        if previous is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()

    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        assert len(_rows(conn)) == 1
