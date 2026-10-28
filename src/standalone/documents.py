"""The exported package's documents — DS-18 / ADR-0059 §10.

A `knowledge_search` node names Knowledge Base ids. In the package those
shelves are folders: `documents/<knowledge base id>/<file>`, the original
files the creator uploaded, plus `documents/.extraction/<sha256>.json` — the
platform's own extraction of each, so a scanned page that took a vision model
to read on the platform is not sent to one from inside the company.

**Indexed at start-up, once.** `prepare_knowledge` reads every file, and one
whose content hash is already in the package's Qdrant — under the same
embedding model — is left alone; anything else goes through the platform's
own extraction and chunking (`src.services.extraction`, `src.services.chunking`)
and the company's embedding endpoint (`embedding.base_url`). A file removed or
replaced since, and every point a different embedding model wrote, is deleted.
A second start therefore embeds nothing but one probe.

**Searched as the platform searches.** Dense and sparse over one collection,
RRF-fused by Qdrant, reranked when `reranker.enabled` and in fused order when
not, spread across sections — `src.workflow.knowledge`'s own functions, over
the package's collection instead of `documents_kb`.
"""

import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from qdrant_client import QdrantClient, models

from src.services.chunking import chunk_document
from src.services.extraction import ExtractedBlock, ExtractionResult, extract_document
from src.services.qdrant import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    ensure_collection,
    sparse_vector_of,
    upsert_passages,
)
from src.services.reranker import RerankerClient, rerank_at
from src.standalone.config import EmbeddingConfig, RuntimeConfig, RuntimeConfigError
from src.workflow.knowledge import (
    DEFAULT_PASSAGE_LIMIT,
    KnowledgeSourceError,
    KnowledgeStore,
    Passage,
    fused_points,
    passages_of_points,
)

_log = logging.getLogger(__name__)

#: The bundle's extractions, beside the shelves: `<documents>/.extraction/<sha256>.json`.
EXTRACTIONS_DIR = ".extraction"

#: Point ids from `(shelf, content, chunk)`: an ingest that is retried lands on the same points.
_POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "agent-builder.package.documents")

#: What a point carries beyond `PointPayload`: the model that embedded it, so a model change re-indexes.
MODEL_FIELD = "embedding_model"

Embedder = Callable[[Sequence[str]], list[list[float]]]


class NoDocuments:
    """The `KnowledgeStore` of a package whose workflow searches no documents."""

    def search(
        self, query: str, *, source_refs: Sequence[str], limit: int = DEFAULT_PASSAGE_LIMIT
    ) -> tuple[Passage, ...]:
        return ()


@dataclass(frozen=True)
class IngestReport:
    """What one start-up did: files indexed now, files already indexed, files that could not be."""

    indexed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PackageKnowledgeStore:
    """The package's shelves in its own Qdrant — `KnowledgeStore` for `knowledge_search` nodes."""

    client: QdrantClient
    collection: str
    embed: Embedder
    shelves: frozenset[str]
    reranker: RerankerClient | None = None

    def search(
        self, query: str, *, source_refs: Sequence[str], limit: int = DEFAULT_PASSAGE_LIMIT
    ) -> tuple[Passage, ...]:
        refs = [str(ref) for ref in source_refs]
        missing = sorted(set(refs) - self.shelves)
        if missing:
            # Named, never dropped — `KnowledgeSourceError`'s reason on the platform holds here.
            raise KnowledgeSourceError(f"Gói không mang kho tài liệu: {', '.join(missing)}")
        if not refs or not query.strip():
            return ()
        source_filter = models.Filter(must=[models.FieldCondition(key="kb_id", match=models.MatchAny(any=refs))])
        points = fused_points(self.client, self.collection, query, self.embed([query])[0], source_filter)
        return passages_of_points(query, points, limit=limit, reranker_client=self.reranker)


def missing_shelves(root: Path, shelves: Sequence[str]) -> list[str]:
    """The shelves the workflow searches that the bundle has no folder for."""
    return [shelf for shelf in shelves if not (root / shelf).is_dir()]


