"""The `documents/` folder of an exported package — DS-18 / ADR-0059 §10.

What a package needs to answer from the same documents the agent was built
on, written for the export (DS-19) to put in its zip:

    documents/<knowledge base id>/<file>        each Document's original bytes
    documents/.extraction/<sha256>.json         the platform's extraction of it

The extraction travels so the package never has to send a scanned page to a
vision model from inside the company: its ingest finds the result already
made, keyed by content hash, exactly as the platform's own cache is
(`src.services.extraction.FileExtractionCache`). Only the shelves a
`knowledge_search` node searches, and only the owner's.

A `byodb` shelf lives in the creator's own vector database and has no files
to carry; it is reported rather than silently left out.
"""

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Document, KnowledgeBase, KnowledgeBaseMode
from src.models.node_registry import NodeType
from src.services.extraction import ExtractionCache, get_extraction_cache
from src.workflow.documents import path_of

#: Mirrors `src.standalone.documents.EXTRACTIONS_DIR` — spelled here so the platform never imports the package.
EXTRACTIONS_DIR = ".extraction"


@dataclass
class WrittenDocuments:
    """What went into `documents/`, and what could not."""

    files: list[str] = field(default_factory=list)
    extractions: int = 0
    problems: list[str] = field(default_factory=list)


def searched_shelves(workflow: dict[str, Any] | None) -> list[str]:
    """The Knowledge Base ids a stored graph's `knowledge_search` nodes search — read as stored JSON."""
    refs = {
        str(ref)
        for node in (workflow or {}).get("nodes", [])
        if isinstance(node, dict)
        and node.get("type") == NodeType.KNOWLEDGE_SEARCH
        and isinstance(node.get("config"), dict)
        for ref in node["config"].get("source_ids") or []
    }
    return sorted(refs)


def write_documents(
    db: Session,
    owner_id: int,
    workflow: dict[str, Any] | None,
    into: Path,
    *,
    extraction_cache: ExtractionCache | None = None,
) -> WrittenDocuments:
    """Every Document of every shelf `workflow` searches, and its extraction, under `into`."""
    cache = extraction_cache if extraction_cache is not None else get_extraction_cache()
    wanted = searched_shelves(workflow)
    ids = [int(ref) for ref in wanted if ref.isdigit()]
    shelves = {
        str(kb.id): kb
        for kb in db.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(ids), KnowledgeBase.owner_id == owner_id))
    }
    written = WrittenDocuments()
    for ref in wanted:
        kb = shelves.get(ref)
        if kb is None:
            written.problems.append(f"kho tài liệu {ref} không tồn tại hoặc không phải của chủ agent")
            continue
        if kb.mode is KnowledgeBaseMode.BYODB:
            written.problems.append(
                f"kho {kb.name!r} đọc từ database vector riêng (byodb) — không có file để mang theo"
            )
            continue
        shelf = into / ref
        shelf.mkdir(parents=True, exist_ok=True)
        taken: set[str] = set()
        for document in db.scalars(select(Document).where(Document.kb_id == kb.id).order_by(Document.id)):
            source = path_of(document)
            if not source.is_file():
                written.problems.append(f"tệp {document.file_name!r} của kho {kb.name!r} không còn trên đĩa")
                continue
            name = _unique(document.file_name, taken)
            shutil.copyfile(source, shelf / name)
            written.files.append(f"{ref}/{name}")
            extraction = cache.get(document.content_sha256)
            if extraction is not None:
                target = into / EXTRACTIONS_DIR / f"{document.content_sha256}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(extraction.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")
                written.extractions += 1
    return written


def _unique(name: str, taken: set[str]) -> str:
    """`name`, or `name (2)`… — two Documents on one shelf may share a name; two files in a folder may not."""
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    candidate, counter = name, 2
    while candidate in taken:
        candidate = f"{stem} ({counter}).{suffix}" if suffix else f"{stem} ({counter})"
        counter += 1
    taken.add(candidate)
    return candidate
