"""The `data_write` node in a real trial, through HTTP — ADR-0094.

A real Hệ CSDL (a base table and an employee view in the test Postgres), real
access rules with a `tables` grant, the real runtime and the real approval
gate; only the models are scripted. What is held: a write is shown on the
approval card exactly as it will run, nothing changes until a person approves
it, a trial mocks it unless switched to "Gửi thật", the asker's scope is bound
into it, an asker with no grant gets "không có quyền", and a graph with a path
to a write that skips the gate cannot be confirmed.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from src.config import get_settings
from src.datasources.access import AccessRules
from src.datasources.sources import (
    create_database_source,
    run_database_test,
    save_access_rules,
    set_allowed_views,
    set_allowed_write_tables,
)
from src.db.base import engine
from src.db.models import AgentVersion, DatabaseDialect, DatabaseSource, SecretStoreEntry, User, VersionStatus
from src.models.workflow import WorkflowJSON
from src.services.secret_store import EncryptedDbSecretStore
from src.workflow.validation import graph_problems
from tests.conftest import ScriptedRun, parse_sse
from tests.test_api.test_workflow_approval import answers, decide, trial_workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

TRUSTED = str(engine.url.host)

RULES = AccessRules.model_validate(
    {
        "identity": {
            "view": "v_dwn_staff",
            "match_by": "email",
            "attributes": {"employee_code": "emp_id", "position": "position"},
        },
        "rules": [
            {
                "position": "Nhân viên KD",
                "tables": {
                    "t_dwn_notes": {"operations": ["insert", "update"], "where": {"sales_rep_id": "employee_code"}}
                },
            },
            {"position": "Trưởng nhóm KD", "views": {"v_dwn_staff": "all"}},
        ],
    }
)

NOTE = {"table": "t_dwn_notes", "operation": "insert", "values": {"customer_id": "C1", "note": "hẹn trả 20/11"}}
ASKED = "Ghi lại giúp: khách C1 hẹn trả ngày 20/11"


@pytest.fixture(autouse=True)
def _trusted_test_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trial door's reader reads the setting; the test database is local, so it has to be trusted."""
    monkeypatch.setattr(get_settings(), "database_source_trusted_hosts", TRUSTED)


@pytest.fixture(autouse=True)
def _tables() -> Iterator[None]:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE OR REPLACE VIEW v_dwn_staff AS SELECT * FROM (VALUES "
                "('an@cty.vn', 'E102', 'Nhân viên KD'), ('binh@cty.vn', 'E200', 'Trưởng nhóm KD')"
                ") AS t(email, emp_id, position)"
            )
        )
        connection.execute(text("DROP TABLE IF EXISTS t_dwn_notes"))
        connection.execute(
            text(
                "CREATE TABLE t_dwn_notes (id serial PRIMARY KEY, customer_id text NOT NULL, "
                "sales_rep_id text, note text)"
            )
        )
        connection.execute(text("INSERT INTO t_dwn_notes (customer_id, sales_rep_id, note) VALUES ('C3', 'E200', 'c')"))
    yield
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW IF EXISTS v_dwn_staff"))
        connection.execute(text("DROP TABLE IF EXISTS t_dwn_notes"))


@pytest.fixture
def source(db_session: Session, seeded_creator: User) -> Iterator[DatabaseSource]:
    url: URL = engine.url.set(drivername="postgresql")
    store = EncryptedDbSecretStore(db=db_session)
    row = create_database_source(
        db_session,
        seeded_creator.id,
        name="ERP staging",
        dialect=DatabaseDialect.POSTGRESQL,
        dsn=url.render_as_string(hide_password=False),
        secret_store=store,
    )
    run_database_test(db_session, row, secret_store=store, trusted_hosts=frozenset({TRUSTED}))
    set_allowed_views(db_session, row, ["v_dwn_staff"])
    set_allowed_write_tables(db_session, row, ["t_dwn_notes"])
    save_access_rules(db_session, row, RULES)
    yield row
    db_session.delete(row)
    db_session.query(SecretStoreEntry).filter(SecretStoreEntry.key == row.dsn_secret_ref).delete()
    db_session.commit()


