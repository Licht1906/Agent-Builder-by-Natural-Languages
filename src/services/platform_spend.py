"""The ledger for model calls the platform makes on its own behalf.

`src.agents.spend` does this for a creator's agent: one `run_costs` row per run,
which the PM cost board reads. This module is the same idea for the other half of
the bill — the calls nobody was counting.

**Why it is a wrapper and not a line in each caller.** There were thirteen call
sites and they had one thing in common: they all ended at
`BaseChatModel.ainvoke`. Thirteen copies of "read `usage_metadata`, price it,
write a row" is thirteen chances to forget one, and the one that gets forgotten
is always the expensive one. So the meter goes around the model, and the callers
say only *what job this is* — which is the one fact the wrapper genuinely cannot
work out for itself.

**What it records is what the provider reported, and nothing else.** No usage
metadata means no row: a scripted fake in a test reports none, so the whole suite
runs without touching this table, and a provider that stops reporting usage
produces a gap rather than a fabricated number. That is the same choice
`model_catalog.usd_cost` makes by returning `None` for an unpriced model — a
silent zero reads as "this really was free".

**It never fails a call.** A model call that succeeded and a ledger row that
could not be written are two different events, and only the first one is the
creator's. A bookkeeping error that turned a drafted workflow into an error
screen would be a worse bug than the missing ledger it came from.
"""

import logging
import time
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from sqlalchemy import func, select

from src.db.base import SessionLocal
from src.db.models import PlatformCallPurpose, PlatformModelCall
from src.models.model_catalog import usd_cost

_log = logging.getLogger(__name__)


def metered(model: BaseChatModel, purpose: PlatformCallPurpose, *, attempt: int = 1) -> BaseChatModel:
    """`model`, with what each call costs filed under `purpose`."""
    return _MeteredChatModel(model, purpose, attempt)


class _MeteredChatModel:
    """A chat model that writes down what its answers cost.

    A proxy rather than a subclass: `ChatOpenAI` is a pydantic model and will not
    take a new field, and a subclass would have to know which of the two
    providers it was wrapping. Everything it does not implement is delegated
    untouched, so it stays a `BaseChatModel` to every caller.
    """

    def __init__(self, inner: BaseChatModel, purpose: PlatformCallPurpose, attempt: int) -> None:
        self._inner = inner
        self._purpose = purpose
        self._attempt = attempt

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def bind_tools(self, *args: Any, **kwargs: Any) -> "_MeteredChatModel":
        """Binding tools returns a *new* model, so the meter has to come along.

        Without this the meter is lost at exactly the call site that needs it
        most: `src.metaagent.loop.run_turn` binds its tools first and then runs
        an agent loop of up to a dozen round trips, every one of them on the
        platform's strongest model.
        """
        return _MeteredChatModel(self._inner.bind_tools(*args, **kwargs), self._purpose, self._attempt)

    async def ainvoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        reply = await self._inner.ainvoke(messages, *args, **kwargs)
        record(
            purpose=self._purpose,
            model=self._model_id(),
            reply=reply,
            seconds=time.perf_counter() - started,
            attempt=self._attempt,
        )
        return reply

    def _model_id(self) -> str:
        """Which model actually answered, asked of the object rather than assumed.

        `model_name` is `ChatOpenAI`'s own field and both providers arrive as one
        (OpenRouter speaks the same wire protocol). A fake in a test has neither
        that attribute nor usage metadata, so it never reaches pricing anyway.
        """
        return str(getattr(self._inner, "model_name", "") or getattr(self._inner, "model", "") or "")


def record(*, purpose: PlatformCallPurpose, model: str, reply: Any, seconds: float, attempt: int = 1) -> None:
    """File one call, if the provider said what it cost. Never raises.

    `reply` rather than three token counts, because the shape LangChain reports
    usage in is a detail this module should own alone — a caller that had to know
    about `usage_metadata` would be a caller that gets it wrong when it changes.
    """
    try:
        usage = getattr(reply, "usage_metadata", None) or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        if not model or (input_tokens == 0 and output_tokens == 0):
            # Nothing was reported — a scripted double, or a provider that does
            # not say. A row of zeroes would read as "this call was free".
            return
        cached = int((usage.get("input_token_details") or {}).get("cache_read") or 0)
        cost = usd_cost(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
        )
        with SessionLocal() as session:
            session.add(
                PlatformModelCall(
                    purpose=str(purpose),
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_input_tokens=cached,
                    # An unpriced model is a model somebody added to the platform
                    # and not to the catalog. Zero is wrong and so is refusing —
                    # the tokens were really spent — so the row lands with the
                    # count intact and the warning says which model to price.
                    cost_usd=cost if cost is not None else 0.0,
                    latency_seconds=seconds,
                    attempt=attempt,
                )
            )
            session.commit()
        if cost is None:
            _log.warning("Model %r chưa có giá trong MODEL_CATALOG — ghi sổ với cost 0", model)
    except Exception:  # noqa: BLE001 — see the module docstring: never fail a call
        _log.warning("Không ghi được sổ chi phí cho lời gọi %s", purpose, exc_info=True)


def spend_by_purpose(*, since_days: int | None = None) -> Sequence[dict[str, Any]]:
    """What the platform spent on itself, grouped by job, dearest first.

    A function here rather than a query in the route, because "the meta bill" is
    a fact about this ledger and two callers deriving it separately is how two
    screens start disagreeing about the same money.
    """
    with SessionLocal() as session:
        query = select(
            PlatformModelCall.purpose,
            func.count(PlatformModelCall.id).label("calls"),
            func.coalesce(func.sum(PlatformModelCall.cost_usd), 0.0).label("usd"),
            func.coalesce(func.sum(PlatformModelCall.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(PlatformModelCall.cached_input_tokens), 0).label("cached_input_tokens"),
            func.coalesce(func.sum(PlatformModelCall.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.avg(PlatformModelCall.attempt), 1.0).label("attempts_avg"),
        ).group_by(PlatformModelCall.purpose)
        if since_days is not None:
            query = query.where(
                PlatformModelCall.created_at >= func.now() - func.make_interval(0, 0, 0, since_days)
            )
        rows = session.execute(query.order_by(func.sum(PlatformModelCall.cost_usd).desc())).all()
    return [
        {
            "purpose": row.purpose,
            "calls": int(row.calls),
            "usd": float(row.usd),
            "input_tokens": int(row.input_tokens),
            "cached_input_tokens": int(row.cached_input_tokens),
            "output_tokens": int(row.output_tokens),
            "attempts_avg": float(row.attempts_avg),
        }
        for row in rows
    ]