def openai_compatible_embedder(
    config: EmbeddingConfig, api_key: str | None, *, transport: httpx.BaseTransport | None = None
) -> Embedder:
    """`POST {base_url}/embeddings` in batches — any OpenAI-compatible server (vLLM, TEI, Ollama, LiteLLM…)."""

    def embed(texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        with httpx.Client(timeout=config.timeout_seconds, transport=transport) as client:
            for start in range(0, len(texts), config.batch_size):
                batch = list(texts[start : start + config.batch_size])
                try:
                    reply = client.post(
                        f"{config.base_url.rstrip('/')}/embeddings",
                        json={"model": config.model, "input": batch},
                        headers=headers,
                    )
                    reply.raise_for_status()
                    data = sorted(reply.json()["data"], key=lambda item: item["index"])
                except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                    raise EmbeddingEndpointError(
                        f"Endpoint embedding {config.base_url} không trả được vector: {exc}"
                    ) from None
                if len(data) != len(batch):
                    raise EmbeddingEndpointError(
                        f"Gửi {len(batch)} đoạn nhưng endpoint embedding trả {len(data)} vector"
                    )
                vectors.extend([list(map(float, item["embedding"])) for item in data])
        return vectors

    return embed


class EmbeddingEndpointError(RuntimeError):
    """The company's embedding endpoint did not give back one vector per passage."""


def prepare_knowledge(
    config: RuntimeConfig,
    shelves: Sequence[str],
    secrets: Mapping[str, str],
    *,
    base: Path,
    qdrant_client: QdrantClient | None = None,
    embedding_transport: httpx.BaseTransport | None = None,
    reranker_transport: httpx.BaseTransport | None = None,
) -> tuple[KnowledgeStore, IngestReport]:
    """The package's documents indexed (what is not already) and the store that searches them.

    No shelf searched → `NoDocuments`, and nothing is dialled. Otherwise
    `embedding` and `qdrant` are required (`load_package` has said so already);
    an embedding endpoint or a Qdrant that does not answer stops the start.
    `qdrant_client` and the transports are test seams.
    """
    if not shelves:
        return NoDocuments(), IngestReport()
    if config.embedding is None or config.qdrant is None:
        raise RuntimeConfigError("Workflow tìm trong tài liệu nhưng runtime.yaml thiếu embedding hoặc qdrant")
    root = _path(config.documents, base)
    embed = openai_compatible_embedder(
        config.embedding, secrets.get(config.embedding.api_key_secret or ""), transport=embedding_transport
    )
    client = qdrant_client if qdrant_client is not None else QdrantClient(url=config.qdrant.url)
    try:
        report = ingest_documents(
            root, shelves, client=client, collection=config.qdrant.collection, embed=embed, model=config.embedding.model
        )
    except EmbeddingEndpointError as exc:
        raise RuntimeConfigError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 — Qdrant unreachable or refusing: a reason not to start, by name
        raise RuntimeConfigError(f"Không ghi được tài liệu vào Qdrant {config.qdrant.url}: {exc}") from None
    reranker = (
        rerank_at(config.reranker.url, timeout=config.reranker.timeout_seconds, transport=reranker_transport)
        if config.reranker.enabled
        else None
    )
    store = PackageKnowledgeStore(
        client=client, collection=config.qdrant.collection, embed=embed, shelves=frozenset(shelves), reranker=reranker
    )
    return store, report


def ingest_documents(
    root: Path, shelves: Sequence[str], *, client: QdrantClient, collection: str, embed: Embedder, model: str
) -> IngestReport:
    """Every file on `shelves` in `collection`, each indexed at most once per content and model.

    One embedding call is made whatever happens — the probe that says how wide
    this model's vectors are. A collection of another width was written by
    another model, and is rebuilt.
    """
    width = len(embed(["kiểm tra kết nối embedding"])[0])
    if client.collection_exists(collection):
        found = client.get_collection(collection).config.params.vectors
        current = found.get(DENSE_VECTOR_NAME) if isinstance(found, dict) else None
        if current is None or current.size != width:
            _log.warning(
                "Collection %s có vector khác cỡ (%s ≠ %s) — dựng lại", collection, current and current.size, width
            )
            client.delete_collection(collection)
    ensure_collection(client, collection=collection, dense_size=width)

    extractions = root / EXTRACTIONS_DIR
    report = IngestReport()
    present: list[str] = []
    for shelf in shelves:
        for path in sorted(
            item for item in (root / shelf).iterdir() if item.is_file() and not item.name.startswith(".")
        ):
            label = f"{shelf}/{path.name}"
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            present.append(digest)
            if _indexed(client, collection, shelf, digest, model):
                report.kept.append(label)
                continue
            try:
                extraction = extract_document(
                    content, path.name, cache=_BundledExtractions(extractions), vlm_client=_no_vision_model
                )
                passages = chunk_document(extraction)
                vectors = embed([passage.embeddable for passage in passages]) if passages else []
            except EmbeddingEndpointError:
                raise
            except Exception as exc:  # noqa: BLE001 — one unreadable file is reported; the others are still indexed
                report.failed[label] = str(exc)
                _log.warning("Không index được %s: %s", label, exc)
                continue
            _delete(client, collection, models.FieldCondition(key="doc_id", match=models.MatchValue(value=digest)))
            upsert_passages(
                client,
                [
                    models.PointStruct(
                        id=str(uuid.uuid5(_POINT_NAMESPACE, f"{shelf}:{digest}:{index}")),
                        vector={DENSE_VECTOR_NAME: vector, SPARSE_VECTOR_NAME: sparse_vector_of(passage.embeddable)},
                        payload={
                            "kb_id": shelf,
                            "owner_id": "package",
                            "doc_id": digest,
                            "doc_name": path.name,
                            "section_path": passage.section_path,
                            "page_number": passage.page_number,
                            "chunk_text": passage.text,
                            "chunk_index": index,
                            MODEL_FIELD: model,
                        },
                    )
                    for index, (passage, vector) in enumerate(zip(passages, vectors, strict=True))
                ],
                collection=collection,
            )
            report.indexed.append(label)
    # What no file on disk is any more, and what another model embedded: gone, so neither can be cited.
    if present:
        client.delete(
            collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(must_not=[models.FieldCondition(key="doc_id", match=models.MatchAny(any=present))])
            ),
        )
    else:
        client.delete(collection, points_selector=models.FilterSelector(filter=models.Filter()))
    client.delete(
        collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must_not=[models.FieldCondition(key=MODEL_FIELD, match=models.MatchValue(value=model))]
            )
        ),
    )
    return report


