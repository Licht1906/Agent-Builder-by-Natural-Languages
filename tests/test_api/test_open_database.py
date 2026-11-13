"""A Hệ CSDL with nothing to keep apart, and the description a creator writes for the agent — ADR-0095.

`open: true` is a rule set of its own: no employee view, nobody to look up,
every ticked view read and every ticked table written the same way for
whoever asks — and a write still waits for a person. No rules at all stays
"deny everyone". The first probe ticks every view; the creator's notes reach
the model that writes each query.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from src.config import get_settings
from src.datasources.access import AccessRules, All, from_yaml, scope_for, scope_for_write, to_yaml
from src.datasources.query import StructuredQuery
from src.datasources.reader import PlatformDatabaseReader
from src.datasources.sources import (
    create_database_source,
    run_database_test,
    save_access_rules,
    set_agent_notes,
    set_allowed_views,
    set_allowed_write_tables,
)
from src.datasources.write import StructuredWrite, WriteOp
from src.db.base import engine
from src.db.models import AgentVersion, DatabaseDialect, DatabaseSource, SecretStoreEntry, User, VersionStatus
from src.models.node_registry import DataQueryConfig, DataWriteConfig
from src.services.secret_store import EncryptedDbSecretStore
from tests.conftest import ScriptedRun
from tests.test_api.test_data_query_node import SPEC
from tests.test_api.test_workflow_approval import trial_workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

TRUSTED = str(engine.url.host)


@pytest.fixture(autouse=True)
def _trusted_test_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "database_source_trusted_hosts", TRUSTED)


@pytest.fixture(autouse=True)
def _data() -> Iterator[None]:
    with engine.begin() as connection:
        connection.execute(
            text("CREATE OR REPLACE VIEW v_op_items AS SELECT * FROM (VALUES ('A', 3), ('B', 5)) AS t(sku, qty)")
        )
        connection.execute(text("CREATE OR REPLACE VIEW v_op_other AS SELECT 1 AS one"))
        connection.execute(text("DROP TABLE IF EXISTS t_op_log"))
        connection.execute(text("CREATE TABLE t_op_log (sku text NOT NULL, note text)"))
    yield
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW IF EXISTS v_op_items"))
        connection.execute(text("DROP VIEW IF EXISTS v_op_other"))
        connection.execute(text("DROP TABLE IF EXISTS t_op_log"))


@pytest.fixture
def source(db_session: Session, seeded_creator: User) -> Iterator[DatabaseSource]:
    url: URL = engine.url.set(drivername="postgresql")
    store = EncryptedDbSecretStore(db=db_session)
    row = create_database_source(
        db_session,
        seeded_creator.id,
        name="Kho hàng",
        dialect=DatabaseDialect.POSTGRESQL,
        dsn=url.render_as_string(hide_password=False),
        secret_store=store,
    )
    run_database_test(db_session, row, secret_store=store, trusted_hosts=frozenset({TRUSTED}))
    yield row
    db_session.delete(row)
    db_session.query(SecretStoreEntry).filter(SecretStoreEntry.key == row.dsn_secret_ref).delete()
    db_session.commit()


def _nobody(db: Session, owner: User) -> PlatformDatabaseReader:
    return PlatformDatabaseReader(
        db=db,
        owner_id=owner.id,
        asker=None,
        secret_store=EncryptedDbSecretStore(db=db),
        trusted_hosts=frozenset({TRUSTED}),
    )


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #


async def test_open_is_its_own_shape_and_reads_back_as_written() -> None:
    open_rules = AccessRules(open=True)
    assert open_rules.model_dump(mode="json") == {"open": True}
    assert to_yaml(open_rules) == "open: true\n"
    assert from_yaml("open: true").open is True
    assert scope_for(open_rules, None, "v_any") == All()
    assert scope_for_write(open_rules, None, "t_any", WriteOp.DELETE) == All()


@pytest.mark.parametrize(
    "document",
    [
        {"open": True, "identity": {"view": "v", "match_by": "email", "attributes": {"position": "p"}}},
        {"open": True, "rules": [{"position": "X", "views": {"v": "all"}}]},
        {"rules": []},
    ],
)
async def test_open_takes_nothing_else_and_closed_still_needs_an_identity(document: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AccessRules.model_validate(document)


# --------------------------------------------------------------------------- #
# The first probe, and reading/writing for nobody in particular
# --------------------------------------------------------------------------- #


async def test_the_first_probe_ticks_every_view_and_a_later_one_keeps_what_was_unticked(
    db_session: Session, source: DatabaseSource
) -> None:
    assert {"v_op_items", "v_op_other"} <= set(source.allowed_views)
    assert "tự chọn tất cả" in (source.last_probe_detail or "")

    set_allowed_views(db_session, source, ["v_op_items"])
    run_database_test(
        db_session, source, secret_store=EncryptedDbSecretStore(db=db_session), trusted_hosts=frozenset({TRUSTED})
    )
    assert source.allowed_views == ["v_op_items"]


async def test_an_open_database_is_read_and_written_with_nobody_to_look_up(
    db_session: Session, seeded_creator: User, source: DatabaseSource
) -> None:
    reader = _nobody(db_session, seeded_creator)
    query = StructuredQuery.model_validate({"view": "v_op_items", "order_by": [{"column": "sku"}]})
    read_config = DataQueryConfig(source_id=str(source.id), views=["v_op_items"])
    assert reader.read(read_config, query).denied is not None, "no rules at all still denies everyone"

    save_access_rules(db_session, source, AccessRules(open=True))
    rows = reader.read(read_config, query).rows
    assert rows is not None and [row[0] for row in rows.rows] == ["A", "B"]

    set_allowed_write_tables(db_session, source, ["t_op_log"])
    write = StructuredWrite.model_validate({"table": "t_op_log", "operation": "insert", "values": {"sku": "A"}})
    assert reader.write(DataWriteConfig(source_id=str(source.id), tables=["t_op_log"]), write).affected == 1


# --------------------------------------------------------------------------- #
# A trial on an open database, the notes in the prompt
# --------------------------------------------------------------------------- #


def _workflow(source: DatabaseSource) -> dict[str, Any]:
    return {
        "start_node_id": "query",
        "nodes": [
            {"id": "query", "type": "data_query", "config": {"source_id": str(source.id), "views": ["v_op_items"]}},
            {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời.", "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "query", "source_port": "done", "target_node_id": "answer"},
            {"id": "e2", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {},
    }


async def test_a_trial_with_nobody_picked_reads_an_open_database_and_the_query_model_is_told_the_notes(
    client: AsyncClient,
    creator_token: str,
    db_session: Session,
    seeded_creator: User,
    source: DatabaseSource,
    solution_spec_version: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    save_access_rules(db_session, source, AccessRules(open=True))
    set_agent_notes(db_session, source, "  v_op_items là tồn kho; qty tính bằng thùng  ")
    version = solution_spec_version(
        seeded_creator, SPEC, status=VersionStatus.WORKFLOW_CONFIRMED, workflow=_workflow(source)
    )
    query = {"view": "v_op_items", "columns": ["sku", "qty"]}
    run = scripted_chat_model_factory(
        [
            [AIMessage(content="", tool_calls=[{"name": "data_query", "args": query, "id": "q"}])],
            [AIMessage(content="A, B")],
        ]
    )

    response = await trial_workflow(client, version, "còn bao nhiêu hàng?", creator_token)

    assert response.status_code == 200, response.text
    assert "| A | 3" in response.text or "A | 3" in response.text
    asked = next(message for message in run[1][0].received_message_batches[0] if isinstance(message, HumanMessage))
    assert "v_op_items là tồn kho; qty tính bằng thùng" in str(asked.content)
    assert source.agent_notes == "v_op_items là tồn kho; qty tính bằng thùng"
