"""Manual Qdrant/Postgres reconciliation (RAG-09 / ADR-0044 §1).

ADR-0044 §1 names the gap this module exists for plainly: choosing Qdrant
over `pgvector` means chunks live outside Postgres, and **no transaction
spans both stores**. `src.workflow.knowledge_bases.delete_knowledge_base`
and `src.workflow.documents.delete_document` close the ordinary path — Qdrant
deleted before Postgres commits, so a Qdrant failure leaves nothing to clean
up — but "the ordinary path" is not the only way a Postgres row can vanish.
An operator running a manual `DELETE` against the database, a migration that
truncates a table, or any bug in code written before this ticket landed all
bypass the ordering those functions enforce. What is left behind is an
**orphaned point**: still searchable, still carrying a citation, naming a
`kb_id`/`doc_id` no Postgres row answers to any more — the multi-tenant leak
the parent ticket's whole reasoning is about, just arriving by a door other
than the ones this codebase's own delete paths guard.

This is a **manual, run-by-hand** job on purpose, not a background sweep:
finding an orphan here is a signal that something *else* went wrong, and the
honest response is a report a person reads, not a silent auto-delete that
would hide the same evidence a real bug produced.
"""

from dataclasses import dataclass

from qdrant_client import QdrantClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Document, KnowledgeBase
from src.services.qdrant import COLLECTION_NAME, delete_document_points, delete_knowledge_base_points

#: How many points `_distinct_ids` reads per `scroll` call — large enough that
#: a demo-scale corpus finishes in one or two pages, small enough not to pull
#: an unbounded response into memory on a corpus nobody has sized yet.
_SCROLL_PAGE_SIZE = 1000


@dataclass(frozen=True)
class OrphanReport:
    """Every `kb_id`/`doc_id` Qdrant still carries points for, with no matching Postgres row.

    Two separate lists rather than one, because they are found and deleted
    by two different filters (`delete_knowledge_base_points` vs.
    `delete_document_points`) — a point can be doc-orphaned, kb-orphaned, or
    (the ordinary case: a whole shelf deleted out from under Postgres) both.
    """

    orphaned_kb_ids: tuple[str, ...]
    orphaned_doc_ids: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not self.orphaned_kb_ids and not self.orphaned_doc_ids


def _distinct_ids(client: QdrantClient, field: str) -> set[str]:
    """Every distinct value `field` (`"kb_id"` or `"doc_id"`) takes across every point in `documents_kb`.

    A plain `scroll` over the whole collection reading one payload field,
    rather than a faceting call — `qdrant_client`'s local/embedded mode
    (what the test suite runs against) does not support server-side facets,
    and a demo-scale corpus makes a full scroll cheap enough not to matter.
    """
    ids: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            COLLECTION_NAME, with_payload=[field], with_vectors=False, limit=_SCROLL_PAGE_SIZE, offset=offset
        )
        for point in points:
            value = point.payload.get(field) if point.payload else None
            if value is not None:
                ids.add(value)
        if offset is None:
            break
    return ids


def find_orphans(client: QdrantClient, db: Session) -> OrphanReport:
    """Every `kb_id`/`doc_id` present in Qdrant with no matching Postgres row.

    Sorted numerically (every id here is a stringified integer primary key)
    so two runs against an unchanged database produce byte-identical output —
    worth having for a report a person diffs run to run.
    """
    qdrant_kb_ids = _distinct_ids(client, "kb_id")
    qdrant_doc_ids = _distinct_ids(client, "doc_id")

    postgres_kb_ids = {str(row) for row in db.execute(select(KnowledgeBase.id)).scalars()}
    postgres_doc_ids = {str(row) for row in db.execute(select(Document.id)).scalars()}

    return OrphanReport(
        orphaned_kb_ids=tuple(sorted(qdrant_kb_ids - postgres_kb_ids, key=int)),
        orphaned_doc_ids=tuple(sorted(qdrant_doc_ids - postgres_doc_ids, key=int)),
    )


def delete_orphans(client: QdrantClient, report: OrphanReport) -> None:
    """Delete every point `report` named. Re-run `find_orphans` first if time has passed since it ran.

    Both lists are deleted — not just `orphaned_doc_ids` — even though a
    kb-orphaned point is necessarily also doc-orphaned (a KB's Documents
    cascade away in Postgres together): the `kb_id` filter is the backstop
    for a point whose `doc_id` field is missing or does not match anything
    `find_orphans` recognized, so it is not skipped just because the other
    filter already caught it.
    """
    for kb_id in report.orphaned_kb_ids:
        delete_knowledge_base_points(client, kb_id)
    for doc_id in report.orphaned_doc_ids:
        delete_document_points(client, doc_id)
