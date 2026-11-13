"""The exported package changing its company's database — ADR-0094.

A real package, a real base table in the test Postgres. In the package a run
is production: once the asker approves the exact write on its card, the row
lands — scoped to them — and a rejection leaves the table alone. A table the
workflow writes that the real database does not have stops the start, and a
`write_dsn_secret` is the account the change goes through.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import text

from src.db.base import engine
from src.standalone.app import create_app
from src.standalone.config import RuntimeConfigError
from src.standalone.package import Package, prepare_database
from tests.conftest import ScriptedRun, parse_sse
from tests.test_standalone.test_standalone_runtime import (
    ACCESS,
    PREFIX,
    SOURCE_ID,
    _loaded,
    _platform_factory,
    _runtime,
    _views,
    package_dir,
    secrets_env,
)

pytestmark = pytest.mark.asyncio

# Scenery borrowed from `test_standalone_runtime`, named so pytest sees it.
__all__ = ["_views", "package_dir", "secrets_env"]

WRITING_ACCESS = {
    **ACCESS,
    "rules": [
        {
            "position": "Nhân viên KD",
            "views": {"v_sa_ar": {"where": {"sales_rep_id": "employee_code"}}},
            "tables": {"t_sa_notes": {"operations": ["insert"], "where": {"sales_rep_id": "employee_code"}}},
        }
    ],
}
NOTE = {"table": "t_sa_notes", "operation": "insert", "values": {"customer_id": "C1", "note": "hen tra 20/11"}}
ASKER = {"X-Asker": "an@cty.vn"}


@pytest.fixture(autouse=True)
def _notes() -> Iterator[None]:
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS t_sa_notes"))
        connection.execute(text("CREATE TABLE t_sa_notes (customer_id text NOT NULL, sales_rep_id text, note text)"))
    yield
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS t_sa_notes"))


def _workflow(table: str = "t_sa_notes") -> dict[str, Any]:
    """`human_approval → data_write → report → end`, the rejection reported too."""
    return {
        "start_node_id": "gate",
        "nodes": [
            {
                "id": "gate",
                "type": "human_approval",
                "config": {"approver_role": "nhân viên", "message_template": "Duyệt thay đổi dữ liệu"},
            },
            {"id": "write", "type": "data_write", "config": {"source_id": SOURCE_ID, "tables": [table]}},
            {"id": "report", "type": "agent_llm", "config": {"prompt": "Báo lại.", "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "gate", "source_port": "approved", "target_node_id": "write"},
            {"id": "e2", "source_node_id": "gate", "source_port": "rejected", "target_node_id": "report"},
            {"id": "e3", "source_node_id": "write", "source_port": "done", "target_node_id": "report"},
            {"id": "e4", "source_node_id": "report", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {},
    }


def _package(package_dir: Callable[..., Path], **runtime: Any) -> Package:
    directory = package_dir(workflow=_workflow(), **runtime)
    (directory / "access.yaml").write_text(yaml.safe_dump(WRITING_ACCESS, allow_unicode=True), encoding="utf-8")
    return _loaded(directory)


def _rows() -> list[tuple[Any, ...]]:
    with engine.begin() as connection:
        found = connection.execute(text("SELECT customer_id, sales_rep_id, note FROM t_sa_notes"))
        return [tuple(row) for row in found]


async def _paused(
    package: Package, scripted: Callable[[list[list[AIMessage]]], ScriptedRun]
) -> tuple[AsyncClient, int]:
    app = create_app(package, model_factory=_platform_factory())
    app.state.ids = prepare_database(package)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://package")
    call = {"name": "data_write", "args": NOTE, "id": "w1", "type": "tool_call"}
    scripted([[AIMessage(content="", tool_calls=[call])]])
    paused = await client.post("/chat", json={"message": "ghi lại: C1 hẹn trả 20/11"}, headers=ASKER)
    [event] = parse_sse(paused.text, event="awaiting_approval")
    [action] = event["actions"]
    assert (action["tool"], action["args"]["values"]) == ("data_write", NOTE["values"])
    return client, event["approval_id"]


def _write_result(response: Any) -> dict[str, Any]:
    [result] = [line for line in parse_sse(response.text, event="tool_result") if line["tool"] == "data_write"]
    return result


async def test_an_approved_write_lands_in_the_companys_database_scoped_to_the_asker(
    package_dir: Callable[..., Path],
    secrets_env: None,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    client, approval_id = await _paused(_package(package_dir), scripted_chat_model_factory)
    assert _rows() == [], "nothing may change before the decision"

    scripted_chat_model_factory([[AIMessage(content="Đã ghi.")]])
    resumed = await client.post(f"/approvals/{approval_id}", json={"approved": True}, headers=ASKER)
    await client.aclose()

    assert resumed.status_code == 200, resumed.text
    result = _write_result(resumed)
    assert (result["ok"], result["simulated"]) == (True, False)
    assert _rows() == [("C1", "E102", "hen tra 20/11")]


async def test_a_rejected_write_changes_nothing(
    package_dir: Callable[..., Path],
    secrets_env: None,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    client, approval_id = await _paused(_package(package_dir), scripted_chat_model_factory)

    scripted_chat_model_factory([[AIMessage(content="Chưa ghi.")]])
    resumed = await client.post(
        f"/approvals/{approval_id}", json={"approved": False, "reason": "sai ngày"}, headers=ASKER
    )
    await client.aclose()

    assert resumed.status_code == 200, resumed.text
    assert _rows() == []


async def test_a_table_the_workflow_writes_that_the_database_lacks_stops_the_start(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    directory = package_dir(workflow=_workflow("t_sa_missing"))

    with pytest.raises(RuntimeConfigError, match="không có bảng t_sa_missing"):
        _loaded(directory)


async def test_the_write_account_is_what_a_change_goes_through(
    package_dir: Callable[..., Path],
    secrets_env: None,
    monkeypatch: pytest.MonkeyPatch,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """A write DSN nobody can sign in with: the card still shows, the change itself is refused."""
    broken = engine.url.set(drivername="postgresql", password="wrong-password").render_as_string(hide_password=False)
    monkeypatch.setenv(f"{PREFIX}ERP_WRITE_DSN", broken)
    sources = [{**_runtime()["data_sources"][0], "write_dsn_secret": "erp_write_dsn"}]
    client, approval_id = await _paused(_package(package_dir, data_sources=sources), scripted_chat_model_factory)

    scripted_chat_model_factory([[AIMessage(content="Chưa ghi được.")]])
    resumed = await client.post(f"/approvals/{approval_id}", json={"approved": True}, headers=ASKER)
    await client.aclose()

    assert _write_result(resumed)["ok"] is False
    assert _rows() == []
