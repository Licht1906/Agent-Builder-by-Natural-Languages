"""UC1-21 (#137) + UC1-23 (#139) — `POST /approvals/actions/{action_hash}/decision`
and `GET /approvals/actions/queue`, spec.md §19.

`_approval_binding_pep` is a small, faithful stand-in for `authz.rego`'s own
`approval_granted` rule (hash match + approver ≠ requester + role match) — the
real Rego rule has its own coverage in `authz_test.rego`; what this suite
proves is that the HTTP layer submits `approver_sub` from the *caller's own*
JWT (never the client body), and that a self-approval is refused by *both*
layers independently — the PEP is genuinely asked (`_approval_binding_pep`'s
own `seen` list proves it was reached) and denies on its own terms, with the
API's identity check as a backstop rather than the only reason it fails.
"""

import smtplib
from typing import Any

import pytest
from httpx import AsyncClient

from src.auth.passwords import hash_password
from src.auth.tokens import create_access_token
from src.compiler.tools import hash_action
from src.config import get_settings
from src.db.models import UC1AgentSpecVersion, UC1PendingAction, User, UserRole
from src.main import app
from src.policy.pep import AuthzRequest, Decision, Effect, get_pep_client
from src.services.vault_secret_store import get_vault_store
from tests.conftest import auth_header
from tests.test_agentspec.conftest import spec_payload
from tests.test_api.test_connection_routes import _FakeVault

pytestmark = pytest.mark.asyncio


def _approval_binding_pep():
    seen: list[AuthzRequest] = []

    def _client(request: AuthzRequest) -> Decision:
        seen.append(request)
        granted = any(
            a.tool == request.action.tool and a.action_hash == request.action.hash and a.approver_sub != request.principal.sub
            for a in request.approvals
        )
        effect = Effect.ALLOW if granted else Effect.REQUIRE_APPROVAL
        return Decision(effect=effect, reasons=[], row_filter={}, approver_roles=["sales_manager"], redact_fields=[], max_rows=0)

    _client.seen = seen  # type: ignore[attr-defined]
    return _client


@pytest.fixture(autouse=True)
def _fake_vault_and_pep():
    pep = _approval_binding_pep()
    app.dependency_overrides[get_vault_store] = lambda: _FakeVault()
    app.dependency_overrides[get_pep_client] = lambda: pep
    yield pep
    app.dependency_overrides.pop(get_vault_store, None)
    app.dependency_overrides.pop(get_pep_client, None)


def _seed_spec_version(db_session, *, created_by_id: int, agent_id: str = "agt_ar_assistant", version: int = 1) -> None:
    payload = spec_payload(
        metadata={**spec_payload()["metadata"], "id": agent_id, "version": version},
        capabilities=[{"tool": "mail.send_reminder", "scope": "write", "requires_approval": True}],
        authority={"autonomy": "propose_then_act", "budget": {"max_tool_calls_per_session": 40}},
    )
    db_session.add(UC1AgentSpecVersion(agent_id=agent_id, version=version, spec=payload, created_by_id=created_by_id))
    db_session.commit()


