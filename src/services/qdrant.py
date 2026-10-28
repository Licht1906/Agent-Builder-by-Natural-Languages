"""Qdrant collection setup (RAG-01 / ADR-0044 §1, §3).

One collection, `documents_kb`, shared by every Knowledge Base — tenants are
separated by payload filter (`kb_id`, `owner_id`, `doc_id`), not by one
collection per KB or per user. ADR-0044 §1 chose Qdrant specifically so that
hybrid retrieval is a collection *configuration* — a named dense vector next to
a named sparse vector — rather than a second index that has to be kept in
step with the first (§3).

`ensure_collection` is the only thing this module promised at first — calling
it against a fresh Qdrant creates `documents_kb`; calling it again against an
already-provisioned one is a no-op. RAG-08's ingestion orchestrator (#94) is
what upserts and deletes points, and `delete_document_points`/`upsert_passages`
are the two calls it makes to do that; both live here rather than in the
orchestrator because everything else about the collection's shape (name,
vector names, payload fields) already does. Querying points for retrieval is
a separate concern — `QdrantKnowledgeStore` (ticket #96), a different
implementation reading the same collection.
"""

import math
import re
import zlib
from collections import Counter
from collections.abc import Sequence
from typing import TypedDict

from qdrant_client import QdrantClient, models

from src.config import get_settings

#: Deliberately one collection for every Knowledge Base — see module docstring.
COLLECTION_NAME = "documents_kb"


class PointPayload(TypedDict):
    """The payload every point in `documents_kb` carries — ticket #87's schema.

    Deliberately excludes `article_number` / `law_name` / `effective_date` /
    `status`: `section_path` already carries the article number for documents
    that have one and is `None` for every document that does not, so a legal
    column here would be a vertical leaking into a shared schema (ADR-0044 §6).
    """

    kb_id: str
    owner_id: str
    doc_id: str
    doc_name: str
    section_path: str | None
    #: `None` wherever `Passage.page_number` was — every `.md`/`.txt`/`.docx`
    #: chunk, since those formats have no page concept at all.
    page_number: int | None
    chunk_text: str
    chunk_index: int


#: Matches `bge-m3` (ADR-0044 §2) — verified 1024-dim against `/api/embed`.
DENSE_VECTOR_NAME = "dense"
DENSE_VECTOR_SIZE = 1024
SPARSE_VECTOR_NAME = "sparse"

#: The three keys every filter in the retrieval path uses (kb_id always;
#: owner_id and doc_id on the endpoints that scope to one document or one
#: caller). Indexing exactly these three, and no others, keeps the schema from
#: growing a column per future filter — see the ticket's "Không thuộc ticket
#: này" note on `article_number` et al.
PAYLOAD_INDEX_FIELDS = ("kb_id", "owner_id", "doc_id")


def get_qdrant_client() -> QdrantClient:
    return QdrantClient(url=get_settings().qdrant_url)


def ensure_collection(
    client: QdrantClient, *, collection: str = COLLECTION_NAME, dense_size: int = DENSE_VECTOR_SIZE
) -> None:
    """Create `documents_kb` if absent. Safe to call on every startup.

    Never drops or recreates an existing collection — an idempotent init must
    not be the thing that deletes a Knowledge Base's data on the second run.

    `collection` and `dense_size` are the exported package's (DS-18): its own
    collection, as wide as the company's embedding model makes vectors. The
    shape — named dense + sparse, the payload indexes — is this one.
    """
    if not client.collection_exists(collection):
        client.create_collection(
            collection_name=collection,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=dense_size,
                    distance=models.Distance.COSINE,
                    quantization_config=models.ScalarQuantization(
                        scalar=models.ScalarQuantizationConfig(
                            type=models.ScalarType.INT8,
                            always_ram=True,
                        )
                    ),
                ),
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: models.SparseVectorParams(),
            },
        )

    # `create_payload_index` is itself idempotent on a real Qdrant server (a
    # repeat call against an already-indexed field is a no-op), so this runs
    # unconditionally rather than being gated on the collection-creation branch
    # above — an init run against a collection made by an older version of this
    # script still ends up with the indexes it should have.
    for field_name in PAYLOAD_INDEX_FIELDS:
        client.create_payload_index(
            collection_name=collection,
            field_name=field_name,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )


