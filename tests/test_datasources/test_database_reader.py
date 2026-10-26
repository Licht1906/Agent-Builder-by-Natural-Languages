"""`PlatformDatabaseReader` — a `data_query` node's one door into a Hệ CSDL. DS-04 / ADR-0059 §5–6.

End to end against the test Postgres: a real Hệ CSDL row, its DSN in the
secret store, probed for real, views ticked, access rules saved. Two askers
ask the same question and get the rows their position allows; nobody asking
means no query at all.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from src.datasources.access import AccessRules, Deny, DenyCause
from src.datasources.query import StructuredQuery
from src.datasources.reader import PlatformDatabaseReader
from src.datasources.sources import create_database_source, run_database_test, save_access_rules, set_allowed_views
from src.db.base import engine
from src.db.models import DatabaseDialect, DatabaseSource, SecretStoreEntry, User
from src.models.node_registry import DataQueryConfig
from src.services.secret_store import EncryptedDbSecretStore

TRUSTED = frozenset({str(engine.url.host)})

RULES = AccessRules.model_validate(
    {
        "identity": {
            "view": "v_dq_staff",
            "match_by": "email",
            "attributes": {"employee_code": "emp_id", "department": "dept_code", "position": "position"},
        },
        "rules": [
            {"position": "Nhân viên KD", "views": {"v_dq_ar": {"where": {"sales_rep_id": "employee_code"}}}},
            {"position": "Trưởng nhóm KD", "views": {"v_dq_ar": {"where": {"department": "department"}}}},
        ],
    }
)

OVERDUE = StructuredQuery.model_validate(
    {
        "view": "v_dq_ar",
        "columns": ["customer_id", "days_overdue"],
        "filters": [{"column": "days_overdue", "op": ">", "value": 30}],
        "order_by": [{"column": "customer_id", "dir": "asc"}],
    }
)


@pytest.fixture(autouse=True)
def _views() -> Iterator[None]:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE OR REPLACE VIEW v_dq_staff AS SELECT * FROM (VALUES "
                "('an@cty.vn', 'E102', 'SALES', 'Nhân viên KD'), "
                "('binh@cty.vn', 'E200', 'SALES', 'Trưởng nhóm KD'), "
                "('chi@cty.vn', 'E300', 'FIN', 'Kế toán trưởng')"
                ") AS t(email, emp_id, dept_code, position)"
            )
        )
        connection.execute(
            text(
                "CREATE OR REPLACE VIEW v_dq_ar AS SELECT * FROM (VALUES "
                "('C1', 'E102', 'SALES', 45), ('C2', 'E102', 'SALES', 10), "
                "('C3', 'E200', 'SALES', 60), ('C4', 'E300', 'FIN', 90)"
                ") AS t(customer_id, sales_rep_id, department, days_overdue)"
            )
        )
    yield
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW IF EXISTS v_dq_staff"))
        connection.execute(text("DROP VIEW IF EXISTS v_dq_ar"))


@pytest.fixture
def source(db_session: Session, seeded_creator: User) -> Iterator[DatabaseSource]:
    """A probed Hệ CSDL with both views ticked and the rules above saved."""
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
    run_database_test(db_session, row, secret_store=store, trusted_hosts=TRUSTED)
    set_allowed_views(db_session, row, ["v_dq_staff", "v_dq_ar"])
    save_access_rules(db_session, row, RULES)
    yield row
    db_session.delete(row)
    db_session.query(SecretStoreEntry).filter(SecretStoreEntry.key == row.dsn_secret_ref).delete()
    db_session.commit()


def _reader(db_session: Session, owner: User, asker: str | None) -> PlatformDatabaseReader:
    return PlatformDatabaseReader(
        db=db_session,
        owner_id=owner.id,
        asker=asker,
        secret_store=EncryptedDbSecretStore(db=db_session),
        trusted_hosts=TRUSTED,
    )


def _config(source: DatabaseSource, **overrides: Any) -> DataQueryConfig:
    return DataQueryConfig.model_validate({"source_id": str(source.id), "views": ["v_dq_ar"], **overrides})


def _customers(outcome: Any) -> list[str]:
    assert outcome.rows is not None, outcome.refusal or outcome.denied
    index = outcome.rows.columns.index("customer_id")
    return [row[index] for row in outcome.rows.rows]


def test_two_askers_with_different_positions_get_different_rows(
    db_session: Session, seeded_creator: User, source: DatabaseSource
) -> None:
    rep = _reader(db_session, seeded_creator, "an@cty.vn").read(_config(source), OVERDUE)
    lead = _reader(db_session, seeded_creator, "binh@cty.vn").read(_config(source), OVERDUE)

    assert _customers(rep) == ["C1"]
    assert _customers(lead) == ["C1", "C3"]


def test_nobody_asking_means_no_query(db_session: Session, seeded_creator: User, source: DatabaseSource) -> None:
    outcome = _reader(db_session, seeded_creator, None).read(_config(source), OVERDUE)

    assert outcome.rows is None
    assert outcome.denied == Deny(cause=DenyCause.UNKNOWN_ASKER)


def test_an_asker_the_employee_view_does_not_know_is_denied(
    db_session: Session, seeded_creator: User, source: DatabaseSource
) -> None:
    outcome = _reader(db_session, seeded_creator, "la@cty.vn").read(_config(source), OVERDUE)

    assert outcome.denied == Deny(cause=DenyCause.UNKNOWN_ASKER)


def test_a_position_with_no_rule_is_denied(db_session: Session, seeded_creator: User, source: DatabaseSource) -> None:
    outcome = _reader(db_session, seeded_creator, "chi@cty.vn").read(_config(source), OVERDUE)

    assert outcome.denied == Deny(cause=DenyCause.NO_RULE)


def test_a_view_the_node_does_not_read_is_refused_with_a_reason(
    db_session: Session, seeded_creator: User, source: DatabaseSource
) -> None:
    staff = StructuredQuery.model_validate({"view": "v_dq_staff", "columns": ["email"]})

    outcome = _reader(db_session, seeded_creator, "an@cty.vn").read(_config(source), staff)

    assert outcome.rows is None
    assert outcome.refusal == "view v_dq_staff không nằm trong các view node này được đọc"


def test_a_column_the_view_does_not_have_is_refused_by_name(
    db_session: Session, seeded_creator: User, source: DatabaseSource
) -> None:
    salary = StructuredQuery.model_validate({"view": "v_dq_ar", "columns": ["salary"]})

    outcome = _reader(db_session, seeded_creator, "an@cty.vn").read(_config(source), salary)

    assert outcome.refusal == "view v_dq_ar không có cột salary"


def test_a_source_that_is_not_the_owners_is_never_read(
    db_session: Session, source: DatabaseSource, second_creator: User
) -> None:
    outcome = _reader(db_session, second_creator, "an@cty.vn").read(_config(source), OVERDUE)

    assert outcome.rows is None
    assert outcome.refusal is not None


def test_a_source_with_no_rules_denies_before_reading_anybody(
    db_session: Session, seeded_creator: User, source: DatabaseSource
) -> None:
    source.access_rules = None
    db_session.commit()

    outcome = _reader(db_session, seeded_creator, "an@cty.vn").read(_config(source), OVERDUE)

    assert outcome.denied == Deny(cause=DenyCause.NO_RULES)


def test_the_nodes_row_ceiling_is_kept(db_session: Session, seeded_creator: User, source: DatabaseSource) -> None:
    every = StructuredQuery.model_validate({"view": "v_dq_ar", "columns": ["customer_id"], "limit": 100})

    outcome = _reader(db_session, seeded_creator, "binh@cty.vn").read(_config(source, max_rows=1), every)

    assert len(_customers(outcome)) == 1


def test_the_outcome_says_where_its_numbers_came_from(
    db_session: Session, seeded_creator: User, source: DatabaseSource
) -> None:
    outcome = _reader(db_session, seeded_creator, "an@cty.vn").read(_config(source), OVERDUE)

    assert outcome.source_name == "ERP staging"
    assert outcome.view == "v_dq_ar"
    assert "days_overdue > 30" in outcome.description
    assert "phạm vi" in outcome.description


def test_the_columns_the_model_may_ask_for_are_the_nodes_views_only(
    db_session: Session, seeded_creator: User, source: DatabaseSource
) -> None:
    columns = _reader(db_session, seeded_creator, "an@cty.vn").columns_of(_config(source))

    assert columns == {"v_dq_ar": ["customer_id", "sales_rep_id", "department", "days_overdue"]}