def _make_user(db_session, *, role: UserRole, employee_id: str, department: str) -> tuple[User, str]:
    user = User(
        email=f"{employee_id.lower()}@example.com",
        password_hash=hash_password("a-long-enough-password"),
        role=role,
        employee_id=employee_id,
        department=department,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user, create_access_token(user)


def _seed_pending_action(
    db_session, *, requester: User, args: dict[str, Any], agent_id: str = "agt_ar_assistant", version: int = 1
) -> UC1PendingAction:
    row = UC1PendingAction(
        agent_id=agent_id,
        version=version,
        tool="mail.send_reminder",
        args=args,
        action_hash=hash_action("mail.send_reminder", args),
        connection_id="conn_mail",
        approver_roles=["sales_manager"],
        requested_by_id=requester.id,
        requested_by_sub=str(requester.id),
        requested_by_employee_id=requester.employee_id,
        requested_by_department=requester.department,
        requested_by_roles=[requester.role.value],
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


async def test_approving_sends_the_real_reminder_and_marks_the_row_approved(
    client: AsyncClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = "agt_action_approve"
    # Not E102: that id belongs to the seeded `sales.rep@demo.local` persona
    # (`src.auth.seed.seed_sales_demo_rows`) and `users.employee_id` is unique.
    # Nothing in this test depends on *which* rep is asking — it is about the
    # approval flow — and the sibling tests below already each take their own
    # id, so this one takes the next free one rather than squatting on the
    # persona's. `test_uc1_demo_rehearsal.py` had to reclaim E102 by hand
    # precisely because this line used to hold it.
    requester, _ = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E107", department="SALES")
    approver, approver_token = _make_user(db_session, role=UserRole.SALES_MANAGER, employee_id="E900", department="SALES")
    _seed_spec_version(db_session, created_by_id=approver.id, agent_id=agent_id)
    row = _seed_pending_action(db_session, requester=requester, args={"customer_id": "cust-a", "amount_due": 500}, agent_id=agent_id)

    monkeypatch.setattr(get_settings(), "smtp_host", "mailhog.test")
    sent: dict[str, Any] = {}

    class _FakeSMTP:
        def __init__(self, host: str, port: int) -> None:
            sent["host"], sent["port"] = host, port

        def __enter__(self) -> "_FakeSMTP":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def send_message(self, message: object) -> None:
            sent["message"] = message

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    response = await client.post(
        f"/api/v1/approvals/actions/{row.action_hash}/decision",
        json={"decision": "approve"},
        headers=auth_header(approver_token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["result"]["sent"] is True
    assert "message" in sent, "the real SMTP send never ran"

    db_session.refresh(row)
    assert row.status.value == "approved"
    assert row.decided_by_id == approver.id


async def test_the_requester_may_not_approve_their_own_action(client: AsyncClient, db_session, _fake_vault_and_pep) -> None:
    """FR-46: refused by *both* layers, not the API's check alone — proven by
    asserting the PEP was actually reached (`pep.seen`), not merely that the
    HTTP response happened to be 403.
    """
    agent_id = "agt_action_self_approve"
    requester, requester_token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E103", department="SALES")
    _seed_spec_version(db_session, created_by_id=requester.id, agent_id=agent_id)
    row = _seed_pending_action(db_session, requester=requester, args={"customer_id": "cust-b"}, agent_id=agent_id)

    response = await client.post(
        f"/api/v1/approvals/actions/{row.action_hash}/decision",
        json={"decision": "approve"},
        headers=auth_header(requester_token),
    )

    assert response.status_code == 403, response.text
    assert _fake_vault_and_pep.seen, "the PEP was never asked — self-approval was refused by the API layer alone"
    assert _fake_vault_and_pep.seen[-1].principal.sub == _fake_vault_and_pep.seen[-1].approvals[0].approver_sub
    db_session.refresh(row)
    assert row.status.value == "pending"


async def test_rejecting_marks_the_row_rejected_and_never_touches_smtp(
    client: AsyncClient, db_session, smtp_that_must_never_be_reached: None
) -> None:
    agent_id = "agt_action_reject"
    requester, _ = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E104", department="SALES")
    approver, approver_token = _make_user(db_session, role=UserRole.SALES_MANAGER, employee_id="E901", department="SALES")
    _seed_spec_version(db_session, created_by_id=approver.id, agent_id=agent_id)
    row = _seed_pending_action(db_session, requester=requester, args={"customer_id": "cust-c"}, agent_id=agent_id)

    response = await client.post(
        f"/api/v1/approvals/actions/{row.action_hash}/decision",
        json={"decision": "reject", "reason": "không cần thiết"},
        headers=auth_header(approver_token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    db_session.refresh(row)
    assert row.status.value == "rejected"
    assert row.decision_reason == "không cần thiết"


async def test_editing_the_row_after_creation_invalidates_the_original_approval(
    client: AsyncClient, db_session, smtp_that_must_never_be_reached: None
) -> None:
    """FR-45: the approval a human would have seen was bound to the *original*
    `action_hash`. If `args` on the row itself were tampered with after that
    (simulated here with a direct DB write, standing in for whatever bug or
    attacker would have to reach the row to do this), the freshly recomputed
    hash no longer matches — `execute_tool_call` refuses, and the real SMTP
    implementation never runs.
    """
    agent_id = "agt_action_edited"
    requester, _ = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E105", department="SALES")
    approver, approver_token = _make_user(db_session, role=UserRole.SALES_MANAGER, employee_id="E902", department="SALES")
    _seed_spec_version(db_session, created_by_id=approver.id, agent_id=agent_id)
    row = _seed_pending_action(db_session, requester=requester, args={"customer_id": "cust-d", "amount_due": 100}, agent_id=agent_id)
    original_hash = row.action_hash

    row.args = {"customer_id": "cust-d", "amount_due": 999999}
    db_session.commit()

    response = await client.post(
        f"/api/v1/approvals/actions/{original_hash}/decision",
        json={"decision": "approve"},
        headers=auth_header(approver_token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    db_session.refresh(row)
    assert row.status.value == "pending"


async def test_the_queue_lists_pending_actions(client: AsyncClient, db_session) -> None:
    agent_id = "agt_action_queue"
    requester, requester_token = _make_user(db_session, role=UserRole.SALES_REP, employee_id="E106", department="SALES")
    _seed_spec_version(db_session, created_by_id=requester.id, agent_id=agent_id)
    _seed_pending_action(db_session, requester=requester, args={"customer_id": "cust-e"}, agent_id=agent_id)

    response = await client.get("/api/v1/approvals/actions/queue", headers=auth_header(requester_token))

    assert response.status_code == 200, response.text
    entries = response.json()
    assert any(e["tool"] == "mail.send_reminder" and e["args"]["customer_id"] == "cust-e" for e in entries)
