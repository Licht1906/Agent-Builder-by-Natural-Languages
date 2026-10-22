"""Uploading Documents onto a Knowledge Base, and the isolation that makes it safe — #90.

Two claims are being made here and they are different sizes. The small one is
that a creator can attach a `.md`, a `.txt`, a `.pdf` or a `.docx` to their own
shelf and get a Document back. The large one is that a Document **cannot reach
another creator's shelf** — which is not enforced by a check anybody wrote for
it, but by every path being rooted at the owning creator's own directory.

Every test runs against a workspace this file owns, so nothing here can read or
write the developer's own `./data/workspace`.
"""

import io
from collections.abc import Generator
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import Document
from src.deployments.rate_limit import SlidingWindowLimiter
from src.main import app
from src.services.extraction import ExtractedBlock, get_vlm_page_client
from src.services.qdrant import get_qdrant_client
from src.services.upload_rate_limit import get_upload_rate_limiter
from src.workflow.documents import MAX_DOCUMENT_BYTES, path_of
from src.workflow.knowledge import KnowledgeSourceError
from tests.conftest import SECOND_CREATOR_EMAIL, auth_header, knowledge_store_for

pytestmark = pytest.mark.asyncio

HANDBOOK = "Nghi phep\n\nNhan vien duoc nghi 12 ngay phep mot nam.\n"


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """Point `TOOL_FILE_BASE_DIR` at a directory this test owns, and nothing else."""
    monkeypatch.setattr(get_settings(), "tool_file_base_dir", str(tmp_path))
    yield tmp_path


@pytest.fixture(autouse=True)
def fresh_upload_rate_limiter() -> Generator[SlidingWindowLimiter, None, None]:
    """Give every test its own upload counters (#101) — the process-wide ones would leak between tests.

    Same reasoning `test_deployment_rate_limit.py`'s `fresh_limiter` gives for
    the Deployment ceiling: without this, one test's uploads would be the next
    test's starting point.
    """
    limiter = SlidingWindowLimiter()
    app.dependency_overrides[get_upload_rate_limiter] = lambda: limiter
    yield limiter
    app.dependency_overrides.pop(get_upload_rate_limiter, None)


def upload(name: str, content: bytes) -> dict[str, tuple[str, io.BytesIO, str]]:
    return {"file": (name, io.BytesIO(content), "application/octet-stream")}


async def _kb_id(client: AsyncClient, token: str, name: str = "Tủ tài liệu") -> int:
    response = await client.post("/api/v1/knowledge-bases", json={"name": name}, headers=auth_header(token))
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --------------------------------------------------------------------------- #
# Uploading and listing
# --------------------------------------------------------------------------- #


async def test_uploading_a_text_document_hands_back_a_document(client: AsyncClient, creator_token: str) -> None:
    kb_id = await _kb_id(client, creator_token)

    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["file_name"] == "so-tay.md"
    assert body["byte_size"] == len(HANDBOOK.encode())
    assert body["status"] == "pending"
    assert isinstance(body["id"], int)


async def test_an_uploaded_document_appears_in_the_list(client: AsyncClient, creator_token: str) -> None:
    kb_id = await _kb_id(client, creator_token)
    await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )

    listed = await client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", headers=auth_header(creator_token))

    assert listed.status_code == 200
    assert [document["file_name"] for document in listed.json()] == ["so-tay.md"]


async def test_two_documents_on_one_kb_may_share_a_name(client: AsyncClient, creator_token: str) -> None:
    """Identity is `id`, not `file_name` — see `src.db.models.Document`."""
    kb_id = await _kb_id(client, creator_token)
    await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", b"ban thao luan"),
        headers=auth_header(creator_token),
    )

    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", b"ban chinh thuc"),
        headers=auth_header(creator_token),
    )

    assert response.status_code == 201, response.text
    listed = await client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", headers=auth_header(creator_token))
    assert len(listed.json()) == 2
    assert {document["file_name"] for document in listed.json()} == {"so-tay.md"}


