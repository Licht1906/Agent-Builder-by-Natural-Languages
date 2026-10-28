"""The exported package's documents — DS-18 / ADR-0059 §10.

A package whose workflow searches one shelf (`documents/11/`), indexed into an
in-memory Qdrant through a scripted OpenAI-compatible `/embeddings` endpoint
that counts what it is sent. The ticket's three criteria:

- after start-up, an answer from `knowledge_search` cites the document;
- a second start-up indexes nothing again (one probe, no passage embedded);
- with the reranker off, search still answers.

Beside them: what changes re-index (content, model), what is removed, a
scanned file with and without the platform's extraction, the start-up
refusals, and the platform's half — writing `documents/` for an export.
"""

import hashlib
import json
import re
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from qdrant_client import QdrantClient, models
from sqlalchemy.orm import Session

from src.agents.package_documents import write_documents
from src.db.models import KnowledgeBase, KnowledgeBaseMode, User
from src.services.extraction import ExtractedBlock, ExtractionResult
from src.standalone.app import create_app
from src.standalone.config import RuntimeConfigError, load_runtime_config
from src.standalone.documents import EXTRACTIONS_DIR, MODEL_FIELD, NoDocuments, prepare_knowledge
from src.standalone.package import Package, prepare_database
from src.workflow.documents import store_document
from tests.conftest import ScriptedRun, parse_sse
from tests.test_standalone.test_standalone_runtime import _loaded, _platform_factory, _views, package_dir, secrets_env

pytestmark = pytest.mark.asyncio

# Scenery borrowed from `test_standalone_runtime`, named so pytest sees it.
__all__ = ["_views", "package_dir", "secrets_env"]

SHELF = "11"
COLLECTION = "agent_documents"
HANDBOOK = """# Sổ tay nhân viên

## Điều 1. Nghỉ phép năm

Nhân viên chính thức được nghỉ phép năm 12 ngày, cộng thêm một ngày cho mỗi năm năm làm việc.

## Điều 2. Làm việc từ xa

Nhân viên được làm việc từ xa tối đa hai ngày mỗi tuần khi trưởng nhóm đồng ý.
"""
SEARCH = {
    "embedding": {"base_url": "http://embed.internal/v1", "model": "bge-m3", "api_key_secret": "llm_key"},
    "qdrant": {"url": "http://qdrant:6333", "collection": COLLECTION},
}