class QdrantWriteError(RuntimeError):
    """A Qdrant delete/upsert failed. The caller may retry once Qdrant is reachable again.

    Raised by `src.workflow.documents.delete_document` and
    `src.workflow.knowledge_bases.delete_knowledge_base` — never by the plain
    `qdrant_client` calls in this module, which let whatever the client raises
    (a connection error, a non-2xx response) propagate as-is. Wrapping happens
    one layer up, at the point that actually decides a failed Qdrant delete
    must not be followed by a Postgres commit (ADR-0044 §1 / RAG-09).
    """


def delete_document_points(client: QdrantClient, doc_id: str) -> None:
    """Remove every point belonging to `doc_id`. A no-op if there are none.

    The mechanism ADR-0044 §4 relies on: called before every upsert in the
    ingestion orchestrator, first-time ingest included, so a re-ingest never
    needs to know it is one — it just always deletes-then-writes, and the
    first ingest deletes nothing because there is nothing there yet. RAG-09
    calls this too, from `delete_document`, for the same reason a Document's
    points must not outlive its Postgres row.
    """
    client.delete(
        COLLECTION_NAME,
        points_selector=models.FilterSelector(
            filter=models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])
        ),
    )


def delete_knowledge_base_points(client: QdrantClient, kb_id: str) -> None:
    """Remove every point belonging to `kb_id`, across every Document on that shelf. A no-op if there are none.

    RAG-09 / ADR-0044 §1: deleting a Knowledge Base with no matching Qdrant
    cleanup leaves chunks that still search, belonging to a shelf that no
    longer exists — a multi-tenant leak that looks like a cited answer, not
    like a bug. `delete_document_points` alone does not cover this: a KB's
    Documents cascade away in Postgres, but nothing calls this per-document
    delete for each of them, so a KB-scoped filter is its own function rather
    than a loop the caller has to remember to write.
    """
    client.delete(
        COLLECTION_NAME,
        points_selector=models.FilterSelector(
            filter=models.Filter(must=[models.FieldCondition(key="kb_id", match=models.MatchValue(value=kb_id))])
        ),
    )


def upsert_passages(
    client: QdrantClient, points: Sequence[models.PointStruct], *, collection: str = COLLECTION_NAME
) -> None:
    """Write `points` into `documents_kb` (or the package's own collection). The caller decides ids, vectors and payload."""
    if not points:
        return
    client.upsert(collection, points=list(points))


#: What counts as a word for the sparse vector — the same definition
#: `src.workflow.knowledge._TERMS` uses, duplicated rather than imported so
#: this module (write-side collection shape) has no dependency on that one
#: (the toy reader RAG-10 replaces). Unicode by default, the only reason
#: Vietnamese text tokenizes at all.
_SPARSE_TERMS = re.compile(r"\w+", re.UNICODE)

#: A sparse index is a `uint32` on the wire, so a term's hash is masked into
#: that range rather than used as Python's arbitrary-precision `hash()` would
#: give it. Collisions land two unrelated terms on the same dimension; at a
#: demo corpus's vocabulary size that is rarer than it would need to be to
#: change a ranking, and the alternative (a maintained vocabulary table) is
#: exactly the "second index to keep in step" ADR-0044 §3 chose Qdrant to avoid.
_SPARSE_INDEX_MASK = 0x7FFFFFFF


def sparse_vector_of(text: str) -> models.SparseVector:
    """`text` as a hashed bag-of-words sparse vector — RAG-10 / ADR-0044 §3.

    The idea the retriever this replaced scored with (distinct term overlap,
    no IDF, no length normalisation) "done properly", per ADR-0044 §3:
    sublinear term-frequency weighting so a paragraph that repeats one
    word does not out-score one that uses several once each, hashed into a
    fixed index space so no vocabulary table has to be built or kept in step
    with the corpus. Used identically for a chunk at ingest time and for a
    query at search time — the same function is what makes the two
    comparable at all.
    """
    counts = Counter(_SPARSE_TERMS.findall(text.lower()))
    if not counts:
        return models.SparseVector(indices=[], values=[])
    indices = [zlib.crc32(term.encode("utf-8")) & _SPARSE_INDEX_MASK for term in counts]
    values = [1.0 + math.log(count) for count in counts.values()]
    return models.SparseVector(indices=indices, values=values)