async def test_reuploading_identical_content_into_the_same_kb_is_refused(
    client: AsyncClient, creator_token: str
) -> None:
    """`content_sha256` is unique per KB — the same file twice on one shelf is a wasted upload, not a new Document."""
    kb_id = await _kb_id(client, creator_token)
    await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )

    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("ban-sao.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422
    assert "so-tay.md" in response.json()["detail"]


async def test_the_same_content_in_two_different_kbs_is_valid(client: AsyncClient, creator_token: str) -> None:
    kb_a = await _kb_id(client, creator_token, "Tu A")
    kb_b = await _kb_id(client, creator_token, "Tu B")
    first = await client.post(
        f"/api/v1/knowledge-bases/{kb_a}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )
    second = await client.post(
        f"/api/v1/knowledge-bases/{kb_b}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text


async def test_one_creator_never_sees_anothers_knowledge_base(
    client: AsyncClient, creator_token: str, second_creator_token: str
) -> None:
    kb_id = await _kb_id(client, creator_token)

    response = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=auth_header(second_creator_token))

    assert response.status_code == 404


async def test_a_path_in_the_filename_cannot_escape_the_workspace(
    client: AsyncClient, creator_token: str, workspace: Path
) -> None:
    """`../../.env` is stored as a file called `.env` under the caller's own tree.

    The store would refuse to *read* an escaping ref anyway, but a write is the
    one operation where being refused afterwards is too late.
    """
    kb_id = await _kb_id(client, creator_token)

    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("../../secrets.md", b"x"),
        headers=auth_header(creator_token),
    )

    assert response.status_code == 201, response.text
    assert response.json()["file_name"] == "secrets.md"
    assert not (workspace / "secrets.md").exists()


async def test_an_unsupported_type_is_refused_at_upload(client: AsyncClient, creator_token: str) -> None:
    """Told now, not on a run three screens later."""
    kb_id = await _kb_id(client, creator_token)

    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("script.exe", b"MZ"),
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422
    assert ".pdf" in response.json()["detail"]


async def test_an_empty_file_is_refused(client: AsyncClient, creator_token: str) -> None:
    kb_id = await _kb_id(client, creator_token)

    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("trong.md", b""), headers=auth_header(creator_token)
    )

    assert response.status_code == 422


async def test_a_file_over_the_ceiling_is_refused(client: AsyncClient, creator_token: str) -> None:
    kb_id = await _kb_id(client, creator_token)

    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files=upload("to.md", b"x" * (MAX_DOCUMENT_BYTES + 1)),
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422


async def test_deleting_a_document_removes_it(client: AsyncClient, creator_token: str) -> None:
    kb_id = await _kb_id(client, creator_token)
    uploaded = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )
    document_id = uploaded.json()["id"]

    deleted = await client.delete(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{document_id}", headers=auth_header(creator_token)
    )

    assert deleted.status_code == 204
    listed = await client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", headers=auth_header(creator_token))
    assert listed.json() == []


async def test_deleting_a_document_removes_its_qdrant_points(
    client: AsyncClient, creator_token: str, _no_live_ingestion_backends
) -> None:
    """RAG-09: the point half of the delete, not just the Postgres row."""
    kb_id = await _kb_id(client, creator_token)
    uploaded = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )
    document_id = uploaded.json()["id"]
    points_before, _ = _no_live_ingestion_backends.scroll("documents_kb", limit=100)
    assert any(point.payload["doc_id"] == str(document_id) for point in points_before), "arrange: ingest must have run"

    deleted = await client.delete(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{document_id}", headers=auth_header(creator_token)
    )

    assert deleted.status_code == 204
    points_after, _ = _no_live_ingestion_backends.scroll("documents_kb", limit=100)
    assert not any(point.payload["doc_id"] == str(document_id) for point in points_after)


async def test_a_dead_qdrant_refuses_the_delete_with_a_503_and_keeps_the_row(
    client: AsyncClient, creator_token: str
) -> None:
    """RAG-09's ordering, from the wire: Qdrant fails -> Postgres never commits -> retryable."""
    kb_id = await _kb_id(client, creator_token)
    uploaded = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )
    document_id = uploaded.json()["id"]

    class _DeadQdrant:
        def delete(self, *args: object, **kwargs: object) -> None:
            raise ConnectionError("Qdrant không phản hồi")

    app.dependency_overrides[get_qdrant_client] = lambda: _DeadQdrant()
    try:
        response = await client.delete(
            f"/api/v1/knowledge-bases/{kb_id}/documents/{document_id}", headers=auth_header(creator_token)
        )
    finally:
        app.dependency_overrides.pop(get_qdrant_client, None)

    assert response.status_code == 503
    listed = await client.get(f"/api/v1/knowledge-bases/{kb_id}/documents", headers=auth_header(creator_token))
    assert [document["id"] for document in listed.json()] == [document_id]


