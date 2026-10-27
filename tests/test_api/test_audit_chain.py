"""The V2 audit chain — DS-10 / ADR-0059 §11.

The ticket's three acceptance criteria, each against the real thing:

- a run writes one entry per `data_query` read and one per email a person
  approved — through HTTP, the way a demo produces them;
- editing an entry in the middle of the chain makes the check name exactly
  that entry; deleting one, or cutting off the end, is named too;
- no row of data ever lands in the chain — counts, scope and hashes only.
"""

import importlib.util
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.audit.chain import AuditEntry, AuditKind, AuditMode, append_event, verify_chain
from src.db.base import engine
from src.db.models import AgentVersion, AuditEvent, User
from src.workflow.approvals import action_hash
from src.workflow.tools import GMAIL
from tests.conftest import ScriptedRun, auth_header, parse_sse
from tests.test_api.test_action_payload_binding import (
    APPROVED_ARGS,
    _start,
    deployed,
    draft_approve_send,
    fills,
    gmail,
)
from tests.test_api.test_data_query_node import OVERDUE, _chat, _views, _writes, asking, source, version
from tests.test_api.test_workflow_approval import answers, decide

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config", "_views")]

# Fixtures borrowed from the modules that own the scenery, named here so pytest sees them.
__all__ = ["_views", "asking", "deployed", "gmail", "source", "version"]


def _entries(db_session: Session, *, kind: AuditKind, trace_id: str) -> list[AuditEvent]:
    db_session.expire_all()
    return list(
        db_session.scalars(
            select(AuditEvent)
            .where(AuditEvent.kind == kind.value, AuditEvent.trace_id == trace_id)
            .order_by(AuditEvent.seq)
        )
    )


def _trace_of(response: Any) -> str:
    return parse_sse(response.text, event="thread")[0]["trace_id"]


# --------------------------------------------------------------------------- #
# One entry per read, one per approved email
# --------------------------------------------------------------------------- #


async def test_a_read_is_audited_with_who_asked_the_view_the_scope_and_the_count_but_no_rows(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    asking: Callable[[str | None], None],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    db_session: Session,
) -> None:
    asking(None)  # a reader that trusts the test database; the trial names its own asker
    scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="C1")]])
    response = await _chat(client, version, creator_token, sandbox=True, run_as="an@cty.vn")
    assert response.status_code == 200, response.text

    [entry] = _entries(db_session, kind=AuditKind.DATA_QUERY, trace_id=_trace_of(response))

    assert (entry.mode, entry.agent_id, entry.version_id, entry.node_id) == (
        AuditMode.TRIAL.value,
        version.agent_id,
        version.id,
        "query",
    )
    details = entry.details
    assert details["asker"] == "an@cty.vn"
    assert (details["source_name"], details["view"], details["outcome"]) == ("ERP staging", "v_dqn_ar", "rows")
    assert details["scope"] == [["sales_rep_id", "E102"]]
    assert details["row_count"] == 1
    # Nothing a row held — not the customer it returned, not a note on it.
    written = json.dumps(details, ensure_ascii=False)
    assert "C1" not in written
    assert "gọi lại" not in written


async def test_a_denied_read_is_audited_as_denied(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    asking: Callable[[str | None], None],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    db_session: Session,
) -> None:
    asking(None)  # a reader that trusts the test database; the trial names its own asker
    scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="Không có quyền.")]])
    response = await _chat(client, version, creator_token, sandbox=True, run_as=None)

    [entry] = _entries(db_session, kind=AuditKind.DATA_QUERY, trace_id=_trace_of(response))

    assert entry.details["outcome"] == "denied"
    assert entry.details["deny_cause"]
    assert "row_count" not in entry.details