def _workflow(source_id: str, *, gated: bool = True) -> dict[str, Any]:
    """`HumanApproval → DataWrite → AgentLlm → End`, the rejection reported by its own `AgentLlm`."""
    write = {
        "id": "write",
        "type": "data_write",
        "config": {"source_id": source_id, "tables": ["t_dwn_notes"]},
    }
    report = {"id": "report", "type": "agent_llm", "config": {"prompt": "Báo lại.", "model": "gpt-4o-mini"}}
    finish = {"id": "finish", "type": "end", "config": {}}
    if not gated:
        return {
            "start_node_id": "write",
            "nodes": [write, report, finish],
            "edges": [
                {"id": "e1", "source_node_id": "write", "source_port": "done", "target_node_id": "report"},
                {"id": "e2", "source_node_id": "report", "source_port": "done", "target_node_id": "finish"},
            ],
            "metadata": {},
        }
    return {
        "start_node_id": "gate",
        "nodes": [
            {
                "id": "gate",
                "type": "human_approval",
                "config": {"approver_role": "nhân viên", "message_template": "Duyệt thay đổi dữ liệu"},
            },
            write,
            report,
            {"id": "refused", "type": "agent_llm", "config": {"prompt": "Nói chưa ghi.", "model": "gpt-4o-mini"}},
            finish,
        ],
        "edges": [
            {"id": "e1", "source_node_id": "gate", "source_port": "approved", "target_node_id": "write"},
            {"id": "e2", "source_node_id": "gate", "source_port": "rejected", "target_node_id": "refused"},
            {"id": "e3", "source_node_id": "write", "source_port": "done", "target_node_id": "report"},
            {"id": "e4", "source_node_id": "report", "source_port": "done", "target_node_id": "finish"},
            {"id": "e5", "source_node_id": "refused", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {},
    }


SPEC: dict[str, Any] = {
    "goal": "Ghi chú công nợ",
    "users": ["Nhân viên kinh doanh"],
    "data": [{"name": "ERP", "kind": "database", "ref": None}],
    "actions": ["Ghi lại ngày khách hẹn trả"],
    "expected_outcome": ["Ghi chú được lưu"],
    "success_criteria": [{"text": "Ghi đúng khách", "category": "correctness"}],
    "prohibitions": ["Không sửa khách của người khác"],
}


@pytest.fixture
def version(
    solution_spec_version: Callable[..., AgentVersion], seeded_creator: User, source: DatabaseSource
) -> AgentVersion:
    return solution_spec_version(
        seeded_creator, SPEC, status=VersionStatus.WORKFLOW_CONFIRMED, workflow=_workflow(str(source.id))
    )


def _writes(write: dict[str, Any]) -> AIMessage:
    """The write-preparing model's reply: one `data_write` tool call."""
    return AIMessage(content="", tool_calls=[{"name": "data_write", "args": write, "id": "w1", "type": "tool_call"}])


def _notes() -> list[tuple[str, str | None, str | None]]:
    with engine.begin() as connection:
        rows = connection.execute(text("SELECT customer_id, sales_rep_id, note FROM t_dwn_notes ORDER BY id"))
        return [tuple(row) for row in rows]


def _write_lines(response: Any, event: str) -> list[dict[str, Any]]:
    return [line for line in parse_sse(response.text, event=event) if line["tool"] == "data_write"]


async def _paused(
    client: AsyncClient,
    version: AgentVersion,
    token: str,
    scripted: Callable[..., ScriptedRun],
    *,
    write: dict[str, Any] = NOTE,
    run_as: str = "an@cty.vn",
    **body: Any,
) -> dict[str, Any]:
    scripted([[_writes(write)]])
    response = await trial_workflow(client, version, ASKED, token, run_as=run_as, **body)
    assert response.status_code == 200, response.text
    [card] = parse_sse(response.text, event="awaiting_approval")
    return card


async def test_the_card_shows_the_write_exactly_and_nothing_changes_before_a_decision(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    scripted_chat_model_factory: Callable[..., ScriptedRun],
) -> None:
    card = await _paused(client, version, creator_token, scripted_chat_model_factory)

    [action] = card["actions"]
    assert (action["node_id"], action["tool"], action["action"]) == ("write", "data_write", "insert")
    assert action["args"]["table"] == "t_dwn_notes"
    assert action["args"]["values"] == NOTE["values"]
    assert _notes() == [("C3", "E200", "c")]


async def test_approved_in_a_trial_the_write_is_mocked_unless_sent_for_real(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    scripted_chat_model_factory: Callable[..., ScriptedRun],
) -> None:
    card = await _paused(client, version, creator_token, scripted_chat_model_factory)

    scripted_chat_model_factory([[answers("Đã ghi (mô phỏng).")]])
    resumed = await decide(client, version, card["approval_id"], creator_token, approved=True)

    assert resumed.status_code == 200, resumed.text
    [result] = _write_lines(resumed, "tool_result")
    assert (result["ok"], result["simulated"]) == (True, True)
    assert "chưa ghi thật" in result["result"]
    assert _notes() == [("C3", "E200", "c")]


async def test_sent_for_real_the_approved_write_lands_with_the_askers_scope(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    scripted_chat_model_factory: Callable[..., ScriptedRun],
) -> None:
    card = await _paused(client, version, creator_token, scripted_chat_model_factory, send_for_real=True)

    scripted_chat_model_factory([[answers("Đã ghi.")]])
    resumed = await decide(client, version, card["approval_id"], creator_token, approved=True)

    [result] = _write_lines(resumed, "tool_result")
    assert (result["ok"], result["simulated"], result["live_in_sandbox"]) == (True, False, True)
    assert "1 dòng thay đổi" in result["result"]
    # The model never named a rep; the asker's own code was written in.
    assert _notes() == [("C3", "E200", "c"), ("C1", "E102", "hẹn trả 20/11")]


async def test_rejected_nothing_is_written(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    scripted_chat_model_factory: Callable[..., ScriptedRun],
) -> None:
    card = await _paused(client, version, creator_token, scripted_chat_model_factory, send_for_real=True)

    scripted_chat_model_factory([[answers("Chưa ghi vì bị từ chối.")]])
    resumed = await decide(client, version, card["approval_id"], creator_token, approved=False, reason="sai ngày")

    assert resumed.status_code == 200, resumed.text
    assert _write_lines(resumed, "tool") == []
    assert _notes() == [("C3", "E200", "c")]


async def test_an_update_of_another_reps_row_changes_nothing(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    scripted_chat_model_factory: Callable[..., ScriptedRun],
) -> None:
    """The scope is ANDed in: C3 is E200's, so the rep's update finds no row of theirs to change."""
    update = {
        "table": "t_dwn_notes",
        "operation": "update",
        "values": {"note": "đã gọi"},
        "where": [{"column": "customer_id", "value": "C3"}],
    }
    card = await _paused(client, version, creator_token, scripted_chat_model_factory, write=update, send_for_real=True)

    scripted_chat_model_factory([[answers("Không có gì thay đổi.")]])
    resumed = await decide(client, version, card["approval_id"], creator_token, approved=True)

    [result] = _write_lines(resumed, "tool_result")
    assert "Không có dòng nào khớp" in result["result"]
    assert _notes() == [("C3", "E200", "c")]


async def test_a_write_moving_a_row_out_of_the_askers_scope_is_never_put_to_a_person(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    scripted_chat_model_factory: Callable[..., ScriptedRun],
) -> None:
    hijack = {"table": "t_dwn_notes", "operation": "insert", "values": {"customer_id": "C9", "sales_rep_id": "E200"}}
    card = await _paused(client, version, creator_token, scripted_chat_model_factory, write=hijack)

    assert card["actions"] == []


async def test_an_asker_with_no_table_grant_is_told_no_permission(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    scripted_chat_model_factory: Callable[..., ScriptedRun],
) -> None:
    card = await _paused(
        client, version, creator_token, scripted_chat_model_factory, run_as="binh@cty.vn", send_for_real=True
    )
    assert card["actions"] == []

    scripted_chat_model_factory([[_writes(NOTE)], [answers("Bạn không có quyền.")]])
    resumed = await decide(client, version, card["approval_id"], creator_token, approved=True)

    [result] = _write_lines(resumed, "tool_result")
    assert result["ok"] is False
    assert "không có quyền thay đổi" in result["result"]
    assert _notes() == [("C3", "E200", "c")]


async def test_a_graph_with_a_path_to_a_write_that_skips_approval_cannot_be_confirmed() -> None:
    exposed = graph_problems(WorkflowJSON.model_validate(_workflow("7", gated=False)))
    assert any("data_write" in problem.message and problem.node_id == "write" for problem in exposed)
    guarded = graph_problems(WorkflowJSON.model_validate(_workflow("7")))
    assert not any(problem.node_id == "write" for problem in guarded)