def _indexed(client: QdrantClient, collection: str, shelf: str, digest: str, model: str) -> bool:
    found = client.count(
        collection,
        count_filter=models.Filter(
            must=[
                models.FieldCondition(key="kb_id", match=models.MatchValue(value=shelf)),
                models.FieldCondition(key="doc_id", match=models.MatchValue(value=digest)),
                models.FieldCondition(key=MODEL_FIELD, match=models.MatchValue(value=model)),
            ]
        ),
        exact=True,
    )
    return found.count > 0


def _delete(client: QdrantClient, collection: str, condition: models.FieldCondition) -> None:
    client.delete(collection, points_selector=models.FilterSelector(filter=models.Filter(must=[condition])))


@dataclass(frozen=True)
class _BundledExtractions:
    """The platform's extractions, read from the bundle; nothing written back (the folder is mounted read-only)."""

    directory: Path

    def get(self, digest: str) -> ExtractionResult | None:
        path = self.directory / f"{digest}.json"
        return ExtractionResult.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None

    def set(self, digest: str, result: ExtractionResult) -> None:
        return None


def _no_vision_model(image: bytes, page_number: int) -> list[ExtractedBlock]:
    raise RuntimeError(
        f"trang {page_number} là ảnh scan và bundle không mang bản trích xuất của nó — gói không gửi ảnh ra "
        "mô hình thị giác bên ngoài; xuất lại gói từ nền tảng để kèm documents/.extraction"
    )


def _path(value: str, base: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path
