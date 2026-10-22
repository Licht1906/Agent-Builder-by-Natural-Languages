"""`GET /admin/ingestion-costs` — the admin's ingest spend board (#101).

Same crossing-ownership shape as `GET /admin/costs` (#11): admin-only, and the
value of the view is that it totals spend across every owner's Knowledge
Bases. Arranged directly against the DB (`record_vlm_spend`) rather than
through a real upload — the upload → cost-row path is already proven end to
end in `tests/test_services/test_ingestion.py`; this suite is only about the
HTTP door onto the aggregate.
"""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from src.auth.passwords import hash_password
from src.config import get_settings
from src.db.models import KnowledgeBase, User, UserRole
from src.services.extraction import VlmSpend
from src.services.ingestion_costs import record_vlm_spend
from src.workflow.documents import store_document
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "tool_file_base_dir", str(tmp_path))


def _fresh_owner(db_session: Session) -> User:
    user = User(email=f"admin-costs-{uuid4().hex}@example.com", password_hash=hash_password("x"), role=UserRole.CREATOR)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _arrange_two_cost_rows(db_session: Session) -> User:
    owner = _fresh_owner(db_session)
    kb = KnowledgeBase(owner_id=owner.id, name="Tủ tài liệu")
    db_session.add(kb)
    db_session.commit()
    db_session.refresh(kb)
    document_a = store_document(db_session, kb, "a.pdf", b"noi dung a")
    document_b = store_document(db_session, kb, "b.pdf", b"noi dung b")
    spend = VlmSpend(model=get_settings().extraction_vlm_model, input_tokens=1000, output_tokens=100, page_count=2)
    record_vlm_spend(db_session, document_a, spend)
    record_vlm_spend(db_session, document_b, spend)
    return owner


async def test_admin_can_read_ingest_spend_grouped_by_owner(client, admin_token, db_session: Session) -> None:
    owner = _arrange_two_cost_rows(db_session)

    response = await client.get(
        "/api/v1/admin/ingestion-costs", params={"group_by": "owner"}, headers=auth_header(admin_token)
    )

    assert response.status_code == 200
    entries = {entry["key"]: entry for entry in response.json()}
    entry = entries[str(owner.id)]
    assert entry["document_count"] == 2
    assert entry["page_count"] == 4
    assert entry["input_tokens"] == 2000
    assert entry["output_tokens"] == 200
    assert entry["usd"] > 0


async def test_group_by_kb_is_also_available(client, admin_token, db_session: Session) -> None:
    _arrange_two_cost_rows(db_session)

    response = await client.get(
        "/api/v1/admin/ingestion-costs", params={"group_by": "kb"}, headers=auth_header(admin_token)
    )

    assert response.status_code == 200
    assert response.json()  # at least the KB just arranged


async def test_group_by_defaults_to_day(client, admin_token, db_session: Session) -> None:
    _arrange_two_cost_rows(db_session)

    response = await client.get("/api/v1/admin/ingestion-costs", headers=auth_header(admin_token))

    assert response.status_code == 200


async def test_an_invalid_group_by_is_refused(client, admin_token) -> None:
    response = await client.get(
        "/api/v1/admin/ingestion-costs", params={"group_by": "year"}, headers=auth_header(admin_token)
    )

    assert response.status_code == 422


async def test_creator_token_is_forbidden(client, creator_token) -> None:
    response = await client.get("/api/v1/admin/ingestion-costs", headers=auth_header(creator_token))

    assert response.status_code == 403
