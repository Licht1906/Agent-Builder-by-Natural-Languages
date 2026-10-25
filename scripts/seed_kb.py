#!/usr/bin/env python3
"""Load the demo corpus into two Knowledge Bases (RAG-17 / #103).

Idempotent — safe to run on every fresh environment and safe to run twice in
a row: a Knowledge Base is looked up by name before being created, and a
document is skipped (not re-ingested) once its content already sits on its
target shelf — the same `content_sha256` de-duplication `store_document`
already enforces on every real upload, so this script never has to invent a
second definition of "already loaded".

Runs the real pipeline, not a shortcut around it: `store_document` writes the
row and the bytes exactly as an upload would, and `ingest_document` runs
synchronously (there is no HTTP request here for `BackgroundTasks` to detach
from) against the real embedding client and the real Qdrant collection. Every
seed file is `.md`, so extraction takes RAG-06's text-layer fast path and
never calls the VLM — this script never spends real OpenRouter money.

Each shelf names its files explicitly rather than globbing the directory —
`seed_corpus/company_policies/NOTES.md` documents this shelf's deliberate
law/policy contradictions right next to the files it is about, and a glob
would try to ingest that note as if it were a fourth company policy.

Usage:
    python scripts/seed_kb.py
"""

import sys
from dataclasses import dataclass
from pathlib import Path

from qdrant_client import QdrantClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.base import SessionLocal
from src.db.models import KnowledgeBase, User
from src.services.ingestion import ingest_document
from src.services.qdrant import ensure_collection, get_qdrant_client
from src.workflow.documents import DocumentError, store_document

_SEED_ROOT = Path(__file__).resolve().parent.parent / "seed_corpus"


@dataclass(frozen=True, slots=True)
class SeedShelf:
    """One Knowledge Base this script loads: its name, description, and the exact files that belong on it.

    `name` is the lookup key idempotency is built on — a second run finds the
    same shelf by name rather than making a duplicate. `files` is explicit
    rather than a directory glob so a note living beside the real documents
    (see `company_policies/NOTES.md`) is never mistaken for a fourth one.
    """

    name: str
    description: str
    directory: Path
    files: tuple[str, ...]


_SEED_SHELVES: tuple[SeedShelf, ...] = (
    SeedShelf(
        name="Quy định lao động",
        description="5 văn bản pháp luật lao động công khai — seed demo (RAG-17 / #103)",
        directory=_SEED_ROOT / "labor_law",
        files=(
            "bo-luat-lao-dong-2019.md",
            "nghi-dinh-145-2020.md",
            "luat-bao-hiem-xa-hoi-2014.md",
            "nghi-dinh-12-2022.md",
            "luat-an-toan-ve-sinh-lao-dong-2015.md",
        ),
    ),
    SeedShelf(
        name="Chính sách công ty",
        description="3 chính sách nội bộ giả lập — seed demo (RAG-17 / #103)",
        directory=_SEED_ROOT / "company_policies",
        files=(
            "quy-che-nghi-phep-lam-them-gio.md",
            "chinh-sach-cong-tac-phi.md",
            "quy-trinh-xin-duyet-chi.md",
        ),
    ),
    # Kệ của ba agent Phòng Kinh doanh (`docs/TAO-4-AGENT-BANG-APP.md`). Tên trùng
    # đúng tên Knowledge Base mà tài liệu đó bảo tạo, nên `_find_or_create_kb` nhận
    # lại kệ đang có chứ không dựng kệ thứ hai cùng tên. Ba agent ấy cùng đọc một kệ
    # là điểm của chúng, nên ba file này đi cùng nhau hoặc không file nào cả.
    SeedShelf(
        name="Chính sách Kinh doanh — công ty",
        description="Chính sách công nợ/chiết khấu, mẫu biên bản làm việc, công tác phí — giả lập",
        directory=_SEED_ROOT / "company_policies",
        files=(
            "chinh-sach-cong-no-va-chiet-khau.md",
            "mau-bien-ban-lam-viec-voi-khach.md",
            "chinh-sach-cong-tac-phi.md",
        ),
    ),
)


def _find_or_create_kb(db: Session, owner_id: int, name: str, description: str) -> tuple[KnowledgeBase, bool]:
    existing = db.execute(
        select(KnowledgeBase).where(KnowledgeBase.owner_id == owner_id, KnowledgeBase.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    kb = KnowledgeBase(owner_id=owner_id, name=name, description=description)
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return kb, True


def _seed_shelf(db: Session, qdrant: QdrantClient, owner_id: int, shelf: SeedShelf) -> None:
    kb, created = _find_or_create_kb(db, owner_id, shelf.name, shelf.description)
    print(f"{'Created' if created else 'Reusing'} Knowledge Base {shelf.name!r} (id={kb.id})")

    for filename in shelf.files:
        path = shelf.directory / filename
        content = path.read_bytes()
        try:
            document = store_document(db, kb, filename, content)
        except DocumentError as exc:
            print(f"  {filename}: skipped — {exc}")
            continue

        # Its own session, on purpose — `ingest_document` closes whatever
        # session it was handed once it's done (the same reasoning
        # `document_routes.py` relies on for the `BackgroundTasks` case: it
        # runs detached, with no caller left to hand the session back to).
        # Reusing `db` here would leave every row this loop already
        # touched — `kb` included — detached the moment the first document
        # finishes. `db.refresh` afterwards sees it: `ingest_document` fully
        # commits and closes its session before returning, and this
        # process's default Postgres isolation is READ COMMITTED.
        ingest_document(document.id, qdrant_client=qdrant)
        db.refresh(document)
        print(f"  {filename}: {document.status.value} ({document.chunk_count} passages)")


def main() -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        creator = db.execute(select(User).where(User.email == settings.seed_creator_email)).scalar_one_or_none()
        if creator is None:
            print(f"No user {settings.seed_creator_email!r} — run migrations first (alembic upgrade head).")
            sys.exit(1)

        qdrant = get_qdrant_client()
        ensure_collection(qdrant)

        for shelf in _SEED_SHELVES:
            if not shelf.directory.is_dir():
                print(f"Skipping {shelf.name!r} — no source directory at {shelf.directory}")
                continue
            _seed_shelf(db, qdrant, creator.id, shelf)
    finally:
        db.close()


if __name__ == "__main__":
    main()
