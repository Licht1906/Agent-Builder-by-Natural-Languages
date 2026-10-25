"""Which model a *composed* node ends up running on — `composition._assigned_model`.

The composer names a model per node and that name is the single most expensive
decision it makes, because a graph is drawn once and then runs for as long as the
agent exists. Measured over 246 real runs, `gpt-4o` was **52% of the entire
bill** — $0.81 of $1.57 — at 166 calls, because the composer wrote it into 80 of
114 `agent_llm` nodes.

Two changes came out of that, and only the second one held:

1. The prompt was rewritten — models listed cheapest-first, "default to the top
   of the list for every node", "step one down, never to the bottom". It worked
   for cheap nodes and, three end-to-end runs in a row, still wrote `gpt-4o` on
   the one node it judged to need a strong model.
2. So the substitution moved into code. Naming a *tier* is a judgement the
   composer is qualified to make; picking which SKU realises that tier is a price
   list, and a model reaching for the name it has read most often is not reading
   one.

These tests are about the second. What they must not let through is the version
of it that would be worse than the bug: a platform that silently overrides real
judgements about quality.
"""

from src.models.model_catalog import (
    ASSIGNABLE_MODELS,
    DEFAULT_NODE_MODEL,
    MODEL_CATALOG,
    RETIRED_MODELS,
    SEEDED_MODEL_ALLOWLIST,
    SUPERSEDED_BY,
    served_model,
)
from src.workflow.composition import _assigned_model

#: Every assignable model, as a default install offers them.
ALL = ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o", "gpt-4.1"]


def test_a_dominated_model_is_swapped_for_the_one_that_dominates_it() -> None:
    """The measured failure, closed. `gpt-4o` is $2.50/$10.00; `gpt-4.1` is $2.00/$8.00."""
    assert _assigned_model("gpt-4o", ALL) == "gpt-4.1"


def test_everything_that_is_not_dominated_is_left_exactly_as_written() -> None:
    """The composer's judgement survives — this is a price correction, not a review.

    `gpt-4o-mini` is the case that says so out loud. `gpt-4.1-mini` is newer and
    better and this codebase already prefers it (`DEFAULT_NODE_MODEL`), yet it
    costs $0.40/$1.60 against gpt-4o-mini's $0.15/$0.60 — so that pair is a real
    trade and nothing here gets to make it on the composer's behalf.
    """
    for named in ("gpt-4.1", "gpt-4.1-mini", "gpt-4o-mini"):
        assert _assigned_model(named, ALL) == named


def test_a_dominated_model_is_kept_when_its_replacement_is_not_allowed() -> None:
    """An admin's allowlist outranks this rule, and silence is the wrong failure.

    Substituting a model the admin disallowed would write a workflow naming
    something the runtime refuses — a graph that looks wired and cannot run,
    which is the exact failure `default_node_model` was written to avoid.
    """
    assert _assigned_model("gpt-4o", ["gpt-4o", "gpt-4o-mini"]) == "gpt-4o"


def test_an_unlisted_or_missing_model_still_falls_back_to_the_platform_default() -> None:
    """Unchanged behaviour, asserted because the substitution runs beside it now."""
    assert _assigned_model("gpt-5-turbo", ALL) == DEFAULT_NODE_MODEL
    assert _assigned_model("", ALL) == DEFAULT_NODE_MODEL
    assert _assigned_model(None, ALL) == DEFAULT_NODE_MODEL


def test_every_domination_claim_in_the_catalog_is_true_of_its_prices() -> None:
    """The map is a claim about money, so it is checked against the money.

    `SUPERSEDED_BY` says "cheaper on both rates and not worse at the job". The
    second half is a judgement a test cannot check; the first half is arithmetic,
    and an entry that fails it is an entry that would make every composed graph
    quietly dearer while looking like a saving.
    """
    assert SUPERSEDED_BY, "một map rỗng thì không có gì để kiểm"
    for dominated, better in SUPERSEDED_BY.items():
        assert dominated in ASSIGNABLE_MODELS, dominated
        assert better in ASSIGNABLE_MODELS, better
        worse_price, better_price = MODEL_CATALOG[dominated], MODEL_CATALOG[better]
        assert better_price.usd_per_million_input_tokens < worse_price.usd_per_million_input_tokens
        assert better_price.usd_per_million_output_tokens < worse_price.usd_per_million_output_tokens
        # Same tier, or the claim is about capability rather than price and does
        # not belong in a table that overrides the composer without asking.
        assert better_price.tier is worse_price.tier


def test_a_retired_model_is_never_assigned_even_when_the_allowlist_still_names_it() -> None:
    """OpenAI 404s `gpt-4.1-nano` now; an admin allowlist saved before that still lists it."""
    assert _assigned_model("gpt-4.1-nano", ALL) == RETIRED_MODELS["gpt-4.1-nano"]
    assert _assigned_model("gpt-4.1-nano", ["gpt-4.1-nano", "gpt-4o-mini"]) == "gpt-4o-mini"


def test_a_stored_node_naming_a_retired_model_is_served_by_its_replacement() -> None:
    assert served_model("gpt-4.1-nano") == "gpt-4.1-mini"
    assert served_model("gpt-4o-mini") == "gpt-4o-mini"
    assert "gpt-4.1-nano" not in SEEDED_MODEL_ALLOWLIST
