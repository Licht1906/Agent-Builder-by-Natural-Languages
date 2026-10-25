"""Quy tắc truy cập through HTTP — DS-03 / ADR-0059 §6.

A real Hệ CSDL underneath: the test Postgres, with a small employee view
added, probed for real, its views ticked. What is pinned here is what the
API promises around the engine — a rule naming a missing column is refused
naming that column, YAML goes in and comes back out, the positions offered
on the form are the database's own.
"""

from collections.abc import Generator
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.base import engine
from src.db.models import DatabaseSource, SecretStoreEntry
from src.deployments.rate_limit import SlidingWindowLimiter
from src.main import app
from src.services.connection_test_rate_limit import get_connection_test_rate_limiter
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

RULES: dict[str, Any] = {
    "identity": {
        "view": "v_test_staff",
        "match_by": "email",
        "attributes": {"employee_code": "emp_id", "department": "dept_code", "position": "position"},
    },
    "rules": [
        {
            "position": "Nhân viên KD",
            "views": {"v_ar_open": {"where": {"sales_rep_id": "employee_code"}}},
        },
        {"position": "Trưởng nhóm KD", "views": {"v_ar_open": {"where": {"department": "department"}}}},
        {"position": "Kế toán trưởng", "views": {"v_ar_open": "all"}},
    ],
}


@pytest.fixture(autouse=True)
def _staff_view() -> Generator[None, None, None]:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE OR REPLACE VIEW v_test_staff AS SELECT * FROM (VALUES "
                "('an@cty.vn', 'E102', 'SALES', 'Nhân viên KD'), "
                "('binh@cty.vn', 'E200', 'SALES', 'Trưởng nhóm KD'), "
                "('chi@cty.vn', 'E300', 'FIN', 'Kế toán trưởng')"
                ") AS t(email, emp_id, dept_code, position)"
            )
        )
    yield
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW IF EXISTS v_test_staff"))


@pytest.fixture(autouse=True)
def _trusted_test_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "database_source_trusted_hosts", str(engine.url.host))


@pytest.fixture(autouse=True)
def _fresh_test_limiter() -> Generator[None, None, None]:
    limiter = SlidingWindowLimiter()
    app.dependency_overrides[get_connection_test_rate_limiter] = lambda: limiter
    yield
    app.dependency_overrides.pop(get_connection_test_rate_limiter, None)


@pytest.fixture(autouse=True)
def _no_source_outlives_its_test(db_session: Session) -> Generator[None, None, None]:
    yield
    db_session.expire_all()
    for row in db_session.query(DatabaseSource).all():
        db_session.delete(row)
    db_session.query(SecretStoreEntry).filter(SecretStoreEntry.key.like("database_source:%")).delete(
        synchronize_session=False
    )
    db_session.commit()


@pytest_asyncio.fixture
async def source_url(client: AsyncClient, creator_token: str) -> str:
    """A probed Hệ CSDL with the staff view and `v_ar_open` ticked."""
    url: URL = engine.url.set(drivername="postgresql")
    created = await client.post(
        "/api/v1/database-sources",
        json={"name": "ERP staging", "dialect": "postgresql", "dsn": url.render_as_string(hide_password=False)},
        headers=auth_header(creator_token),
    )
    base = f"/api/v1/database-sources/{created.json()['id']}"
    tested = await client.post(f"{base}/test", headers=auth_header(creator_token))
    assert tested.json()["status"] == "connected", tested.text
    ticked = await client.patch(
        base, json={"allowed_views": ["v_test_staff", "v_ar_open"]}, headers=auth_header(creator_token)
    )
    assert ticked.status_code == 200, ticked.text
    return base


