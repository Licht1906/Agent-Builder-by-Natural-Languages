"""A structured query on frozen rows answers what Postgres answers — EV-02.

`src.datasources.in_memory` is what every eval `data_query` runs through, so
its answers are only worth anything if they are the database's. Each query
below runs twice on "ERP mẫu": live through `PlatformDatabaseReader`, and in
memory on a snapshot of the same view read through the same reader — the
cassette's own recipe. The two must agree row for row, once values are put in
the JSON shape a cassette stores.
"""

# The ERP mẫu fixtures are `test_erp_mau_seed`'s, imported so pytest finds them by
# name — which ruff reads as each test redefining the import it receives.
# ruff: noqa: F811

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.datasources.in_memory import run_in_memory
from src.datasources.query import StructuredQuery
from src.datasources.reader import PlatformDatabaseReader
from src.db.base import engine
from src.db.models import DatabaseSource
from src.evals.cassettes import _json_value
from src.models.node_registry import DataQueryConfig
from src.services.secret_store import EncryptedDbSecretStore
from tests.test_db.test_erp_mau_seed import TRUSTED, erp_dsn, erp_source  # noqa: F401 — fixtures are found by name

CHIEF = "ke.toan@erp-mau.local"
REP = "nv.kinhdoanh@erp-mau.local"

QUERIES: list[dict[str, Any]] = [
    {"view": "v_ar_open", "aggregates": [{"fn": "count", "column": "invoice_id", "as": "n"}],
     "filters": [{"column": "days_overdue", "op": ">", "value": 30}]},
    {"view": "v_ar_open", "group_by": ["department"],
     "aggregates": [{"fn": "sum", "column": "amount_due", "as": "total"}, {"fn": "max", "column": "days_overdue", "as": "worst"}],
     "order_by": [{"column": "department", "dir": "asc"}]},
    {"view": "v_ar_open", "columns": ["customer_name", "days_overdue"],
     "order_by": [{"column": "days_overdue", "dir": "desc"}], "limit": 3},
    {"view": "v_ar_open", "columns": ["customer_id"], "filters": [{"column": "customer_name", "op": "like", "value": "%Minh%"}],
     "order_by": [{"column": "customer_id", "dir": "asc"}]},
    {"view": "v_ar_open", "columns": ["customer_id", "days_overdue"],
     "filters": [{"column": "days_overdue", "op": "between", "value": [40, 60]},
                 {"column": "sales_rep_id", "op": "in", "value": ["E102", "E103", "E105"]}],
     "order_by": [{"column": "customer_id", "dir": "asc"}]},
    {"view": "v_ar_open", "columns": ["customer_id", "due_date"],
     "filters": [{"column": "due_date", "op": "<", "value": "2100-01-01"}, {"column": "department", "op": "!=", "value": "Tài chính"}],
     "order_by": [{"column": "due_date", "dir": "asc"}, {"column": "customer_id", "dir": "asc"}], "limit": 5},
    {"view": "v_customer_safe", "columns": ["customer_id", "note"],
     "filters": [{"column": "note", "op": "is_not_null"}]},
    {"view": "v_customer_safe", "group_by": ["city"],
     "aggregates": [{"fn": "count", "column": "*", "as": "n"}, {"fn": "count", "column": "note", "as": "with_note"}],
     "order_by": [{"column": "n", "dir": "desc"}, {"column": "city", "dir": "asc"}]},
    {"view": "v_ar_open", "aggregates": [{"fn": "avg", "column": "days_overdue", "as": "mean"}],
     "filters": [{"column": "days_overdue", "op": ">", "value": 100000}]},
]  # fmt: skip


def _reader(db: Session, source: DatabaseSource, asker: str) -> PlatformDatabaseReader:
    return PlatformDatabaseReader(
        db=db,
        owner_id=source.owner_id,
        asker=asker,
        secret_store=EncryptedDbSecretStore(db=db),
        trusted_hosts=TRUSTED,
    )


def _normalised(rows: Any) -> list[list[Any]]:
    out = []
    for row in rows:
        values = []
        for value in row:
            value = _json_value(value)
            values.append(round(value, 6) if isinstance(value, float) else value)
        out.append(values)
    return out


@pytest.mark.parametrize("asker", [CHIEF, REP])
@pytest.mark.parametrize("raw", QUERIES, ids=[f"q{n}" for n in range(len(QUERIES))])
def test_a_query_on_the_frozen_view_answers_what_the_database_answers(
    db_session: Session, erp_source: DatabaseSource, asker: str, raw: dict[str, Any]
) -> None:
    query = StructuredQuery.model_validate(raw)
    reader = _reader(db_session, erp_source, asker)
    config = DataQueryConfig(source_id=str(erp_source.id), views=["v_ar_open", "v_customer_safe"], max_rows=1000)

    live = reader.read(config, query)
    snapshot = reader.read(config, StructuredQuery(view=query.view))
    assert live.rows is not None and snapshot.rows is not None, live.refusal or live.denied
    frozen = run_in_memory(
        query, columns=snapshot.rows.columns, rows=_normalised(snapshot.rows.rows), limit=query.limit or 1000
    )

    assert frozen.columns == live.rows.columns
    assert _normalised(frozen.rows) == _normalised(live.rows.rows)


def test_text_sorts_in_the_databases_collation_not_by_code_point() -> None:
    """Vietnamese names, case, spaces and punctuation: the in-memory sort is the database's `ORDER BY`."""
    words = ["Hà Nội", "Hải Phòng", "Đà Nẵng", "Hồ Chí Minh", "Cần Thơ", "a b", "ab", "A c", "Đa", "Da", "da"]
    words += ["KH-0010", "KH-001", "KH 002", "Z", "é", "e", "f"]
    values = ", ".join(f"('{word}')" for word in words)
    with engine.connect() as connection:
        live = [row[0] for row in connection.execute(text(f"SELECT x FROM (VALUES {values}) AS t(x) ORDER BY x"))]

    query = StructuredQuery.model_validate({"view": "t", "order_by": [{"column": "x", "dir": "asc"}]})
    frozen = run_in_memory(query, columns=["x"], rows=[[word] for word in words], limit=100)

    assert [row[0] for row in frozen.rows] == live
