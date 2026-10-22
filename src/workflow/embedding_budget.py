"""How many embedding tokens this deployment has spent, and when to stop — ADR-0045 §8.

No embedding provider will tell you how much you have spent with it: Voyage
grants 200M free tokens per model and publishes no endpoint to ask how much is
left, and OpenAI simply bills. So the only honest answer to "how much is this
costing" is one this platform keeps itself: every call reports
`usage.total_tokens`, and every one of those numbers is written down here.

`src.agents.budget` makes the same argument for a running agent and it applies
unchanged: *cumulative is the whole point*. No single embedding call ever looks
expensive — a handbook is a few thousand tokens, a question is twenty — so
nothing but a total can tell you the allowance is nearly gone.

**Written in a session of its own.** A ledger entry records money that has
already been spent, so it must not be able to roll back with the caller's work:
the tokens were consumed by the provider whether or not the chunks they produced
were successfully stored. That is also why the write cannot join the caller's
transaction — committing there would commit whatever else that session had
pending, which is not this module's decision to make.

**What happens at the ceiling is a choice, and it is expressed as a list.**
`embedding_model` is tried in order and the first model still usable is taken,
so:

- one model in the list  → search stops using embeddings when it is exhausted
  and ingest refuses rather than writing a Document with no vectors;
- two or more           → it moves to the next model instead, and only falls
  back when every one of them is spent.

Falling back rather than failing is deliberate. An exhausted allowance is a
budget working, not an outage, and a `KnowledgeSearch` node that started
raising would take down agents that were answering fine a minute earlier. It is
not silent either — see `usage_report`, the admin endpoint that reads it, and
the warning logged the first time a model is retired.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.base import SessionLocal, get_db
from src.db.models import EmbeddingUsage
from src.workflow.embeddings import Embedder, embedder_for

logger = logging.getLogger(__name__)

#: What Voyage grants each embedding model per account, as published on their
#: pricing page for the `voyage-4` family. Kept as a named number for whoever
#: sets `EMBEDDING_TOKEN_ALLOWANCE` back on — it is not a fact any API can be
#: asked for, and OpenAI has no equivalent (it bills instead of granting).
FREE_TOKEN_ALLOWANCE = 200_000_000


@dataclass(frozen=True, slots=True)
class ModelSpend:
    """What one embedding model has cost so far, against what it was given."""

    model: str
    tokens: int
    allowance: int

    @property
    def exhausted(self) -> bool:
        """Whether this model may still be used. No allowance means no ceiling."""
        return bool(self.allowance) and self.tokens >= self.allowance

    @property
    def remaining(self) -> int:
        return max(0, self.allowance - self.tokens) if self.allowance else 0

    @property
    def used_fraction(self) -> float:
        """0.0–1.0 for a model with a ceiling, 0.0 for one without."""
        return min(1.0, self.tokens / self.allowance) if self.allowance else 0.0


def record(model: str, tokens: int) -> None:
    """Write down that `model` spent `tokens`, in a transaction of its own.

    Called after each successful batch rather than once at the end of an
    indexing run: a run that fails halfway has still spent what it spent, and a
    ledger that only counts completed work under-reports exactly when usage is
    highest.
    """
    if tokens <= 0:
        return
    with SessionLocal() as session:
        session.add(EmbeddingUsage(model=model, tokens=tokens))
        session.commit()


def spent(db: Session, model: str) -> int:
    """Total tokens `model` has spent, ever.

    Ever, and not per calendar month, because Voyage's grant is a one-off rather
    than a monthly reset and OpenAI's bill is cumulative anyway. With
    `EMBEDDING_TOKEN_ALLOWANCE=0` — the default, now that the default provider is
    a paid one — this becomes a figure to read rather than a ceiling to hit.
    """
    total = db.scalar(select(func.sum(EmbeddingUsage.tokens)).where(EmbeddingUsage.model == model))
    return int(total or 0)


def usage_report(db: Session, models: Sequence[str], *, allowance: int) -> list[ModelSpend]:
    """What every configured model has spent, in the order they are tried.

    The order matters to whoever is reading it: the first entry that is not
    exhausted is the model actually in use, and everything before it has been
    retired. That is the whole state of the feature in one list.
    """
    return [ModelSpend(model=model, tokens=spent(db, model), allowance=allowance) for model in models]


def usable_model(db: Session, models: Sequence[str], *, allowance: int) -> str | None:
    """The first configured model with allowance left, or `None` if there is none.

    `None` is a supported state and not an error: `get_embedder` returns nothing,
    `get_knowledge_store` builds the lexical store, and search goes on working
    with keyword ranking alone.
    """
    for spend in usage_report(db, models, allowance=allowance):
        if not spend.exhausted:
            return spend.model
        # WARNING and not INFO: a model being retired changes what the platform
        # is, and it happens once. Whoever reads the log after noticing that
        # recall dropped should find this line rather than have to infer it.
        logger.warning(
            "Embedding model %s đã chạm trần %s token — chuyển sang model kế tiếp nếu có",
            spend.model,
            spend.allowance,
        )
    return None


def get_embedder(db: Annotated[Session, Depends(get_db)]) -> Embedder | None:
    """FastAPI dependency: the embedder to use right now, or `None`.

    Three ways to get `None`, and all three are supported states rather than
    errors: no key for the configured provider, an empty model list, or every
    configured model out of allowance. Each of them makes `get_knowledge_store`
    build the lexical store, so search keeps working with keyword ranking alone.

    `None` travels rather than being replaced by a null object returning zero
    vectors — a zero vector ranks every passage equally, which looks like a
    working semantic search returning unhelpful results instead of like one
    that was never switched on.

    The ledger is consulted per request rather than cached, because the whole
    point is to notice the ceiling *between* two questions. It is one indexed
    `SUM` over a table with one row per embedding call.
    """
    settings = get_settings()
    for model in settings.embedding_models:
        # A configured model without its provider key is not usable, but it
        # must not block a later provider in the fallback list.
        embedder = embedder_for(
            model,
            openai_api_key=settings.openai_api_key,
            voyage_api_key=settings.voyage_api_key,
            record_usage=record,
        )
        if embedder is None:
            continue
        if usable_model(db, [model], allowance=settings.embedding_token_allowance) is None:
            continue
        return embedder
    return None
