"""An eval case on an agent that writes a Hệ CSDL — ADR-0094 over ADR-0069's frozen data.

Recording a case freezes, beside each view it reads, what its asker may do to
each table a `data_write` changes: the columns and the grant per operation,
never a row. Replayed, that is enough to prepare a write and authorize it
exactly as live — and the write itself can never reach a database.
"""

# The fixtures are `test_data_write_node`'s, imported so pytest finds them by name —
# which ruff reads as each test redefining the import it receives.
# ruff: noqa: F811


import pytest
from sqlalchemy.orm import Session

from src.datasources.reader import PlatformDatabaseReader
from src.datasources.write import StructuredWrite
from src.db.models import AgentVersion, DatabaseSource, EvalCassette, User
from src.evals.cases import add_case
from src.evals.cassettes import TABLE_PREFIX, CassetteReader, cassette_problem, record_case, sources_of
from src.models.node_registry import DataWriteConfig
from src.models.workflow import WorkflowJSON
from src.services.secret_store import EncryptedDbSecretStore
from src.workflow.runtime import side_effecting_actions
from tests.test_api.test_data_write_node import (  # noqa: F401 — fixtures are found by name
    NOTE,
    TRUSTED,
    _tables,
    _trusted_test_host,
    _workflow,
    source,
    version,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]


def _live(db: Session, owner: User) -> PlatformDatabaseReader:
    return PlatformDatabaseReader(
        db=db,
        owner_id=owner.id,
        asker=None,
        secret_store=EncryptedDbSecretStore(db=db),
        trusted_hosts=frozenset({TRUSTED}),
    )


def _recorded(db: Session, owner: User, version: AgentVersion, source: DatabaseSource, run_as: str) -> CassetteReader:
    workflow = WorkflowJSON.model_validate(_workflow(str(source.id)))
    case = add_case(
        db,
        version.agent_id,
        version.id,
        criterion="Ghi đúng khách",
        input_message="ghi lại C1",
        expected="đã ghi",
        run_as=run_as,
    )
    assert record_case(db, case, workflow, _live(db, owner)) is None
    cassettes = db.query(EvalCassette).filter(EvalCassette.case_id == case.id).all()
    assert [cassette.view for cassette in cassettes] == [TABLE_PREFIX + "t_dwn_notes"]
    assert cassette_problem(case, workflow, cassettes, sources_of(db, workflow)) is None
    assert cassette_problem(case, workflow, [], sources_of(db, workflow)) is not None
    return CassetteReader.of(cassettes, asker=run_as)


async def test_a_graph_that_writes_offers_an_action_to_decline(source: DatabaseSource) -> None:
    assert side_effecting_actions(WorkflowJSON.model_validate(_workflow(str(source.id)))) == ("data_write",)


async def test_a_recorded_case_prepares_and_authorizes_a_write_from_frozen_grants_alone(
    db_session: Session, seeded_creator: User, version: AgentVersion, source: DatabaseSource
) -> None:
    frozen = _recorded(db_session, seeded_creator, version, source, "an@cty.vn")
    config = DataWriteConfig(source_id=str(source.id), tables=["t_dwn_notes"])

    assert frozen.tables_of(config) == {"t_dwn_notes": ["id", "customer_id", "sales_rep_id", "note"]}
    allowed = frozen.authorize(config, StructuredWrite.model_validate(NOTE))
    assert allowed.allowed and allowed.scope == (("sales_rep_id", "E102"),)

    delete = StructuredWrite.model_validate(
        {"table": "t_dwn_notes", "operation": "delete", "where": [{"column": "id", "value": 1}]}
    )
    assert frozen.authorize(config, delete).denied is not None
    hijack = StructuredWrite.model_validate({**NOTE, "values": {**NOTE["values"], "sales_rep_id": "E200"}})
    assert frozen.authorize(config, hijack).denied is not None
    guessed = StructuredWrite.model_validate({**NOTE, "values": {"amount": 1}})
    assert "không có cột amount" in (frozen.authorize(config, guessed).refusal or "")


async def test_an_asker_with_no_grant_is_frozen_as_denied_and_a_write_never_runs(
    db_session: Session, seeded_creator: User, version: AgentVersion, source: DatabaseSource
) -> None:
    frozen = _recorded(db_session, seeded_creator, version, source, "binh@cty.vn")
    config = DataWriteConfig(source_id=str(source.id), tables=["t_dwn_notes"])
    write = StructuredWrite.model_validate(NOTE)

    assert frozen.authorize(config, write).denied is not None
    assert frozen.write(config, write).refusal == "Lượt eval không ghi vào Hệ CSDL"