async def test_deleting_something_that_is_not_there_is_a_404(client: AsyncClient, creator_token: str) -> None:
    kb_id = await _kb_id(client, creator_token)

    response = await client.delete(f"/api/v1/knowledge-bases/{kb_id}/documents/999999", headers=auth_header(creator_token))

    assert response.status_code == 404


async def test_documents_need_a_session(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/knowledge-bases/1/documents")).status_code == 401


# --------------------------------------------------------------------------- #
# Upload rate limiting (#101) — the ceiling on how often one owner may set
# the ingest pipeline going, charged before anything is read or written.
# --------------------------------------------------------------------------- #


async def test_uploads_past_the_per_minute_ceiling_are_refused_with_a_429(
    client: AsyncClient,
    creator_token: str,
    fresh_upload_rate_limiter: SlidingWindowLimiter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "document_upload_rate_limit_per_user", 2)
    kb_id = await _kb_id(client, creator_token)
    for index in range(2):
        ok = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            files=upload(f"file-{index}.md", f"noi dung {index}".encode()),
            headers=auth_header(creator_token),
        )
        assert ok.status_code == 201, ok.text

    refused = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files=upload("one-too-many.md", b"noi dung khac han"),
        headers=auth_header(creator_token),
    )

    assert refused.status_code == 429
    assert "Retry-After" in refused.headers


async def test_the_ceiling_is_per_owner_not_shared_across_callers(
    client: AsyncClient,
    creator_token: str,
    second_creator_token: str,
    fresh_upload_rate_limiter: SlidingWindowLimiter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One owner spending their whole allowance must not refuse a different owner's upload."""
    monkeypatch.setattr(get_settings(), "document_upload_rate_limit_per_user", 1)
    first_kb = await _kb_id(client, creator_token, "Tủ của A")
    used_up = await client.post(
        f"/api/v1/knowledge-bases/{first_kb}/documents",
        files=upload("a.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )
    assert used_up.status_code == 201, used_up.text

    second_kb = await _kb_id(client, second_creator_token, "Tủ của B")
    response = await client.post(
        f"/api/v1/knowledge-bases/{second_kb}/documents",
        files=upload("b.md", HANDBOOK.encode()),
        headers=auth_header(second_creator_token),
    )

    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- #
# Ingestion (RAG-08): upload runs extract -> chunk -> embed -> upsert in the
# background. `_no_live_ingestion_backends` (autouse, tests/conftest.py)
# means the pipeline itself has already finished by the time `await
# client.post(...)` hands the response back — see that fixture's docstring.
# --------------------------------------------------------------------------- #


async def test_getting_one_document_reports_its_pipeline_status(client: AsyncClient, creator_token: str) -> None:
    kb_id = await _kb_id(client, creator_token)
    uploaded = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )
    document_id = uploaded.json()["id"]

    response = await client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{document_id}", headers=auth_header(creator_token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "indexed"
    assert body["status_detail"] is None
    assert body["chunk_count"] > 0


async def test_getting_a_document_that_is_not_there_is_a_404(client: AsyncClient, creator_token: str) -> None:
    kb_id = await _kb_id(client, creator_token)

    response = await client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents/999999", headers=auth_header(creator_token)
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# "xem lại" — the original bytes, for review (RAG-12 / #98)
# --------------------------------------------------------------------------- #


async def test_reading_a_documents_content_hands_back_the_original_bytes(
    client: AsyncClient, creator_token: str
) -> None:
    kb_id = await _kb_id(client, creator_token)
    uploaded = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )
    document_id = uploaded.json()["id"]

    response = await client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{document_id}/content", headers=auth_header(creator_token)
    )

    assert response.status_code == 200
    assert response.content == HANDBOOK.encode()
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert "so-tay.md" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].startswith("inline")


async def test_reading_content_of_a_document_that_is_not_there_is_a_404(
    client: AsyncClient, creator_token: str
) -> None:
    kb_id = await _kb_id(client, creator_token)

    response = await client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents/999999/content", headers=auth_header(creator_token)
    )

    assert response.status_code == 404


