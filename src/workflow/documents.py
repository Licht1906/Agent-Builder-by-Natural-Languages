"""Storing the files a Knowledge Base's Documents point at — #90 / ADR-0043.

A Document's identity is its `id`, not its file name (`src.db.models.Document`
— see that class's docstring for why). This module is the other half of that
promise: it decides where the bytes behind a Document actually live on disk,
and nothing outside it is allowed to assume a name.

Each owner still gets a directory of their own (`library_of`), and each
Knowledge Base its own subdirectory under that — the second level is what
makes two shelves able to hold a document called the same thing without one
overwriting the other, which a flat per-owner directory could not do once a
name stopped being an address. **Containment is still the whole isolation
mechanism**: a path built from `owner_id`, `kb_id` and `document.id` can only
ever land inside that owner's own tree, so nothing needs to *check* whose
document a request names — a run can only ever be pointed at one owner's rows
in the first place.

Two things are refused at upload rather than at run time, unchanged from
before: a name that is not a plain file name, and a type nothing can extract
text from. A third is new — the same content already sitting on this shelf —
because `documents.content_sha256` is unique per Knowledge Base and a
duplicate here is never a legitimate second Document, only a wasted upload.
"""

import hashlib
import re
import unicodedata
from pathlib import Path

from qdrant_client import QdrantClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import Document, KnowledgeBase, KnowledgeBaseMode
from src.services.qdrant import QdrantWriteError, delete_document_points, get_qdrant_client

#: What can be turned into text, and therefore what may be uploaded. Extraction
#: for each lives in `src.workflow.knowledge`; this is the same set stated where
#: an upload is refused, so the two cannot disagree about what is supported.
SUPPORTED_SUFFIXES = frozenset({".md", ".txt", ".pdf", ".docx"})

#: Big enough for a policy handbook, small enough that a mistaken upload does not
#: fill a disk. Enforced against the bytes actually read, never against a
#: `Content-Length` the client supplied.
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024

#: Where every owner's directory lives, under `TOOL_FILE_BASE_DIR`. A level of
#: its own so that documents cannot collide with whatever else an operator has
#: put in the workspace — V1's `read_file` tool stands on the same ground.
#: A path detail, not a domain term — see `library_of`.
_OWNERS_DIR = "creators"

#: Anything that is not a letter, a digit or one of `. _ -`. Applied after
#: Unicode normalisation, so a Vietnamese file name survives as something
#: readable instead of being replaced wholesale.
_UNSAFE = re.compile(r"[^\w.\-]+", re.UNICODE)


class DocumentError(ValueError):
    """An upload that cannot be accepted. The message is shown to the creator."""