def _workflow() -> dict[str, Any]:
    return {
        "start_node_id": "search",
        "nodes": [
            {"id": "search", "type": "knowledge_search", "config": {"source_ids": [SHELF]}},
            {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời theo sổ tay.", "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "search", "source_port": "found", "target_node_id": "answer"},
            {"id": "e2", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {},
    }


class Embeddings:
    """A scripted `/embeddings` endpoint: a hashed bag of words, so a question lands near the passage that shares its words."""

    def __init__(self, width: int = 64) -> None:
        self.width = width
        self.batches: list[list[str]] = []
        self.models: list[str] = []

    def vector(self, text: str) -> list[float]:
        vector = [0.0] * self.width
        for word in re.findall(r"\w+", text.lower()):
            vector[zlib.crc32(word.encode()) % self.width] += 1.0
        return vector

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert request.url.path == "/v1/embeddings"
            self.batches.append(body["input"])
            self.models.append(body["model"])
            return httpx.Response(
                200,
                json={"data": [{"index": i, "embedding": self.vector(text)} for i, text in enumerate(body["input"])]},
            )

        return httpx.MockTransport(handle)

    @property
    def passages_embedded(self) -> int:
        """Everything sent but the start-up probe (one text per start)."""
        return sum(len(batch) for batch in self.batches) - sum(
            1 for batch in self.batches if batch == ["kiểm tra kết nối embedding"]
        )


@pytest.fixture
def documents_package(package_dir: Callable[..., Path], secrets_env: None) -> Callable[..., Path]:
    """A package that searches shelf 11, holding the handbook."""

    def _write(**runtime: Any) -> Path:
        directory = package_dir(workflow=_workflow(), **{**SEARCH, **runtime})
        (directory / "documents" / SHELF).mkdir(parents=True, exist_ok=True)
        (directory / "documents" / SHELF / "so-tay.md").write_text(HANDBOOK, encoding="utf-8")
        return directory

    return _write


def _prepared(package: Package, qdrant: QdrantClient, embeddings: Embeddings, **seams: Any) -> Any:
    return prepare_knowledge(
        package.config,
        package.shelves,
        package.secrets,
        base=package.base,
        qdrant_client=qdrant,
        embedding_transport=embeddings.transport(),
        **seams,
    )


def _points(qdrant: QdrantClient, **match: str) -> int:
    conditions = [models.FieldCondition(key=key, match=models.MatchValue(value=value)) for key, value in match.items()]
    return qdrant.count(COLLECTION, count_filter=models.Filter(must=conditions), exact=True).count


# --------------------------------------------------------------------------- #
# The acceptance criteria
# --------------------------------------------------------------------------- #


async def test_after_start_up_an_answer_cites_the_document(
    documents_package: Callable[..., Path], scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun]
) -> None:
    package = _loaded(documents_package())
    knowledge, report = _prepared(package, QdrantClient(":memory:"), Embeddings())
    assert report.indexed == [f"{SHELF}/so-tay.md"]

    app = create_app(package, model_factory=_platform_factory(), knowledge=knowledge)
    app.state.ids = prepare_database(package)
    scripted_chat_model_factory([[AIMessage(content="Bạn được nghỉ phép năm 12 ngày (Điều 1).")]])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://package") as client:
        answered = await client.post(
            "/chat", json={"message": "nhân viên được nghỉ phép năm bao nhiêu ngày"}, headers={"X-Asker": "an@cty.vn"}
        )

    [searched] = [line for line in parse_sse(answered.text, event="tool_result") if line["tool"] == "knowledge_search"]
    assert searched["ok"] is True
    cited = searched["citations"][0]
    assert cited["doc_name"] == "so-tay.md"
    assert "Điều 1" in (cited["section_path"] or "") and "12 ngày" in cited["text"]


def test_a_second_start_up_indexes_nothing_again(documents_package: Callable[..., Path]) -> None:
    package = _loaded(documents_package())
    qdrant, first, second = QdrantClient(":memory:"), Embeddings(), Embeddings()

    _prepared(package, qdrant, first)
    _, report = _prepared(package, qdrant, second)

    assert first.passages_embedded > 0
    assert report.indexed == [] and report.kept == [f"{SHELF}/so-tay.md"]
    assert second.passages_embedded == 0, "only the start-up probe may reach the embedding endpoint"


def test_with_the_reranker_off_search_still_answers_and_with_it_on_it_is_asked(
    documents_package: Callable[..., Path],
) -> None:
    question = "làm việc từ xa mấy ngày mỗi tuần"
    off = _loaded(documents_package())
    store, _ = _prepared(off, QdrantClient(":memory:"), Embeddings())
    asked: list[dict[str, Any]] = []

    def reranker(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body)
        order = sorted(range(len(body["documents"])), key=lambda i: "từ xa" not in body["documents"][i])
        return httpx.Response(200, json={"ranked_indices": order, "scores": [1.0] * len(order)})

    on = _loaded(documents_package(reranker={"enabled": True, "url": "http://reranker.internal"}))
    reranked, _ = _prepared(
        on, QdrantClient(":memory:"), Embeddings(), reranker_transport=httpx.MockTransport(reranker)
    )
    down, _ = _prepared(
        on,
        QdrantClient(":memory:"),
        Embeddings(),
        reranker_transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )

    assert off.config.reranker.enabled is False
    assert "từ xa" in store.search(question, source_refs=[SHELF])[0].text
    assert "từ xa" in reranked.search(question, source_refs=[SHELF])[0].text and asked
    assert down.search(question, source_refs=[SHELF]), "a reranker that is down degrades, it does not fail"


# --------------------------------------------------------------------------- #
# What re-indexes, and what goes
# --------------------------------------------------------------------------- #


def test_a_replaced_file_is_indexed_again_and_its_old_text_is_gone(documents_package: Callable[..., Path]) -> None:
    directory = documents_package()
    qdrant = QdrantClient(":memory:")
    _prepared(_loaded(directory), qdrant, Embeddings())
    old = hashlib.sha256(HANDBOOK.encode()).hexdigest()

    (directory / "documents" / SHELF / "so-tay.md").write_text(HANDBOOK.replace("12 ngày", "14 ngày"), encoding="utf-8")
    _, report = _prepared(_loaded(directory), qdrant, Embeddings())

    assert report.indexed == [f"{SHELF}/so-tay.md"]
    assert _points(qdrant, doc_id=old) == 0
    assert all("12 ngày" not in point.payload["chunk_text"] for point in qdrant.scroll(COLLECTION, limit=100)[0])


def test_a_removed_file_can_no_longer_be_cited(documents_package: Callable[..., Path]) -> None:
    directory = documents_package()
    (directory / "documents" / SHELF / "phu-luc.md").write_text("# Phụ lục\n\nBảng lương tham khảo.", encoding="utf-8")
    qdrant = QdrantClient(":memory:")
    _prepared(_loaded(directory), qdrant, Embeddings())
    assert _points(qdrant, doc_name="phu-luc.md") > 0

    (directory / "documents" / SHELF / "phu-luc.md").unlink()
    _prepared(_loaded(directory), qdrant, Embeddings())

    assert _points(qdrant, doc_name="phu-luc.md") == 0
    assert _points(qdrant, doc_name="so-tay.md") > 0


def test_another_embedding_model_indexes_everything_again(documents_package: Callable[..., Path]) -> None:
    qdrant = QdrantClient(":memory:")
    _prepared(_loaded(documents_package()), qdrant, Embeddings())
    other = documents_package(embedding={**SEARCH["embedding"], "model": "e5-large"})

    _, report = _prepared(_loaded(other), qdrant, Embeddings())

    assert report.indexed == [f"{SHELF}/so-tay.md"]
    assert _points(qdrant, **{MODEL_FIELD: "bge-m3"}) == 0


def test_a_model_of_another_width_rebuilds_the_collection(documents_package: Callable[..., Path]) -> None:
    qdrant = QdrantClient(":memory:")
    _prepared(_loaded(documents_package()), qdrant, Embeddings(width=64))

    _, report = _prepared(_loaded(documents_package()), qdrant, Embeddings(width=32))

    assert report.indexed == [f"{SHELF}/so-tay.md"]
    assert qdrant.get_collection(COLLECTION).config.params.vectors["dense"].size == 32


def test_a_scanned_file_uses_the_platforms_extraction_and_is_reported_without_one(
    documents_package: Callable[..., Path],
) -> None:
    directory = documents_package()
    scanned = b"%PDF-1.4 scanned page, no text layer"
    (directory / "documents" / SHELF / "quyet-dinh.pdf").write_bytes(scanned)

    _, without = _prepared(_loaded(directory), QdrantClient(":memory:"), Embeddings())
    extraction = ExtractionResult(
        blocks=(
            ExtractedBlock(kind="heading", text="Quyết định số 12", page_number=1, extraction_confidence="high"),
            ExtractedBlock(
                kind="text", text="Thưởng Tết bằng một tháng lương.", page_number=1, extraction_confidence="high"
            ),
        ),
        source="vlm",
    )
    (directory / "documents" / EXTRACTIONS_DIR).mkdir()
    (directory / "documents" / EXTRACTIONS_DIR / f"{hashlib.sha256(scanned).hexdigest()}.json").write_text(
        extraction.model_dump_json(), encoding="utf-8"
    )
    qdrant = QdrantClient(":memory:")
    store, with_it = _prepared(_loaded(directory), qdrant, Embeddings())

    assert f"{SHELF}/quyet-dinh.pdf" in without.failed and f"{SHELF}/so-tay.md" in without.indexed
    assert f"{SHELF}/quyet-dinh.pdf" in with_it.indexed
    [found] = [p for p in store.search("thưởng tết", source_refs=[SHELF]) if p.doc_name == "quyet-dinh.pdf"][:1]
    assert found.page_number == 1


def test_a_shelf_the_package_does_not_carry_is_named_when_searched(documents_package: Callable[..., Path]) -> None:
    store, _ = _prepared(_loaded(documents_package()), QdrantClient(":memory:"), Embeddings())

    with pytest.raises(Exception, match="99"):
        store.search("nghỉ phép", source_refs=["99"])


# --------------------------------------------------------------------------- #
# Start-up
# --------------------------------------------------------------------------- #


def test_a_shelf_without_its_folder_or_without_embedding_and_qdrant_refuses_to_start(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    with pytest.raises(RuntimeConfigError) as caught:
        _loaded(package_dir(workflow=_workflow()))

    message = str(caught.value)
    assert f"kho tài liệu {SHELF}" in message
    assert "embedding" in message and "qdrant" in message


def test_an_embedding_endpoint_that_does_not_answer_stops_the_start(documents_package: Callable[..., Path]) -> None:
    package = _loaded(documents_package())

    with pytest.raises(RuntimeConfigError, match="embedding"):
        prepare_knowledge(
            package.config,
            package.shelves,
            package.secrets,
            base=package.base,
            qdrant_client=QdrantClient(":memory:"),
            embedding_transport=httpx.MockTransport(lambda request: httpx.Response(502)),
        )


def test_a_package_that_searches_nothing_dials_nothing(package_dir: Callable[..., Path], secrets_env: None) -> None:
    package = _loaded(package_dir())

    store, report = prepare_knowledge(package.config, package.shelves, package.secrets, base=package.base)

    assert isinstance(store, NoDocuments) and report.indexed == []


def test_the_sample_runtime_yaml_documents_the_search_settings() -> None:
    config = load_runtime_config(Path(__file__).resolve().parents[2] / "deploy" / "package" / "runtime.yaml")

    assert config.embedding is not None and config.qdrant is not None
    assert config.reranker.enabled is False


# --------------------------------------------------------------------------- #
# The platform's half: writing documents/ for an export
# --------------------------------------------------------------------------- #


class _Extractions:
    def __init__(self, known: dict[str, ExtractionResult]) -> None:
        self.known = known

    def get(self, digest: str) -> ExtractionResult | None:
        return self.known.get(digest)

    def set(self, digest: str, result: ExtractionResult) -> None:
        self.known[digest] = result


def test_the_platform_writes_each_searched_shelfs_files_and_extractions_and_a_package_indexes_them(
    db_session: Session, seeded_creator: User, documents_package: Callable[..., Path], tmp_path: Path
) -> None:
    shelf = KnowledgeBase(owner_id=seeded_creator.id, name="Sổ tay DS-18")
    elsewhere = KnowledgeBase(owner_id=seeded_creator.id, name="Kho riêng", mode=KnowledgeBaseMode.BYODB)
    db_session.add_all([shelf, elsewhere])
    db_session.flush()
    handbook = store_document(db_session, shelf, "so-tay.md", HANDBOOK.encode())
    db_session.commit()
    extraction = ExtractionResult(
        blocks=(
            ExtractedBlock(kind="text", text="Nghỉ phép năm 12 ngày.", page_number=None, extraction_confidence="high"),
        ),
        source="text_layer",
    )
    workflow = {
        "nodes": [{"id": "s", "type": "knowledge_search", "config": {"source_ids": [str(shelf.id), str(elsewhere.id)]}}]
    }

    try:
        written = write_documents(
            db_session,
            seeded_creator.id,
            workflow,
            tmp_path / "documents",
            extraction_cache=_Extractions({handbook.content_sha256: extraction}),
        )
    finally:
        db_session.delete(shelf)
        db_session.delete(elsewhere)
        db_session.commit()

    assert written.files == [f"{shelf.id}/so-tay.md"]
    assert (tmp_path / "documents" / str(shelf.id) / "so-tay.md").read_text(encoding="utf-8") == HANDBOOK
    assert (tmp_path / "documents" / EXTRACTIONS_DIR / f"{handbook.content_sha256}.json").is_file()
    assert any("byodb" in problem for problem in written.problems)
