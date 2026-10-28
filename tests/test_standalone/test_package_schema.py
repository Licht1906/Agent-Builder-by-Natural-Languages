"""The package checks the company's database against what the agent uses — DS-15 / ADR-0059 §4.

Against the package `test_standalone_runtime` builds, whose Hệ CSDL is the
test Postgres. Each test reshapes its two views first, as an ERP admin would
between the export and the start-up:

- a column a rule filters on dropped → the package does not start, and says
  which view and which column — every gap at once, not the first;
- a view a `data_query` node reads, or a column it was built with, missing →
  the same refusal;
- a column added since the export → it starts, warns, and leaves the column
  unread;
- the platform's export writes the columns the package checks against.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.datasources.sources import views_built_on
from src.db.base import engine
from src.db.models import Agent, AgentVersion, DatabaseDialect, DatabaseSource, User, VersionStatus
from src.standalone.config import RuntimeConfigError
from tests.conftest import auth_header
from tests.test_standalone.test_standalone_runtime import (
    SOURCE_ID,
    SPEC,
    _loaded,
    _views,
    _workflow,
    package_dir,
    secrets_env,
)

# Scenery borrowed from `test_standalone_runtime`, named so pytest sees it.
__all__ = ["_views", "package_dir", "secrets_env"]

#: `v_sa_ar` as the creator's Hệ CSDL had it when the agent was exported.
BUILT_ON = {SOURCE_ID: {"v_sa_ar": ["customer_id", "sales_rep_id", "department", "days_overdue"]}}


def _replace_view(name: str, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
    """`name` dropped and created with exactly `columns` — `CREATE OR REPLACE` cannot drop one."""
    values = ", ".join("(" + ", ".join(repr(value) for value in row) + ")" for row in rows)
    with engine.begin() as connection:
        connection.execute(text(f"DROP VIEW IF EXISTS {name}"))
        connection.execute(text(f"CREATE VIEW {name} AS SELECT * FROM (VALUES {values}) AS t({', '.join(columns)})"))


def _refusal(directory: Path) -> str:
    with pytest.raises(RuntimeConfigError) as caught:
        _loaded(directory)
    return str(caught.value)


def test_a_dropped_column_a_rule_filters_on_stops_the_start_naming_view_and_column(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    _replace_view("v_sa_ar", ["customer_id", "department", "days_overdue"], [("C1", "SALES", 45)])

    refusal = _refusal(package_dir(database_schema=BUILT_ON))

    assert "v_sa_ar" in refusal and "sales_rep_id" in refusal


def test_every_gap_is_named_at_once_not_only_the_first(package_dir: Callable[..., Path], secrets_env: None) -> None:
    """Two views, two missing columns — one refusal names both, so IT fixes them in one go."""
    _replace_view("v_sa_ar", ["customer_id", "department", "days_overdue"], [("C1", "SALES", 45)])
    _replace_view("v_sa_staff", ["email", "emp_id", "position"], [("an@cty.vn", "E102", "Nhân viên KD")])

    refusal = _refusal(package_dir(database_schema=BUILT_ON))

    assert "view v_sa_ar thiếu cột sales_rep_id" in refusal
    assert "view v_sa_staff thiếu cột dept_code" in refusal
    assert "ERP mẫu" in refusal


def test_a_view_a_data_query_node_reads_missing_stops_the_start(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    workflow = _workflow()
    workflow["nodes"][0]["config"]["views"] = ["v_sa_ar", "v_sa_orders"]

    refusal = _refusal(package_dir(workflow=workflow, database_schema=BUILT_ON))

    assert "không có view v_sa_orders" in refusal


def test_a_column_the_agent_was_built_with_missing_stops_the_start(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    """`days_overdue` is in no rule — the agent's queries use it, which the bundle's schema records."""
    _replace_view("v_sa_ar", ["customer_id", "sales_rep_id", "department"], [("C1", "E102", "SALES")])

    refusal = _refusal(package_dir(database_schema=BUILT_ON))

    assert "view v_sa_ar thiếu cột days_overdue" in refusal


def test_a_view_turned_into_a_table_is_refused_as_a_table(package_dir: Callable[..., Path], secrets_env: None) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW IF EXISTS v_sa_ar"))
        connection.execute(text("CREATE TABLE v_sa_ar (customer_id text, sales_rep_id text, department text)"))
    try:
        refusal = _refusal(package_dir(database_schema=BUILT_ON))
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS v_sa_ar"))

    assert "v_sa_ar là bảng gốc, không phải view" in refusal


def test_a_column_added_since_the_export_starts_with_a_warning_and_stays_unread(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    _replace_view(
        "v_sa_ar",
        ["customer_id", "sales_rep_id", "department", "days_overdue", "credit_limit"],
        [("C1", "E102", "SALES", 45, 1000)],
    )

    package = _loaded(package_dir(database_schema=BUILT_ON))

    [warning] = [line for line in package.warnings if "credit_limit" in line]
    assert "v_sa_ar" in warning
    assert package.sources[SOURCE_ID].view_columns["v_sa_ar"] == [
        "customer_id",
        "sales_rep_id",
        "department",
        "days_overdue",
    ]


def test_a_bundle_without_a_schema_still_checks_the_rules_and_says_it_could_not_check_the_columns(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    package = _loaded(package_dir())

    assert any("không kiểm được cột" in line and "v_sa_ar" in line for line in package.warnings)
    assert package.sources[SOURCE_ID].allowed_views == ["v_sa_ar", "v_sa_staff"]

    _replace_view("v_sa_ar", ["customer_id", "department", "days_overdue"], [("C1", "SALES", 45)])
    assert "sales_rep_id" in _refusal(package_dir())


def test_a_malformed_database_schema_in_the_bundle_is_refused(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    directory = package_dir(database_schema={SOURCE_ID: {"v_sa_ar": "customer_id"}})

    assert "sai định dạng" in _refusal(directory)


def test_a_database_that_cannot_be_reached_is_named_with_the_rest(
    package_dir: Callable[..., Path], secrets_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_PKG_ERP_DSN", "postgresql://nobody@unreachable.invalid:5432/erp")
    refusal = _refusal(package_dir(trusted_hosts=["unreachable.invalid"]))

    assert "Không đọc được Hệ CSDL ERP mẫu" in refusal


# --------------------------------------------------------------------------- #
# The platform's export writes what the package checks against
# --------------------------------------------------------------------------- #


def test_views_built_on_is_the_owners_last_probe_of_each_view_a_node_reads(db_session: Session) -> None:
    owner = User(email="ds15-owner@cty.vn", password_hash="!", role="creator")
    stranger = User(email="ds15-stranger@cty.vn", password_hash="!", role="creator")
    db_session.add_all([owner, stranger])
    db_session.flush()
    mine = DatabaseSource(
        owner_id=owner.id,
        name="ERP",
        dialect=DatabaseDialect.POSTGRESQL,
        allowed_views=["v_sa_ar"],
        view_columns={"v_sa_ar": ["customer_id", "days_overdue"], "v_sa_staff": ["email"]},
    )
    theirs = DatabaseSource(
        owner_id=stranger.id, name="Của người khác", dialect=DatabaseDialect.POSTGRESQL, view_columns={"v_x": ["a"]}
    )
    db_session.add_all([mine, theirs])
    db_session.commit()
    try:
        workflow = {
            "nodes": [
                {
                    "id": "q1",
                    "type": "data_query",
                    "config": {"source_id": str(mine.id), "views": ["v_sa_ar", "v_gone"]},
                },
                {"id": "q2", "type": "data_query", "config": {"source_id": str(theirs.id), "views": ["v_x"]}},
                {"id": "a", "type": "agent_llm", "config": {}},
            ]
        }

        assert views_built_on(db_session, owner.id, workflow) == {
            str(mine.id): {"v_sa_ar": ["customer_id", "days_overdue"]}
        }
    finally:
        db_session.delete(mine)
        db_session.delete(theirs)
        db_session.delete(owner)
        db_session.delete(stranger)
        db_session.commit()


@pytest.mark.asyncio
async def test_an_exported_bundle_carries_the_columns_and_starts_a_package(
    client: AsyncClient,
    creator_token: str,
    db_session: Session,
    seeded_creator: User,
    package_dir: Callable[..., Path],
    secrets_env: None,
) -> None:
    """Export → the package's bundle: the schema round-trips, and a column dropped since refuses the start."""
    source = DatabaseSource(
        owner_id=seeded_creator.id,
        name="ERP",
        dialect=DatabaseDialect.POSTGRESQL,
        allowed_views=["v_sa_ar", "v_sa_staff"],
        view_columns={"v_sa_ar": BUILT_ON[SOURCE_ID]["v_sa_ar"]},
    )
    db_session.add(source)
    db_session.flush()
    workflow = _workflow()
    workflow["nodes"][0]["config"]["source_id"] = str(source.id)
    agent = Agent(owner_id=seeded_creator.id, name="Trợ lý công nợ")
    db_session.add(agent)
    db_session.flush()
    version = AgentVersion(
        agent_id=agent.id, solution_spec=SPEC, workflow=workflow, status=VersionStatus.WORKFLOW_CONFIRMED
    )
    db_session.add(version)
    db_session.commit()

    response = await client.get(
        f"/api/v1/agents/{agent.id}/versions/{version.id}/export", headers=auth_header(creator_token)
    )

    exported = response.json()["database_schema"]
    assert exported == {str(source.id): {"v_sa_ar": BUILT_ON[SOURCE_ID]["v_sa_ar"]}}
    _replace_view("v_sa_ar", ["customer_id", "sales_rep_id", "department"], [("C1", "E102", "SALES")])
    directory = package_dir(database_schema={SOURCE_ID: exported[str(source.id)]})
    assert "days_overdue" in _refusal(directory)
