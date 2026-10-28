""" "ERP mẫu" — the demo company's database, seeded and read through the platform. DS-23.

A scratch database seeded by `scripts/seed_erp_mau.py`, registered as a Hệ
CSDL the way a creator registers it (probe, tick views, `deploy/erp-mau/access.yaml`),
then read through `PlatformDatabaseReader` as each demo employee. The ticket's
criteria:

- the rows each demo employee reads are the demo's numbers (3 for the rep, 15
  for the lead, every department for the chief accountant);
- seeding again changes nothing, including on a later day;
- the customer addresses go to the configured mailbox, and one note carries
  the injected instruction;
- `DEMO_ASKERS` (the sample suite's askers, DS-22) are employees here.
"""

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import psycopg
import pytest
import sqlalchemy
from sqlalchemy import text
from sqlalchemy.orm import Session

from scripts.seed_erp_mau import EMPLOYEES, EXPECTED_OVERDUE, INJECTED_NOTE, overdue_counts, seed
from src.datasources.access import from_yaml
from src.datasources.query import QueryAggregate, QueryFilter, StructuredQuery
from src.datasources.reader import PlatformDatabaseReader
from src.datasources.sources import create_database_source, run_database_test, save_access_rules, set_allowed_views
from src.db.base import engine
from src.db.models import DatabaseDialect, DatabaseSource, SecretStoreEntry, User
from src.evals.sample_suites import DEMO_ASKERS
from src.models.node_registry import DataQueryConfig
from src.services.secret_store import EncryptedDbSecretStore

ACCESS = Path(__file__).resolve().parents[2] / "deploy" / "erp-mau" / "access.yaml"
TRUSTED = frozenset({str(engine.url.host)})
MAILBOX = "demo.congno@example.com"
OVERDUE = StructuredQuery(
    view="v_ar_open",
    filters=[QueryFilter(column="days_overdue", op=">", value=30)],
    aggregates=[QueryAggregate(fn="count", column="invoice_id", alias="n")],
)


@pytest.fixture
def erp_dsn() -> Iterator[str]:
    name = "ds23_erp_mau_test"
    admin = sqlalchemy.create_engine(engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        connection.execute(text(f"CREATE DATABASE {name}"))
    yield engine.url.set(database=name, drivername="postgresql").render_as_string(hide_password=False)
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    admin.dispose()


def _seed(dsn: str, *, today: date | None = None) -> dict[str, int]:
    with psycopg.connect(dsn) as conn:
        seed(conn, mailbox=MAILBOX, today=today)
        counts = overdue_counts(conn)
        conn.commit()
    return counts


@pytest.fixture
def erp_source(db_session: Session, seeded_creator: User, erp_dsn: str) -> Iterator[DatabaseSource]:
    """The seeded database registered as a creator would: probe, tick the three views, save the demo rules."""
    _seed(erp_dsn)
    store = EncryptedDbSecretStore(db=db_session)
    row = create_database_source(
        db_session,
        seeded_creator.id,
        name="ERP mẫu",
        dialect=DatabaseDialect.POSTGRESQL,
        dsn=erp_dsn,
        secret_store=store,
    )
    run_database_test(db_session, row, secret_store=store, trusted_hosts=TRUSTED)
    set_allowed_views(db_session, row, ["v_employees", "v_ar_open", "v_customer_safe"])
    save_access_rules(db_session, row, from_yaml(ACCESS.read_text(encoding="utf-8")))
    yield row
    db_session.delete(row)
    db_session.query(SecretStoreEntry).filter(SecretStoreEntry.key == row.dsn_secret_ref).delete()
    db_session.commit()


def _read_as(db_session: Session, source: DatabaseSource, asker: str, query: StructuredQuery = OVERDUE) -> object:
    reader = PlatformDatabaseReader(
        db=db_session,
        owner_id=source.owner_id,
        asker=asker,
        secret_store=EncryptedDbSecretStore(db=db_session),
        trusted_hosts=TRUSTED,
    )
    config = DataQueryConfig(source_id=str(source.id), views=["v_ar_open", "v_customer_safe"])
    return reader.read(config, query)


def test_each_demo_employee_reads_the_demos_numbers_through_the_platform(
    db_session: Session, erp_source: DatabaseSource
) -> None:
    emails = {employee.code: employee.email for employee in EMPLOYEES}
    counts = {}
    for code in EXPECTED_OVERDUE:
        outcome = _read_as(db_session, erp_source, emails[code])
        assert outcome.rows is not None, outcome.refusal or outcome.denied
        counts[code] = int(outcome.rows.rows[0][0])

    assert counts == EXPECTED_OVERDUE == {"E102": 3, "E105": 4, "E201": 15, "E401": 21}


def test_somebody_outside_the_company_reads_nothing(db_session: Session, erp_source: DatabaseSource) -> None:
    outcome = _read_as(db_session, erp_source, "nguoi.la@example.com")

    assert outcome.rows is None and outcome.denied is not None


def test_seeding_again_changes_nothing_and_puts_a_stale_seed_right(erp_dsn: str) -> None:
    """A seed is true on the day it ran: one written 40 days ago has drifted, and today's run puts it right.

    `days_overdue` is the view's own `CURRENT_DATE - due_date`, which is why
    `erp-mau-seed` and the GCP job run on every start rather than once.
    """
    stale = _seed(erp_dsn, today=date.today() - timedelta(days=40))
    first = _seed(erp_dsn)
    again = _seed(erp_dsn)

    with psycopg.connect(erp_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM customers")
        customers = cur.fetchone()[0]

    assert {code: stale[code] for code in EXPECTED_OVERDUE} != EXPECTED_OVERDUE
    assert first == again
    assert {code: first[code] for code in EXPECTED_OVERDUE} == EXPECTED_OVERDUE
    assert customers == 38, "a re-run replaces the demo rows, it does not add to them"


def test_customers_write_to_the_configured_mailbox_and_one_note_is_an_injection(
    db_session: Session, erp_source: DatabaseSource
) -> None:
    query = StructuredQuery(view="v_customer_safe", columns=["customer_name", "contact_email", "note"])

    outcome = _read_as(db_session, erp_source, DEMO_ASKERS["nhan_vien_kd"], query)

    rows = outcome.rows.rows
    assert all(email.startswith("demo.congno+") and email.endswith("@example.com") for _, email, _ in rows)
    assert [note for _, _, note in rows if note] == [INJECTED_NOTE]
    assert "Công ty ABC" in {name for name, _, _ in rows}, "the customer the demo writes to is the rep's own"


def test_the_sample_suites_askers_are_employees_here() -> None:
    assert set(DEMO_ASKERS.values()) <= {employee.email for employee in EMPLOYEES}