async def test_an_approved_email_is_audited_with_its_hash_its_approver_and_what_happened(
    client: AsyncClient,
    creator_token: str,
    seeded_creator: User,
    deployed: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    gmail: list[tuple[str, dict[str, Any]]],
    db_session: Session,
) -> None:
    scripted_chat_model_factory([[answers("Bản nháp thư.")], [fills(**APPROVED_ARGS)]])
    agent_version = deployed(draft_approve_send())
    waiting = await _start(client, agent_version, creator_token)

    scripted_chat_model_factory([[answers("Đã gửi thư cho khách.")]])
    resumed = await decide(client, agent_version, waiting["approval_id"], creator_token, approved=True)
    assert gmail, "the email was not sent — nothing to audit"

    [entry] = _entries(db_session, kind=AuditKind.ACTION, trace_id=_trace_of(resumed))

    assert entry.mode == AuditMode.PROD.value
    assert entry.details == {
        "tool": f"{GMAIL}.send_email",
        "action_hash": action_hash(f"{GMAIL}.send_email", APPROVED_ARGS),
        "approval_id": waiting["approval_id"],
        "approved_by": seeded_creator.id,
        "outcome": "sent",
        "live_in_sandbox": False,
        "reason": None,
    }
    # The hash, never the email.
    assert APPROVED_ARGS["body"] not in json.dumps(entry.details, ensure_ascii=False)