def library_of(owner_id: int) -> Path:
    """The directory holding `owner_id`'s Knowledge Bases, created if it is not there yet.

    An internal path helper, not a domain concept — `library` stops being a
    word this platform's vocabulary uses (`CONTEXT.md`), and this function is
    the one place the word survives, naming a directory rather than a shelf.

    Keyed by account id rather than by email: an id is stable, and an email is a
    string somebody may one day be allowed to change — at which point every
    Document stored under it would point at a directory that moved.
    """
    directory = Path(get_settings().tool_file_base_dir).resolve() / _OWNERS_DIR / str(owner_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _safe_file_name(filename: str) -> str:
    """`filename` reduced to a plain file name, or a refusal saying why it cannot be.

    Only the final component survives, so a client sending `../../.env` is
    naming a file called `.env` rather than anywhere else. That is belt and
    braces — `path_of` builds every path from `owner_id`/`kb_id`/`document.id`
    rather than from anything a caller sends, so nothing here can actually
    escape its root — but a write is the one operation where being refused
    afterwards is too late.
    """
    stem = Path(filename.replace("\\", "/")).name
    normalised = unicodedata.normalize("NFC", stem).strip()
    cleaned = _UNSAFE.sub("-", normalised).strip(".-")
    if not cleaned:
        raise DocumentError("Tên tệp không hợp lệ")
    suffix = Path(cleaned).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise DocumentError(f"Chỉ hỗ trợ {supported} — tệp {stem!r} không đọc được thành văn bản")
    return cleaned


def path_of(document: Document) -> Path:
    """Where `document`'s bytes live on disk.

    Built from `owner_id`/`kb_id`/`document.id` — never from `file_name` alone
    — so two Documents on the same shelf that happen to share a name never
    collide, and a Document's file cannot be found by guessing its name.
    """
    directory = library_of(document.knowledge_base.owner_id) / str(document.kb_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{document.id}__{document.file_name}"


def store_document(db: Session, knowledge_base: KnowledgeBase, filename: str, content: bytes) -> Document:
    """Add one Document to `knowledge_base` and write its bytes to disk.

    Refuses a second upload of **the same content already on this shelf**
    (`documents.uq_documents_kb_content`) — the same file attached to a
    *different* shelf is a different, legitimate Document, so the check is
    scoped to `knowledge_base` rather than to the owner as a whole.

    Unlike the old file-identity design, re-uploading a file under the same
    *name* no longer replaces anything: two Documents may share a name because
    neither is addressed by it any more, only by `id`. A creator publishing a
    corrected handbook adds a new Document and removes the old one themselves —
    `store_document` does not guess which upload was meant to replace which.

    The size is checked against the bytes in hand rather than against a
    header, because a header is a claim the uploader makes about itself.

    A `byodb` shelf refuses every upload (RAG-22 / #108). The mode-picker
    screen promises a creator that what is in their own database is neither
    indexed nor changed by this platform, and a shelf that quietly accepted
    Documents alongside that promise would make it false — the accepted file
    would be ingested into *this* platform's Qdrant and searched next to the
    external collection, which is precisely the thing the creator was told
    would not happen.
    """
    if knowledge_base.mode is KnowledgeBaseMode.BYODB:
        raise DocumentError(
            f"Tủ {knowledge_base.name!r} đọc từ database riêng của bạn — hệ thống không index hay sửa dữ liệu ở đó. "
            "Thêm tài liệu trực tiếp vào database đó, hoặc tạo một tủ do hệ thống tự quản lý."
        )
    if not content:
        raise DocumentError("Tệp rỗng — không có gì để tìm trong đó")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise DocumentError(f"Tệp vượt quá {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB")

    safe_name = _safe_file_name(filename)
    digest = hashlib.sha256(content).hexdigest()
    duplicate = db.execute(
        select(Document).where(Document.kb_id == knowledge_base.id, Document.content_sha256 == digest)
    ).scalar_one_or_none()
    if duplicate is not None:
        raise DocumentError(f"Tệp này đã có trong tủ tài liệu này, dưới tên {duplicate.file_name!r}")

    document = Document(
        knowledge_base=knowledge_base, file_name=safe_name, content_sha256=digest, byte_size=len(content)
    )
    db.add(document)
    db.flush()  # assigns document.id, without committing — path_of needs it

    path_of(document).write_bytes(content)

    db.commit()
    db.refresh(document)
    return document


def list_documents(db: Session, knowledge_base: KnowledgeBase) -> list[Document]:
    """Every Document on `knowledge_base`, newest first.

    Newest first because the list is read straight after an upload, and the
    Document somebody just added is the one they are looking for.
    """
    return list(
        db.execute(
            select(Document)
            .where(Document.kb_id == knowledge_base.id)
            .order_by(Document.uploaded_at.desc(), Document.id.desc())
        )
        .scalars()
        .all()
    )


def delete_document(db: Session, document: Document, qdrant_client: QdrantClient | None = None) -> None:
    """Remove one Document — its Qdrant points, its row, and the bytes behind it.

    Deliberately does **not** go looking for workflows that named it. A
    confirmed graph is immutable (ADR-0022 §5), so editing one out from under
    its owner is not available even in principle; that stays a question for
    whatever reads `source_ids` at run time (`src.workflow.knowledge`).

    **Qdrant first, Postgres commit last** (RAG-09 / ADR-0044 §1): a chunk
    that outlives its Postgres row is still searchable and cites a Document
    that no longer exists, which is a multi-tenant leak wearing a cleanup
    bug's clothes. Deleting Qdrant's points before `db.commit()` means a
    Qdrant failure leaves the row untouched — `db.delete(document)` alone
    stages nothing until commit — so a retry of the same request is exactly
    the right thing to do, not a special recovery path.
    """
    qdrant = qdrant_client if qdrant_client is not None else get_qdrant_client()
    try:
        delete_document_points(qdrant, str(document.id))
    except Exception as exc:  # noqa: BLE001 — any Qdrant failure must block the Postgres commit, not just some
        raise QdrantWriteError(
            f"Không xoá được dữ liệu Qdrant cho tài liệu {document.file_name!r} — thử lại: {exc}"
        ) from exc

    path_of(document).unlink(missing_ok=True)
    db.delete(document)
    db.commit()
