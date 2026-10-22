"""Ingest spend: written down and added up — see #101 / ADR-0009 §4.

Mirrors the split ADR-0009 §8b draws for run costs: `src.services.extraction`
decides what one document's fresh extraction cost (it is the only place that
actually talked to OpenRouter), and this module decides where that fact lives,
how it is totalled, and when it is loud enough to warn about. Split for the
same reason `src.agents.costs`/`src.agents.spend` are split — a pricing change
and a new question about spending are two different edits.

Priced through `src.models.model_catalog.usd_cost`, the same table
`src.agents.costs` prices a run's model calls from (ADR-0009 §1) — reusing it
is what the ticket's own acceptance line ("dùng lại đúng hàm quy giá đang có,
không viết bảng giá thứ hai") is asking for.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import Document, IngestionCost
from src.models.model_catalog import usd_cost
from src.services.extraction import VlmSpend

logger = logging.getLogger(__name__)


def record_vlm_spend(session: Session, document: Document, spend: VlmSpend) -> IngestionCost | None:
    """Write one `ingestion_costs` row for `spend`, or log and return `None` if `spend.model` is unpriced.

    Called at most once per ingest — `extract_document`'s own `on_vlm_spend`
    contract — and never on a cache hit or a text-native document, which is
    what makes "upload trùng file → không phát sinh dòng mới" true without this
    function having to know anything about caching itself.

    An unpriced model is a configuration gap (`extraction_vlm_model` pointed at
    something `MODEL_CATALOG` has no entry for), not a fault in this Document —
    logged and skipped rather than raised, the same call ADR-0009 §6 makes for
    a broken trace backend: a cost-recording failure must never take an
    ingest that already succeeded down with it.
    """
    usd = usd_cost(spend.model, input_tokens=spend.input_tokens, output_tokens=spend.output_tokens)
    if usd is None:
        logger.warning(
            "Ingest of document %s spent tokens on model %r, which has no MODEL_CATALOG price — cost not recorded",
            document.id,
            spend.model,
        )
        return None
    row = IngestionCost(
        document_id=document.id,
        kb_id=document.kb_id,
        owner_id=document.knowledge_base.owner_id,
        model=spend.model,
        input_tokens=spend.input_tokens,
        output_tokens=spend.output_tokens,
        cost_usd=usd,
        page_count=spend.page_count,
    )
    session.add(row)
    session.commit()
    return row


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def owner_spend_this_month(session: Session, owner_id: int, *, now: datetime | None = None) -> float:
    """`owner_id`'s total ingest spend so far in the current calendar month."""
    since = _month_start(now if now is not None else datetime.now(UTC))
    total = session.execute(
        select(func.sum(IngestionCost.cost_usd)).where(
            IngestionCost.owner_id == owner_id, IngestionCost.created_at >= since
        )
    ).scalar_one()
    return float(total or 0.0)


def check_soft_limit(session: Session, owner_id: int, *, now: datetime | None = None) -> float | None:
    """Warn (and say how far over) if `owner_id` has crossed the configured monthly soft limit.

    Re-checked on every fresh extraction rather than cached anywhere, for
    `src.admin.config`'s own reason: a limit an admin just lowered must apply
    to the very next ingest, not to whichever ingest happens to notice.

    Returns the month-to-date total when it is over the limit (so a caller can
    log or surface the number), `None` when there is no configured limit or
    spend has not crossed it — `None` either way is deliberately not
    distinguishable further, because "no limit configured" and "under the
    limit" both mean the same thing to a caller: nothing to warn about.
    """
    limit = get_settings().ingestion_cost_soft_limit_usd
    if limit is None:
        return None
    total = owner_spend_this_month(session, owner_id, now=now)
    if total <= limit:
        return None
    logger.warning(
        "Owner %s has spent $%.4f on ingestion this month, past the configured soft limit of $%.4f",
        owner_id,
        total,
        limit,
    )
    return total


GroupBy = Literal["day", "month", "owner", "kb"]


@dataclass(frozen=True, slots=True)
class IngestionSpendEntry:
    """One bucket of ingest spend — a day, a month, an owner, or a Knowledge Base."""

    key: str
    document_count: int
    page_count: int
    input_tokens: int
    output_tokens: int
    usd: float


def _bucket(group_by: GroupBy) -> tuple[Any, Callable[[Any], str]]:
    """The column `group_by` aggregates on, and how to read one grouped-by value back as a string key.

    One switch rather than the column and the key-formatter switching
    separately: the two are the same decision (which axis) made twice, and a
    fifth `GroupBy` value added to one without the other is exactly the kind
    of drift a single map closes off.
    """
    if group_by == "day":
        return func.date_trunc("day", IngestionCost.created_at), lambda value: value.date().isoformat()
    if group_by == "month":
        return func.date_trunc("month", IngestionCost.created_at), lambda value: value.strftime("%Y-%m")
    if group_by == "owner":
        return IngestionCost.owner_id, str
    return IngestionCost.kb_id, str


def ingestion_spend(db: Session, *, group_by: GroupBy = "day") -> list[IngestionSpendEntry]:
    """Ingest spend totalled along one axis, most recent / dearest first.

    Aggregated in the database rather than by summing rows in Python — the
    same call `src.agents.spend.agent_spend` makes: the answer is one row per
    bucket however many `ingestion_costs` rows are behind it.

    `document_count` counts distinct documents rather than rows, because a
    Document that was re-ingested (RAG-08's `POST .../reingest`) has more than
    one fresh-extraction row and "how many documents did this cost" is the
    question an admin is actually asking.
    """
    column, key_of = _bucket(group_by)
    statement = (
        select(
            column,
            func.count(func.distinct(IngestionCost.document_id)),
            func.sum(IngestionCost.page_count),
            func.sum(IngestionCost.input_tokens),
            func.sum(IngestionCost.output_tokens),
            func.sum(IngestionCost.cost_usd),
        )
        .group_by(column)
        .order_by(func.sum(IngestionCost.cost_usd).desc())
    )
    return [
        IngestionSpendEntry(
            key=key_of(key),
            document_count=int(documents or 0),
            page_count=int(pages or 0),
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            usd=float(usd or 0.0),
        )
        for key, documents, pages, input_tokens, output_tokens, usd in db.execute(statement).all()
    ]
