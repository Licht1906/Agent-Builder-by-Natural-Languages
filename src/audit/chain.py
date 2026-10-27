"""An append-only hash chain of what V2 runs read and did — DS-10, UC-1's `audit_log` carried into V2.

**One entry per `data_query` read and per action a person had to approve**
(ADR-0059 §11): who asked, which Hệ CSDL, which view, the scope applied and
how many rows — never the rows; which call, its DS-08 `action_hash`, who
approved it and what happened — never the email.

**Each entry seals the one before it.** `entry_hash` is SHA-256 over the
previous entry's hash and the canonical JSON of every field of this one,
`seq` and time included. Edit any field of any entry and that entry's hash
no longer matches; recompute it to hide the edit and the *next* entry's
`prev_hash` no longer does. `verify_chain` walks the chain and names the
first `seq` where either happens — or where one is missing, or where the
chain stops short of its recorded head.

**Appends are serialised on `audit_chain_head`**, one row locked `FOR UPDATE`,
because two runs appending at once would otherwise both link to the same
last entry and fork the chain. Each append is its own short transaction on
its own session: an audit entry is not part of anybody else's unit of work,
and must not be rolled back with it.

UC-1 computed the chain in a `plpgsql` trigger. Here it is Python, so the
exported package (DS-13) keeps the same chain on whatever database it is
pointed at.
"""

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.base import SessionLocal
from src.db.models import AuditChainHead, AuditEvent

#: The `prev_hash` of the first entry — there is nothing before it to seal.
GENESIS_HASH = "0" * 64

#: The one row of `audit_chain_head`.
_HEAD_ID = 1


class AuditKind(StrEnum):
    """What an entry records."""

    DATA_QUERY = "data_query"
    ACTION = "action"


class AuditMode(StrEnum):
    """Which world the run that wrote an entry lives in."""

    TRIAL = "trial"
    PROD = "prod"
    EVAL = "eval"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One entry before it is chained — what the runtime hands an `AuditSink`."""

    kind: AuditKind
    mode: AuditMode
    agent_id: int | None
    version_id: int | None
    trace_id: str
    run_id: str
    node_id: str
    details: dict[str, Any] = field(default_factory=dict)


#: Where the runtime writes entries. The platform's is `record_audit_event`;
#: the exported package passes its own.
AuditSink = Callable[[AuditEntry], None]


@dataclass(frozen=True, slots=True)
class ChainVerdict:
    """What `verify_chain` found: intact, or the first `seq` where it is not, and why."""

    ok: bool
    checked: int
    broken_at: int | None = None
    reason: str | None = None


def entry_hash(prev_hash: str, fields: dict[str, Any]) -> str:
    """SHA-256 over `prev_hash` and the canonical JSON of `fields` — keys sorted, no whitespace, UTF-8 kept."""
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{prev_hash}{canonical}".encode()).hexdigest()


def _sealed_fields(row: AuditEvent) -> dict[str, Any]:
    """Every column of `row` the hash covers — all of them but the two hashes."""
    return {
        "seq": row.seq,
        "occurred_at": row.occurred_at.isoformat(timespec="microseconds"),
        "kind": row.kind,
        "mode": row.mode,
        "agent_id": row.agent_id,
        "version_id": row.version_id,
        "trace_id": row.trace_id,
        "run_id": row.run_id,
        "node_id": row.node_id,
        "details": row.details,
    }


def append_event(db: Session, entry: AuditEntry) -> AuditEvent:
    """Chain `entry` onto the end and commit. Serialised on the chain head; never part of the caller's transaction."""
    head = _locked_head(db)
    row = AuditEvent(
        seq=head.last_seq + 1,
        # Truncated to microseconds and naive UTC, as the column stores it, so
        # the time read back is the time that was hashed.
        occurred_at=datetime.now(UTC).replace(tzinfo=None),
        kind=entry.kind.value,
        mode=entry.mode.value,
        agent_id=entry.agent_id,
        version_id=entry.version_id,
        trace_id=entry.trace_id,
        run_id=entry.run_id,
        node_id=entry.node_id,
        # Round-tripped through JSON so what is hashed is what the column holds.
        details=json.loads(json.dumps(entry.details, default=str)),
        prev_hash=head.last_hash,
        entry_hash="",
    )
    row.entry_hash = entry_hash(row.prev_hash, _sealed_fields(row))
    head.last_seq = row.seq
    head.last_hash = row.entry_hash
    db.add(row)
    db.commit()
    return row


def _locked_head(db: Session) -> AuditChainHead:
    """The chain head, locked for this transaction — created on the very first append."""
    head = db.get(AuditChainHead, _HEAD_ID, with_for_update=True)
    if head is not None:
        return head
    try:
        db.add(AuditChainHead(id=_HEAD_ID, last_seq=0, last_hash=GENESIS_HASH))
        db.flush()
    except IntegrityError:
        # Somebody else created it first; theirs is the head, locked as usual.
        db.rollback()
    head = db.get(AuditChainHead, _HEAD_ID, with_for_update=True, populate_existing=True)
    assert head is not None
    return head


def record_audit_event(entry: AuditEntry) -> None:
    """The platform's `AuditSink`: one append, on a session of its own."""
    db = SessionLocal()
    try:
        append_event(db, entry)
    finally:
        db.close()


def verify_chain(db: Session, *, batch: int = 500) -> ChainVerdict:
    """Walk the chain from the first entry and name the first place it does not hold.

    Four ways it can break, each reported at the `seq` where it shows:
    a `seq` missing (a deleted entry), a `prev_hash` that is not the previous
    entry's hash (an entry removed or reordered), an `entry_hash` that does not
    match the entry's own fields (an edited entry), and a chain that ends
    before its recorded head (the newest entries cut off).
    """
    expected_seq, prev = 1, GENESIS_HASH
    checked = 0
    for row in _rows(db, batch=batch):
        if row.seq != expected_seq:
            return ChainVerdict(False, checked, expected_seq, f"thiếu bản ghi seq {expected_seq}")
        if row.prev_hash != prev:
            return ChainVerdict(False, checked, row.seq, "prev_hash không khớp bản ghi trước — chuỗi bị nối sai")
        if entry_hash(row.prev_hash, _sealed_fields(row)) != row.entry_hash:
            return ChainVerdict(False, checked, row.seq, "nội dung bản ghi không khớp entry_hash — bản ghi đã bị sửa")
        checked += 1
        expected_seq, prev = row.seq + 1, row.entry_hash
    head = db.get(AuditChainHead, _HEAD_ID)
    if head is not None and (head.last_seq != checked or head.last_hash != prev):
        return ChainVerdict(
            False, checked, checked + 1, f"chuỗi dừng ở seq {checked}, đầu chuỗi ghi seq {head.last_seq}"
        )
    return ChainVerdict(True, checked)


def _rows(db: Session, *, batch: int) -> Iterator[AuditEvent]:
    """Every entry in `seq` order, a batch at a time, so a long chain is not loaded at once."""
    after = 0
    while True:
        page = list(db.scalars(select(AuditEvent).where(AuditEvent.seq > after).order_by(AuditEvent.seq).limit(batch)))
        if not page:
            return
        yield from page
        db.expunge_all()
        after = page[-1].seq