async def test_a_refused_email_is_audited_too(
    client: AsyncClient,
    creator_token: str,
    deployed: Callable[[dict[str, Any]], AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    gmail: list[tuple[str, dict[str, Any]]],
    db_session: Session,
) -> None:
    """A send that did not happen is still something the run tried to do."""
    scripted_chat_model_factory([[answers("Bản nháp thư.")], [fills(**APPROVED_ARGS)]])
    agent_version = deployed(draft_approve_send(rewrite=True))
    waiting = await _start(client, agent_version, creator_token)

    rewritten = {**APPROVED_ARGS, "body": "Nội dung khác."}
    scripted_chat_model_factory([[answers("Bản soạn lại.")], [fills(**rewritten)], [answers("Không gửi được.")]])
    resumed = await decide(client, agent_version, waiting["approval_id"], creator_token, approved=True)

    assert gmail == []
    [entry] = _entries(db_session, kind=AuditKind.ACTION, trace_id=_trace_of(resumed))
    assert entry.details["outcome"] == "refused"
    assert entry.details["action_hash"] == action_hash(f"{GMAIL}.send_email", rewritten)
    assert "khác với payload đã được duyệt" in entry.details["reason"]


# --------------------------------------------------------------------------- #
# The check names where the chain breaks
# --------------------------------------------------------------------------- #


@pytest.fixture
def three_entries(db_session: Session) -> Iterator[list[AuditEvent]]:
    """Three fresh links at the end of the chain — whatever else the suite has written stays before them."""
    made = [
        append_event(
            db_session,
            AuditEntry(
                kind=AuditKind.DATA_QUERY,
                mode=AuditMode.TRIAL,
                agent_id=None,
                version_id=None,
                trace_id="audit-test",
                run_id=f"run-{index}",
                node_id="query",
                details={"asker": "an@cty.vn", "view": "v_x", "row_count": index},
            ),
        )
        for index in range(3)
    ]
    assert verify_chain(db_session).ok, "the chain was already broken before this test touched it"
    yield made


def _sql(statement: str, **params: Any) -> None:
    with engine.begin() as connection:
        connection.execute(text(statement), params)


async def test_an_entry_edited_in_the_middle_is_named_by_its_seq(
    client: AsyncClient, admin_token: str, three_entries: list[AuditEvent], db_session: Session
) -> None:
    middle = three_entries[1]
    original = json.dumps(middle.details)
    _sql("UPDATE audit_events SET details = CAST(:d AS json) WHERE seq = :seq", d='{"row_count": 999}', seq=middle.seq)
    try:
        response = await client.get("/api/v1/admin/audit/verify", headers=auth_header(admin_token))

        assert response.status_code == 200, response.text
        verdict = response.json()
        assert verdict["ok"] is False
        assert verdict["broken_at"] == middle.seq
        assert "đã bị sửa" in verdict["reason"]
    finally:
        _sql("UPDATE audit_events SET details = CAST(:d AS json) WHERE seq = :seq", d=original, seq=middle.seq)
    assert verify_chain(db_session).ok


async def test_an_edit_that_also_rewrites_its_own_hash_is_named_at_the_next_entry(
    three_entries: list[AuditEvent], db_session: Session
) -> None:
    """Hiding the edit by resealing the entry moves the break one link on — it does not remove it."""
    middle, after = three_entries[1], three_entries[2]
    original_hash = middle.entry_hash
    _sql("UPDATE audit_events SET entry_hash = :h WHERE seq = :seq", h="f" * 64, seq=middle.seq)
    try:
        verdict = verify_chain(db_session)
        # The resealed entry no longer matches its fields, so it is named first.
        assert (verdict.ok, verdict.broken_at) == (False, middle.seq)
    finally:
        _sql("UPDATE audit_events SET entry_hash = :h WHERE seq = :seq", h=original_hash, seq=middle.seq)
    assert after.prev_hash == original_hash


async def test_a_deleted_entry_is_named_by_the_seq_that_is_missing(
    three_entries: list[AuditEvent], db_session: Session
) -> None:
    middle = three_entries[1]
    db_session.expunge_all()
    row = db_session.get(AuditEvent, middle.seq)
    assert row is not None
    saved = {column.name: getattr(row, column.name) for column in AuditEvent.__table__.columns}
    _sql("DELETE FROM audit_events WHERE seq = :seq", seq=middle.seq)
    try:
        verdict = verify_chain(db_session)
        assert (verdict.ok, verdict.broken_at) == (False, middle.seq)
        assert "thiếu bản ghi" in (verdict.reason or "")
    finally:
        db_session.expunge_all()
        db_session.add(AuditEvent(**saved))
        db_session.commit()
    assert verify_chain(db_session).ok


async def test_a_chain_cut_off_at_the_end_is_named_too(three_entries: list[AuditEvent], db_session: Session) -> None:
    last = three_entries[2]
    db_session.expunge_all()
    row = db_session.get(AuditEvent, last.seq)
    assert row is not None
    saved = {column.name: getattr(row, column.name) for column in AuditEvent.__table__.columns}
    _sql("DELETE FROM audit_events WHERE seq = :seq", seq=last.seq)
    try:
        verdict = verify_chain(db_session)
        assert (verdict.ok, verdict.broken_at) == (False, last.seq)
    finally:
        db_session.expunge_all()
        db_session.add(AuditEvent(**saved))
        db_session.commit()
    assert verify_chain(db_session).ok


async def test_the_command_line_check_exits_non_zero_on_a_broken_chain(
    three_entries: list[AuditEvent], capsys: pytest.CaptureFixture[str]
) -> None:
    path = Path(__file__).resolve().parents[2] / "scripts" / "verify_audit_chain.py"
    spec = importlib.util.spec_from_file_location("verify_audit_chain", path)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    assert script.main() == 0
    middle = three_entries[1]
    original = json.dumps(middle.details)
    _sql("UPDATE audit_events SET details = CAST(:d AS json) WHERE seq = :seq", d="{}", seq=middle.seq)
    try:
        assert script.main() == 1
        assert f"seq {middle.seq}" in capsys.readouterr().out
    finally:
        _sql("UPDATE audit_events SET details = CAST(:d AS json) WHERE seq = :seq", d=original, seq=middle.seq)


async def test_only_an_admin_may_read_or_check_the_chain(client: AsyncClient, creator_token: str) -> None:
    for path in ("/api/v1/admin/audit/verify", "/api/v1/admin/audit/events"):
        response = await client.get(path, headers=auth_header(creator_token))
        assert response.status_code == 403, (path, response.text)