async def test_a_source_has_no_rules_until_some_are_saved(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    response = await client.get(f"{source_url}/access-rules", headers=auth_header(creator_token))

    assert response.status_code == 200
    assert response.json() == {"rules": None, "problems": []}


async def test_rules_that_fit_the_database_are_saved_and_read_back(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    saved = await client.put(f"{source_url}/access-rules", json=RULES, headers=auth_header(creator_token))
    read = await client.get(f"{source_url}/access-rules", headers=auth_header(creator_token))

    assert saved.status_code == 200, saved.text
    assert read.json()["rules"] == RULES
    assert read.json()["problems"] == []


async def test_a_rule_naming_a_missing_column_is_refused_naming_that_column(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    broken = {
        **RULES,
        "rules": [{"position": "Nhân viên KD", "views": {"v_ar_open": {"where": {"rep_code": "employee_code"}}}}],
    }

    response = await client.put(f"{source_url}/access-rules", json=broken, headers=auth_header(creator_token))

    assert response.status_code == 422
    assert response.json()["detail"]["problems"] == [
        "view v_ar_open không có cột rep_code (quy tắc của chức vụ Nhân viên KD)"
    ]


async def test_a_rule_on_a_view_that_is_not_ticked_is_refused(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    broken = {**RULES, "rules": [{"position": "Kế toán trưởng", "views": {"v_customer_safe": "all"}}]}

    response = await client.put(f"{source_url}/access-rules", json=broken, headers=auth_header(creator_token))

    assert response.status_code == 422
    assert "v_customer_safe chưa được tick" in response.json()["detail"]["problems"][0]


async def test_rules_without_a_position_attribute_are_refused(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    broken = {**RULES, "identity": {**RULES["identity"], "attributes": {"employee_code": "emp_id"}}}

    response = await client.put(f"{source_url}/access-rules", json=broken, headers=auth_header(creator_token))

    assert response.status_code == 422


async def test_unticking_a_view_the_rules_use_shows_up_as_a_problem(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    await client.put(f"{source_url}/access-rules", json=RULES, headers=auth_header(creator_token))
    await client.patch(source_url, json={"allowed_views": ["v_test_staff"]}, headers=auth_header(creator_token))

    read = await client.get(f"{source_url}/access-rules", headers=auth_header(creator_token))

    assert any("v_ar_open chưa được tick" in problem for problem in read.json()["problems"])


# --------------------------------------------------------------------------- #
# YAML
# --------------------------------------------------------------------------- #


async def test_yaml_comes_out_and_goes_back_in_unchanged(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    await client.put(f"{source_url}/access-rules", json=RULES, headers=auth_header(creator_token))

    exported = await client.get(f"{source_url}/access-rules/yaml", headers=auth_header(creator_token))
    imported = await client.put(
        f"{source_url}/access-rules/yaml",
        content=exported.text.encode(),
        headers={**auth_header(creator_token), "Content-Type": "application/yaml"},
    )
    again = await client.get(f"{source_url}/access-rules/yaml", headers=auth_header(creator_token))

    assert exported.status_code == 200
    assert "Nhân viên KD" in exported.text
    assert imported.status_code == 200, imported.text
    assert imported.json()["rules"] == RULES
    assert again.text == exported.text


async def test_yaml_with_a_bad_column_is_refused_like_json(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    document = "identity:\n  view: v_test_staff\n  match_by: email\n  attributes:\n    position: chuc_vu\nrules: []\n"

    response = await client.put(
        f"{source_url}/access-rules/yaml",
        content=document.encode(),
        headers={**auth_header(creator_token), "Content-Type": "application/yaml"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["problems"] == [
        "view nhân viên v_test_staff không có cột chuc_vu (thuộc tính position)"
    ]


async def test_yaml_that_is_not_access_rules_is_refused(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    response = await client.put(
        f"{source_url}/access-rules/yaml",
        content=b"- not: [a mapping",
        headers={**auth_header(creator_token), "Content-Type": "application/yaml"},
    )

    assert response.status_code == 422


async def test_there_is_no_yaml_before_there_are_rules(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    response = await client.get(f"{source_url}/access-rules/yaml", headers=auth_header(creator_token))

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# The positions the form offers
# --------------------------------------------------------------------------- #


async def test_the_positions_offered_are_the_databases_own(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    response = await client.get(
        f"{source_url}/distinct-values",
        params={"view": "v_test_staff", "column": "position"},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"values": ["Kế toán trưởng", "Nhân viên KD", "Trưởng nhóm KD"]}


async def test_distinct_values_only_reads_a_ticked_view_and_a_column_it_has(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    unticked = await client.get(
        f"{source_url}/distinct-values",
        params={"view": "v_customer_safe", "column": "department"},
        headers=auth_header(creator_token),
    )
    no_column = await client.get(
        f"{source_url}/distinct-values",
        params={"view": "v_test_staff", "column": "salary"},
        headers=auth_header(creator_token),
    )

    assert unticked.status_code == 422
    assert no_column.status_code == 422


async def test_someone_elses_rules_are_not_found(
    client: AsyncClient, second_creator_token: str, source_url: str
) -> None:
    for response in (
        await client.get(f"{source_url}/access-rules", headers=auth_header(second_creator_token)),
        await client.put(f"{source_url}/access-rules", json=RULES, headers=auth_header(second_creator_token)),
        await client.get(f"{source_url}/access-rules/yaml", headers=auth_header(second_creator_token)),
    ):
        assert response.status_code == 404
