"""The ledger for what the platform spends on itself — `src.services.platform_spend`.

Found the expensive way. An OpenAI project hit its **enforced spend limit**
while this platform's own cost board reported $1.57 in total, because the board
reads `run_costs` and `run_costs` only ever knew about a creator's agent
*running*. Building the agents — reading an interview, drafting a spec, composing
a graph, writing eval cases, judging them — was thirteen call sites through a
two-line `ask()` that counted nothing, on the platform's dearest model, with the
largest prompts in the codebase, retried up to three times per document.

So what is pinned here is not the arithmetic; it is the three properties that
decide whether a ledger is worth having:

- it records **what the provider actually reported**, and stays silent otherwise;
- it **never fails the call** it is measuring;
- it survives the thing it was spent on.
"""

from typing import Any

from src.db.base import SessionLocal
from src.db.models import PlatformCallPurpose, PlatformModelCall
from src.models.model_catalog import CACHED_INPUT_RATE, MODEL_CATALOG, usd_cost
from src.services.platform_spend import record, spend_by_purpose


class _Reply:
    """An `AIMessage` as far as this module is concerned: usage and nothing else."""

    def __init__(self, usage: dict[str, Any] | None) -> None:
        self.usage_metadata = usage


def _usage(inp: int, out: int, cached: int = 0) -> dict[str, Any]:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "input_token_details": {"cache_read": cached},
    }


def _rows() -> list[PlatformModelCall]:
    with SessionLocal() as session:
        return list(session.query(PlatformModelCall).order_by(PlatformModelCall.id).all())


def test_a_call_the_provider_reported_lands_priced(clean_platform_ledger: None) -> None:
    """The whole point, once: tokens in, a row on the bill out."""
    record(
        purpose=PlatformCallPurpose.COMPOSE_GRAPH,
        model="gpt-4.1-mini",
        reply=_Reply(_usage(3000, 700)),
        seconds=8.5,
        attempt=2,
    )

    (row,) = _rows()
    assert row.purpose == PlatformCallPurpose.COMPOSE_GRAPH
    assert (row.input_tokens, row.output_tokens, row.attempt) == (3000, 700, 2)
    assert row.cost_usd == usd_cost("gpt-4.1-mini", input_tokens=3000, output_tokens=700)


def test_a_cached_prefix_is_billed_at_a_quarter_and_not_counted_twice(
    clean_platform_ledger: None,
) -> None:
    """`cached_input_tokens` is a *subset of* `input_tokens`, not an addition.

    Providers report the cached count inside the total. Adding them would bill
    the same tokens twice and make a prompt look more expensive the better its
    caching worked — the exact opposite of the thing the column is for.
    """
    record(
        purpose=PlatformCallPurpose.COMPOSE_GRAPH,
        model="gpt-4.1-mini",
        reply=_Reply(_usage(4000, 0, cached=3000)),
        seconds=1.0,
    )

    (row,) = _rows()
    info = MODEL_CATALOG["gpt-4.1-mini"]
    fresh, discounted = 1000, 3000 * CACHED_INPUT_RATE
    assert row.cached_input_tokens == 3000
    assert row.input_tokens == 4000
    assert row.cost_usd == (fresh + discounted) * info.usd_per_million_input_tokens / 1_000_000


def test_a_reply_that_reports_no_usage_writes_nothing(clean_platform_ledger: None) -> None:
    """Silence rather than a row of zeroes, and it is what keeps the suite honest.

    Every scripted double in this test suite reports no usage, so nothing but a
    real provider call ever reaches this table — which is also why a provider
    that stops reporting usage leaves a *gap* here instead of a made-up number.
    `usd_cost` makes the same choice about an unpriced model by returning `None`:
    a silent zero reads as "this really was free".
    """
    record(purpose=PlatformCallPurpose.JUDGE, model="gpt-4.1-mini", reply=_Reply(None), seconds=0.1)
    record(purpose=PlatformCallPurpose.JUDGE, model="gpt-4.1-mini", reply=_Reply(_usage(0, 0)), seconds=0.1)
    record(purpose=PlatformCallPurpose.JUDGE, model="", reply=_Reply(_usage(10, 10)), seconds=0.1)

    assert _rows() == []


def test_an_unpriced_model_still_files_its_tokens(clean_platform_ledger: None) -> None:
    """A model somebody added to the platform and not to the catalog.

    Refusing the row would lose the fact that the tokens were spent; pretending
    the price is zero would be a lie the totals inherit. The row lands with the
    counts intact, the cost at zero, and a warning in the log naming the model —
    so the gap is findable rather than invisible.
    """
    record(purpose=PlatformCallPurpose.JUDGE, model="model-nobody-priced", reply=_Reply(_usage(50, 20)), seconds=0.2)

    (row,) = _rows()
    assert (row.input_tokens, row.output_tokens) == (50, 20)
    assert row.cost_usd == 0.0


def test_bookkeeping_never_becomes_the_creators_problem(clean_platform_ledger: None) -> None:
    """A model call that worked and a ledger row that did not are two events.

    Only the first one is the creator's. Turning a drafted workflow into an error
    screen because a `platform_model_calls` insert failed would be a worse bug
    than the missing ledger it came from — so `record` swallows everything and
    logs.
    """

    class _Exploding:
        @property
        def usage_metadata(self) -> dict[str, Any]:
            raise RuntimeError("sổ hỏng")

    record(purpose=PlatformCallPurpose.JUDGE, model="gpt-4.1-mini", reply=_Exploding(), seconds=0.1)

    assert _rows() == []


def test_the_bill_groups_by_job_dearest_first(clean_platform_ledger: None) -> None:
    """`purpose` is the group-by the table exists for.

    By job and not by model, because every row runs on one of the same two
    models: "gpt-4.1-mini was 90% of it" is a fact addressed to nobody, while
    "composing graphs is 60% of the build bill" is one somebody can act on.
    """
    for _ in range(3):
        record(
            purpose=PlatformCallPurpose.COMPOSE_GRAPH,
            model="gpt-4.1-mini",
            reply=_Reply(_usage(3000, 700)),
            seconds=8.0,
            attempt=2,
        )
    record(purpose=PlatformCallPurpose.JUDGE, model="gpt-4.1-mini", reply=_Reply(_usage(400, 60)), seconds=1.0)

    compose, judge = spend_by_purpose()
    assert compose["purpose"] == PlatformCallPurpose.COMPOSE_GRAPH
    assert compose["calls"] == 3
    assert compose["attempts_avg"] == 2.0
    assert judge["purpose"] == PlatformCallPurpose.JUDGE
    assert compose["usd"] > judge["usd"]


def test_the_ledger_has_no_foreign_key_to_anything(clean_platform_ledger: None) -> None:
    """ADR-0053's lesson, paid forward — and the reason it is not decoration.

    `agent_runs` survived a mass agent delete because it kept plain values and
    no cascade. A cost ledger that cascaded away with its subject would answer
    "where did the money go" with silence for exactly the agents somebody
    deleted for being expensive. A composition is also not owned by an agent in
    the first place: three failed attempts produce no agent at all.
    """
    assert PlatformModelCall.__table__.foreign_keys == set()