async def test_reading_content_of_someone_elses_document_is_a_404(
    client: AsyncClient, creator_token: str, second_creator_token: str
) -> None:
    kb_id = await _kb_id(client, creator_token)
    uploaded = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )
    document_id = uploaded.json()["id"]

    response = await client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{document_id}/content", headers=auth_header(second_creator_token)
    )

    assert response.status_code == 404


async def test_a_broken_pdf_ends_failed_with_a_readable_reason_and_the_service_stands(
    client: AsyncClient, creator_token: str
) -> None:
    """Upload itself must still succeed — it is the *pipeline*, not the request, that fails."""
    kb_id = await _kb_id(client, creator_token)

    uploaded = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("hong.pdf", b"not a real pdf"),
        headers=auth_header(creator_token),
    )
    assert uploaded.status_code == 201, uploaded.text

    response = await client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{uploaded.json()['id']}", headers=auth_header(creator_token)
    )
    body = response.json()
    assert body["status"] == "failed"
    assert body["status_detail"]
    assert body["chunk_count"] == 0


async def test_reingest_resets_to_pending_before_the_response_goes_out(
    client: AsyncClient, creator_token: str
) -> None:
    kb_id = await _kb_id(client, creator_token)
    uploaded = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )
    document_id = uploaded.json()["id"]

    reingested = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{document_id}/reingest", headers=auth_header(creator_token)
    )

    assert reingested.status_code == 202, reingested.text
    assert reingested.json()["status"] == "pending"


async def test_reingest_after_editing_the_file_replaces_what_search_finds(
    client: AsyncClient, creator_token: str, seeded_creator, db_session: Session, _no_live_ingestion_backends
) -> None:
    """The ticket's own claim: search stops surfacing the old content, it does not sit beside the new."""
    kb_id = await _kb_id(client, creator_token)
    uploaded = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )
    document_id = uploaded.json()["id"]
    document = db_session.get(Document, document_id)
    assert document is not None
    path_of(document).write_bytes(b"Chinh sach moi\n\nMoi thu da thay doi hoan toan.\n")

    reingested = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{document_id}/reingest", headers=auth_header(creator_token)
    )
    assert reingested.status_code == 202, reingested.text

    response = await client.get(
        f"/api/v1/knowledge-bases/{kb_id}/documents/{document_id}", headers=auth_header(creator_token)
    )
    assert response.json()["status"] == "indexed"

    points, _ = _no_live_ingestion_backends.scroll("documents_kb", limit=100)
    texts = [point.payload["chunk_text"] for point in points]
    assert any("thay doi hoan toan" in text for text in texts)
    assert not any("12 ngay phep" in text for text in texts)


async def test_reingest_of_a_document_that_is_not_there_is_a_404(client: AsyncClient, creator_token: str) -> None:
    kb_id = await _kb_id(client, creator_token)

    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/999999/reingest", headers=auth_header(creator_token)
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# What the store makes of what was uploaded
# --------------------------------------------------------------------------- #


async def test_an_uploaded_document_is_searchable_by_the_owners_store(
    client: AsyncClient, creator_token: str, seeded_creator, db_session: Session, _no_live_ingestion_backends
) -> None:
    """The upload and the search agree about what a ref means — the whole point."""
    kb_id = await _kb_id(client, creator_token)
    await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.md", HANDBOOK.encode()),
        headers=auth_header(creator_token),
    )

    store = knowledge_store_for(db_session, seeded_creator.id, _no_live_ingestion_backends)
    passages = store.search("nghi phep bao nhieu ngay", source_refs=[str(kb_id)])

    assert passages, "tài liệu vừa tải lên phải tìm được"
    assert any("12 ngay phep" in passage.text for passage in passages)


async def test_a_ref_naming_another_creators_knowledge_base_is_refused(
    client: AsyncClient,
    second_creator_token: str,
    seeded_creator,
    db_session: Session,
    _no_live_ingestion_backends,
) -> None:
    """The claim the whole isolation design rests on.

    The second creator uploads something; a store scoped to the *first*
    creator is then handed the shelf id it landed on. Nothing about the id
    itself says whose it is — `owner_id` is baked into the store at
    construction, so the shelf simply does not exist from where this search is
    standing, and it is refused by name rather than quietly contributing
    nothing.
    """
    kb_id = await _kb_id(client, second_creator_token, name="Tủ của người khác")
    await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("luong.md", b"bang luong"),
        headers=auth_header(second_creator_token),
    )

    store = knowledge_store_for(db_session, seeded_creator.id, _no_live_ingestion_backends)

    with pytest.raises(KnowledgeSourceError, match=str(kb_id)):
        store.search("luong", source_refs=[str(kb_id)])


