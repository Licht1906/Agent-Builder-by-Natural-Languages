"""UC1-08 (#122), FR-23/FR-24 — the dry-run probe, without a live Postgres.

`list_views`/`row_counter` are the injected seams (`src.services.uc1_connection_probe`);
`dsn` is never dialled by anything in this suite.
"""

import psycopg

from src.services.uc1_connection_probe import ProbeStatus, probe_postgres_connection


def _views(*names: str):
    def _list(schema: str) -> list[str]:
        return list(names)

    return _list


def _counts(mapping: dict[str, int]):
    def _count(dsn: str, name: str) -> int:
        return mapping[name]

    return _count


def test_matches_the_demo_scenario_two_views_1284_rows() -> None:
    """§15's own numbers — the probe result the demo script reads off screen."""
    result = probe_postgres_connection(
        dsn="postgresql://ro:pw@erp/db",
        allowed_objects=["v_ar_open", "v_customer_safe"],
        list_views=_views("v_ar_open", "v_customer_safe"),
        row_counter=_counts({"v_ar_open": 1084, "v_customer_safe": 200}),
    )

    assert result.status is ProbeStatus.CONNECTED
    assert "2 view" in result.detail
    assert "1284" in result.detail
    assert {c.object: c.row_count for c in result.objects} == {"v_ar_open": 1084, "v_customer_safe": 200}


def test_a_base_table_in_allowed_objects_fails_the_probe_before_counting_anything() -> None:
    def _count(dsn: str, name: str) -> int:
        raise AssertionError("must not count rows once a base table is found")

    result = probe_postgres_connection(
        dsn="postgresql://ro:pw@erp/db",
        allowed_objects=["orders"],
        list_views=_views("v_ar_open"),
        row_counter=_count,
    )

    assert result.status is ProbeStatus.FAILED
    assert "orders" in result.detail


def test_an_unreachable_database_fails_clearly_rather_than_raising() -> None:
    def _list(schema: str) -> list[str]:
        raise psycopg.OperationalError("could not connect to server")

    result = probe_postgres_connection(
        dsn="postgresql://ro:pw@nowhere/db", allowed_objects=["v_ar_open"], list_views=_list
    )

    assert result.status is ProbeStatus.FAILED
    assert "Không kết nối được" in result.detail


def test_a_view_that_no_longer_exists_when_counting_fails_clearly() -> None:
    def _count(dsn: str, name: str) -> int:
        raise psycopg.errors.UndefinedTable("relation does not exist")

    result = probe_postgres_connection(
        dsn="postgresql://ro:pw@erp/db",
        allowed_objects=["v_ar_open"],
        list_views=_views("v_ar_open"),
        row_counter=_count,
    )

    assert result.status is ProbeStatus.FAILED
    assert "đếm dòng thất bại" in result.detail