async def test_a_pdf_is_extracted_into_searchable_text(
    client: AsyncClient, creator_token: str, seeded_creator, db_session: Session, _no_live_ingestion_backends
) -> None:
    kb_id = await _kb_id(client, creator_token)
    pdf = _one_page_pdf("Nhan vien duoc nghi 12 ngay phep mot nam")
    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("so-tay.pdf", pdf), headers=auth_header(creator_token)
    )
    assert response.status_code == 201, response.text

    store = knowledge_store_for(db_session, seeded_creator.id, _no_live_ingestion_backends)
    passages = store.search("nghi phep", source_refs=[str(kb_id)])

    assert passages, "PDF phải đọc được thành văn bản"
    assert any("12 ngay phep" in passage.text for passage in passages)


async def test_a_docx_is_extracted_into_searchable_text(
    client: AsyncClient, creator_token: str, seeded_creator, db_session: Session, _no_live_ingestion_backends
) -> None:
    kb_id = await _kb_id(client, creator_token)
    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files=upload("so-tay.docx", _docx("Nhan vien duoc nghi 12 ngay phep mot nam")),
        headers=auth_header(creator_token),
    )
    assert response.status_code == 201, response.text

    store = knowledge_store_for(db_session, seeded_creator.id, _no_live_ingestion_backends)
    passages = store.search("nghi phep", source_refs=[str(kb_id)])

    assert passages, "DOCX phải đọc được thành văn bản"
    assert any("12 ngay phep" in passage.text for passage in passages)


async def test_a_pdf_with_no_text_layer_goes_to_the_vlm_rather_than_indexing_empty(
    client: AsyncClient, creator_token: str, seeded_creator, db_session: Session, _no_live_ingestion_backends
) -> None:
    """A scan has no text layer to read, and indexing it as an empty document is the silent failure.

    Before extraction moved to ingest time the store refused a scan outright,
    which at least *said* "this needs OCR". The pipeline now answers the same
    situation by rendering the page and asking a vision model (RAG-06), so the
    claim worth pinning is that the fallback actually runs — a scan that
    reached Qdrant with no passages would be a shelf that silently covers
    nothing.
    """
    seen: list[int] = []

    def _reading_vlm(image_bytes: bytes, page_number: int) -> list[ExtractedBlock]:
        seen.append(page_number)
        return [
            ExtractedBlock(
                kind="text",
                text="Nhan vien duoc nghi 12 ngay phep mot nam",
                page_number=page_number,
                extraction_confidence="high",
            )
        ]

    app.dependency_overrides[get_vlm_page_client] = lambda: _reading_vlm
    try:
        kb_id = await _kb_id(client, creator_token)
        uploaded = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents", files=upload("scan.pdf", _blank_pdf()),
            headers=auth_header(creator_token),
        )
        assert uploaded.status_code == 201, uploaded.text
    finally:
        app.dependency_overrides.pop(get_vlm_page_client, None)

    assert seen, "trang scan phải được đưa qua mô hình thị giác"
    store = knowledge_store_for(db_session, seeded_creator.id, _no_live_ingestion_backends)
    assert store.search("nghi phep", source_refs=[str(kb_id)])


def _one_page_pdf(text: str) -> bytes:
    """A real one-page PDF carrying `text`, built with the library that reads it back."""
    from pypdf import PdfWriter

    # `pypdf` writes structure but not content streams, so the page's text is
    # composed by hand — which is also what makes this a genuine extraction test
    # rather than a round trip through one library's own serialiser.
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    body = b"%PDF-1.4\n"
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode()
    body += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    )
    assert PdfWriter  # the reader's own library is installed; extraction is asserted above
    return body


def _blank_pdf() -> bytes:
    """A structurally valid PDF with no text at all — what a scan looks like to pypdf."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _docx(text: str) -> bytes:
    from docx import Document as DocxDocument

    document = DocxDocument()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


assert SECOND_CREATOR_EMAIL  # imported for the fixtures it names; kept honest here
