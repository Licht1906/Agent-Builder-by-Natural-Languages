"""Which chat models exist, and everything the platform knows about each one.

Two things hang off this table, and both are assigned **here, in code**:

- how reliably a model drives a tool loop (`tier`), which decides whether a step
  with tools may use it;
- what it costs (`usd_per_million_*`), which is what turns a token count into
  money on the cost figures (#11).

An `AgentSpec` can pick a model; it can never declare either fact about it. The
admin allowlist (ticket #12) can only *narrow* `ASSIGNABLE_MODELS` — it cannot
add a model nobody vetted, and therefore cannot add one nobody priced.

`ASSIGNABLE_MODELS` and the catalog are not the same set, and the difference is
one model: the meta-agent's. See `META_AGENT_MODEL` below.

One table rather than a catalog plus a separate price list: two dicts keyed by
model name drift, and the way they drift is that a model gains an entry in one
and not the other. A missing tier is loud (the spec is rejected); a missing price
is silent, and it prices an agent at zero — a wrong number on a screen whose
whole purpose is to be trusted. Making them one record means a model that exists
has a price by construction.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class ModelTier(StrEnum):
    """How reliably a model drives a tool loop.

    `CHEAP` models are fine for extraction and drafting, but unreliable at tool
    calling — which is why a cheap model may not be assigned to a step that has
    tools (see `AgentSpec`).
    """

    CHEAP = "cheap"
    STRONG = "strong"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One vetted model: how far it is trusted, and what it bills.

    Prices are per million tokens in USD, quoted separately for input and output
    because every provider charges them differently — usually four to five times
    more for output. A single blended rate would misprice exactly the workload
    this platform runs, where a ReAct loop replays a growing prompt (all input)
    to produce a short answer (little output).
    """

    tier: ModelTier
    usd_per_million_input_tokens: float
    usd_per_million_output_tokens: float
    max_tokens_per_call: int
    """The most tokens one call to this model can possibly bill.

    Its context window plus its largest answer — the provider's own bound, not
    one we enforce. It exists so the worst-case figure (#11) can be derived from
    the shape of a spec rather than guessed at, and it is deliberately generous:
    a worst case that flattered the agent would be worthless as a guarantee.
    """


#: Prices as published by OpenAI, USD per million tokens. They change; when they
#: do, this table is the one place to change, and every figure derived from it
#: moves at once.
MODEL_CATALOG: dict[str, ModelInfo] = {
    "gpt-4o-mini": ModelInfo(ModelTier.CHEAP, 0.15, 0.60, max_tokens_per_call=144_384),
    "gpt-4.1-mini": ModelInfo(ModelTier.CHEAP, 0.40, 1.60, max_tokens_per_call=1_080_344),
    "gpt-4.1-nano": ModelInfo(ModelTier.CHEAP, 0.10, 0.40, max_tokens_per_call=1_080_344),
    "gpt-4o": ModelInfo(ModelTier.STRONG, 2.50, 10.00, max_tokens_per_call=144_384),
    "gpt-4.1": ModelInfo(ModelTier.STRONG, 2.00, 8.00, max_tokens_per_call=1_080_344),
    # The meta-agent's, and nobody else's — see `META_AGENT_MODEL`. A pinned
    # snapshot rather than the floating `gpt-4.1` alias: the meta-agent is the
    # hardest part of the system, and a spec generator whose model changes under
    # it is a regression nobody can date.
    "gpt-4.1-2025-04-14": ModelInfo(ModelTier.STRONG, 2.00, 8.00, max_tokens_per_call=1_080_344),
    # The meta-agent's, as of the switch to the GPT-5.6 family. Reserved the same
    # way, and **not** a strong-tier model: OpenAI positions Luna as the
    # cost-optimised member of the family ("roughly corresponds to the nano model
    # tier used in earlier GPT-5 families"), below Terra and Sol. The tier here
    # records that honestly rather than flattering it to satisfy
    # `src.services.llm.meta_agent_model`, which now warns instead of refusing.
    # Context 1,050,000 + 128,000 max output = the worst-case bound below.
    "gpt-5.6-luna": ModelInfo(ModelTier.CHEAP, 0.20, 1.20, max_tokens_per_call=1_178_000),
    # The meta-agent's and the document writer's since 2026-10-25 — see
    # `META_AGENT_MODEL`. Same family position as 5.6 Luna (the cost-optimised
    # one, below Sol), hence CHEAP. Prices from OpenAI's pricing page (standard,
    # ≤272K input). The page states no context window; the worst-case bound is
    # 5.6 Luna's until OpenAI publishes one — an assumption, not a measurement.
    "gpt-6-luna": ModelInfo(ModelTier.CHEAP, 0.10, 0.50, max_tokens_per_call=1_178_000),
    # The document writer's, and nobody else's — see `DOCUMENT_MODEL`. Pinned for
    # `META_AGENT_MODEL`'s reason, and *separate from the assignable
    # `gpt-4.1-mini` above even though it is the same model*: what the platform
    # reserves is not the weights, it is the call. A reserved id is how
    # `src.services.llm` can tell a document the platform is writing (temperature
    # 0, always) from a creator's own node that happens to run the same model
    # (their temperature, warmth being the point there) — the factory receives a
    # bare id and has nothing else to tell them apart by.
    "gpt-4.1-mini-2025-04-14": ModelInfo(ModelTier.CHEAP, 0.40, 1.60, max_tokens_per_call=1_080_344),
    # The extraction VLM's (RAG-06 / #92, priced for RAG-15 / #101), and nobody
    # else's — see `EXTRACTION_VLM_MODEL` below. Prices as published by
    # OpenRouter: $0.104 / $0.416 per million input/output tokens, 131K context.
    # A tier is required by `ModelInfo` but never consulted for this entry — the
    # extraction pipeline never drives a tool loop.
    "qwen/qwen3-vl-32b-instruct": ModelInfo(ModelTier.CHEAP, 0.104, 0.416, max_tokens_per_call=131_072),
    # The agent con's (§6.3), and nobody else's — see `AGENT_MODEL` below. Prices
    # as published by OpenRouter's own `/api/v1/models` for this slug: $0.10 /
    # $0.20 per million input/output tokens, 32,768-token context with a
    # 32,768-token `max_completion_tokens`, hence 65,536 worst case.
    #
    # `CHEAP`, which is the honest tier for a 7B model, and which the compiler
    # binds tools to anyway — the two are not in conflict because the tier rule
    # ("a cheap model may not be assigned to a step that has tools") governs what
    # a *creator* may put on an `AgentSpec` step, and this model is not
    # assignable. §6.3 picked it deliberately for UC-1 ("Cần structured output
    # tốt"), and OpenRouter reports `tools`/`tool_choice`/`structured_outputs`
    # among its supported parameters, so the tool loop is served, not faked.
    "qwen/qwen-2.5-7b-instruct": ModelInfo(ModelTier.CHEAP, 0.10, 0.20, max_tokens_per_call=65_536),
}

#: What the meta-agent runs on. Fixed by the team, never configurable (#12).
#:
#: This is the model that writes the platform's own documents: it drafts the
#: solution spec, picks the template, composes and fills the workflow graph,
#: refines it from a sentence, and writes and judges the eval cases. "The model
#: that creates the workflow" is this one, not `DEFAULT_NODE_MODEL` below — that
#: is what the *drafted steps* run on afterwards.
#:
#: In `MODEL_CATALOG`, because it is a model this platform calls and one table is
#: what stops a model existing half-described. Out of `ASSIGNABLE_MODELS`,
#: because that is the set every admin configuration surface is built from —
#: which makes "an admin cannot reach the meta-agent's model" a property of the
#: data rather than a rule someone has to remember. The meta-agent is the hardest
#: part of the system; a user's configuration does not get a vote in it.
#:
#: **`gpt-6-luna`** — CHEAP tier, $0.10/$0.50 per million tokens. Switched from
#: `gpt-4.1` (2026-10-25) at the team's call. Being CHEAP, it makes `src.services.llm.meta_agent_model` log its "not strong tier"
#: warning on every call — expected, and the line to look for if spec/graph
#: quality drops. Tools are bound with reasoning off (`_ReasoningOffWhenToolsAreBound`).
#: The constant stays reserved and out of `ASSIGNABLE_MODELS` so no admin
#: surface gets a vote in it.
META_AGENT_MODEL = "gpt-6-luna"

#: What *writes the platform's own documents* — see `src.services.llm.DOCUMENT_MODEL`
#: for the measurement and the argument. Reserved exactly like the meta-agent's:
#: in the catalog because the platform calls it, out of `ASSIGNABLE_MODELS`
#: because no admin surface gets a vote in it.
#:
#: The split it makes is between two jobs, not two qualities. The meta-agent's
#: model above answers *"which model may decide to call a tool"* — there, the
#: model is the only thing standing between a bad decision and a real action.
#: This one answers *"which model may write a JSON document"* — there,
#: `src.workflow.validation` and `AgentSpec` stand in the way, and
#: `write_document` re-asks with the complaint up to three times. A cheap model
#: fails that gate more often and passes it just as completely.
#:
#: The same id as `META_AGENT_MODEL` since 2026-10-25 (both moved to
#: `gpt-6-luna`). Luna is CHEAP, so `document_model`'s tier check still holds.
#: `META_AGENT_TEMPERATURE` now applies only where reasoning is off (tool
#: calls): a reasoning model takes no temperature but the default.
DOCUMENT_MODEL = "gpt-6-luna"

#: The eval judge (ADR-0054 §4), **pinned to a dated snapshot** — EV-04 / ADR-0069 §9.
#: A run's verdicts are only comparable with another's under the same judge
#: (`src.pm.reports` refuses to diff across `judge_model`), and an alias such as
#: `gpt-4.1-mini` is moved by the provider without anybody here changing a line:
#: every baseline would silently stop meaning what it meant. A different family
#: from the meta-agent that writes the cases ("judge phải khác họ model với
#: generator", s52). `gpt-6-luna` has no dated snapshot published yet, so it
#: cannot be the judge until it has one.
JUDGE_MODEL = "gpt-4.1-mini-2025-04-14"

#: The aliases an operator may still write in `JUDGE_MODEL=`, each resolved to
#: the snapshot it pins — so `.env` files written before EV-04 keep working and
#: keep meaning one model.
JUDGE_SNAPSHOTS: Mapping[str, str] = {"gpt-4.1-mini": "gpt-4.1-mini-2025-04-14"}

_DATED = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def pinned_judge_model(model_id: str) -> str:
    """`model_id` as a dated snapshot — an alias resolved through `JUDGE_SNAPSHOTS`, anything else refused."""
    model_id = model_id.strip()
    pinned = JUDGE_SNAPSHOTS.get(model_id, model_id)
    if not _DATED.search(pinned):
        raise ValueError(
            f"JUDGE_MODEL={model_id!r} không phải snapshot có ngày (vd. gpt-4.1-mini-2025-04-14) — "
            "alias trôi theo nhà cung cấp làm mọi baseline mất nghĩa (ADR-0069 §9)"
        )
    return pinned


#: What `src.services.extraction` calls for a scanned/photographed page (RAG-06).
#: Same reasoning as `META_AGENT_MODEL`: priced here so ingest cost (#101) reuses
#: this one table rather than a second price list, and excluded from
#: `ASSIGNABLE_MODELS` because it is not a model an agent step may be assigned —
#: nothing here runs a tool loop.
EXTRACTION_VLM_MODEL = "qwen/qwen3-vl-32b-instruct"

#: What a signed-off agent con runs on — §6.3: "Agent con | Qwen2.5-7B-Instruct
#: AWQ | Dùng chung endpoint, khác system prompt." A constant, not a setting, for
#: `META_AGENT_MODEL`'s reason (ADR-0010 §1): which model the signed-off agent con
#: runs on is not something a request chooses.
#:
#: Spelled as **OpenRouter's** slug, not the Hugging Face repo id, and the two
#: differ in a way that is easy to get wrong: HF publishes the weights as
#: `Qwen/Qwen2.5-7B-Instruct`, OpenRouter routes them as
#: `qwen/qwen-2.5-7b-instruct` (lowercase, and a dash before the 2.5). This
#: string goes on the wire in a request body, so it has to be the one the
#: provider actually resolves — the HF id gets a 404 from OpenRouter's router.
#: The AWQ half of §6.3's name is not part of the id either: quantisation is
#: something whichever provider OpenRouter routes to decides, not something a
#: caller of a hosted API names.
#:
#: Lives here rather than in `src.compiler.graph` (which still re-exports it, so
#: every existing importer is unaffected) because `src.services.llm` has to know
#: it in order to route it — see `OPENROUTER_MODELS`. A leaf service reaching up
#: into the langgraph compiler for one string is the dependency this avoids.
AGENT_MODEL = "qwen/qwen-2.5-7b-instruct"

#: Which models are reached through OpenRouter rather than OpenAI directly, and
#: therefore which ones `src.services.llm` points at `openrouter_base_url` with
#: `openrouter_api_key` instead of `openai_api_key`.
#:
#: The provider is a property of the model, not of the call site, which is why
#: this is one set here rather than a flag each caller passes: `_build_chat_model`
#: receives a bare model id and nothing else, and every caller that had to
#: remember "this one goes somewhere else" would eventually forget.
#:
#: `EXTRACTION_VLM_MODEL` is in the set for completeness — it is an OpenRouter
#: model and always was — even though `src.services.extraction` does not go
#: through `_build_chat_model` at all (it POSTs `openrouter_url` with raw httpx,
#: because a vision extraction is a one-shot JSON call, not a chat model with
#: tools bound). Listing it keeps this set the answer to "who serves this model?"
#: rather than the narrower "which models does the chat factory reroute?", so a
#: future caller that *does* want it as a `BaseChatModel` gets the right endpoint.
OPENROUTER_MODELS: frozenset[str] = frozenset({AGENT_MODEL, EXTRACTION_VLM_MODEL})

#: The models a user's agent may be assigned — the catalog, less the three
#: reserved above. The admin allowlist is a subset of *this*, never of the catalog.
ASSIGNABLE_MODELS: dict[str, ModelInfo] = {
    name: info
    for name, info in MODEL_CATALOG.items()
    if name not in (META_AGENT_MODEL, DOCUMENT_MODEL, EXTRACTION_VLM_MODEL, AGENT_MODEL)
}

#: Models that another entry in this catalog **strictly dominates** — cheaper on
#: both rates *and* not worse at the job — mapped to the one that dominates them.
#:
#: One entry, and it is a fact rather than a preference: `gpt-4o` is $2.50/$10.00
#: where `gpt-4.1` is $2.00/$8.00, and gpt-4.1 is the newer model and the better
#: instruction-follower of the two. There is no task for which picking gpt-4o is
#: the right call, which is exactly what makes it dangerous — it is not a choice
#: anybody makes, it is a name that gets typed. Measured over 246 real runs it
#: was **52% of the whole bill**.
#:
#: `gpt-4o-mini` is deliberately **not** here even though `gpt-4.1-mini` is newer
#: and better, because that pair is a real trade: mini costs $0.40/$1.60 against
#: gpt-4o-mini's $0.15/$0.60. Domination means *nothing is given up*. A map that
#: also held honest trades would be a map that silently overrides judgement,
#: which is the opposite of what it is for — see `DEFAULT_NODE_MODEL`, where this
#: codebase already picked the dearer of two cheap models on purpose.
SUPERSEDED_BY: dict[str, str] = {"gpt-4o": "gpt-4.1"}

#: Models the provider **no longer serves**, mapped to the one that runs in their
#: place. Stronger than `SUPERSEDED_BY`: there, the old name is a worse choice;
#: here, it is no choice at all — OpenAI answers `gpt-4.1-nano` with a 404
#: `model_not_found` ("has been deprecated"), measured 2026-10-25.
#:
#: Kept in `MODEL_CATALOG` anyway, for `DEFAULT_MODEL_ALLOWLIST`'s reason: a
#: stored spec naming it must stay readable. What changes is that nothing new
#: is assigned it (`SEEDED_MODEL_ALLOWLIST`, `undominated`) and that a stored
#: node still naming it runs on the replacement (`served_model`) instead of
#: failing the whole run. `gpt-4.1-mini` because it is `DEFAULT_NODE_MODEL`:
#: same family, same tier, and already what an undecided node gets.
RETIRED_MODELS: dict[str, str] = {"gpt-4.1-nano": "gpt-4.1-mini"}


def served_model(model: str) -> str:
    """The model the provider is actually asked for when a caller names `model`."""
    return RETIRED_MODELS.get(model, model)


#: The allowlist that applies when **nobody supplied one** — and it is the
#: *reading* default, not the platform's policy. Keep it permissive.
#:
#: `_model_allowlist_from` falls back to this whenever `AgentSpec.model_validate`
#: is called without a context, and every read-back of a stored spec is such a
#: call: `src.agentspec.store`, the version diff, the approval dossier, the
#: approval queue, `GET /agents/{id}/versions/{v}/spec`. That is deliberate and
#: `src.admin.config.spec_validation_context` states the promise out loud —
#: *"Already-saved specs are validated without it, so tightening the allowlist
#: never takes a running agent down"* (ADR-0003 §7, ADR-0010 §4).
#:
#: So narrowing **this** tuple does not tighten a policy, it **retroactively
#: invalidates history**: a stored spec naming a model taken off the list stops
#: being readable, and the agent list, the dossier and the diff all 500. That was
#: measured the hard way — dropping `gpt-4o` from here turned 98 tests red, and
#: not one of them was about creating anything; they were reads of specs written
#: when gpt-4o was allowed, including a migration that seeds a shipped example.
#:
#: The list that *is* the policy is `SEEDED_MODEL_ALLOWLIST` below, and after it
#: an admin's own `system_config.model_allowlist`. Narrow those.
DEFAULT_MODEL_ALLOWLIST: tuple[str, ...] = tuple(ASSIGNABLE_MODELS)

#: What a fresh `system_config` row starts its **write policy** at — the models a
#: creator on a new install may put on a new agent. Narrower than
#: `DEFAULT_MODEL_ALLOWLIST` on purpose, and this is the list to narrow.
#:
#: `gpt-4o` is left out because it is strictly dominated: `gpt-4.1` is the newer
#: model, the better instruction-follower, and costs **$2.00/$8.00 against
#: gpt-4o's $2.50/$10.00**. A dominated entry on a menu is not a choice, it is a
#: way to spend more by accident — and it was exactly that. Measured over 246
#: real runs, gpt-4o was **52% of the whole bill**: $0.81 of $1.57, at 166 calls,
#: because the composer wrote it into 80 of 114 `agent_llm` nodes.
#:
#: `SUPERSEDED_BY` already substitutes it away at composition time, so this is
#: the second of two belts and it is the one that covers the *canvas*: a creator
#: picking a model by hand from the dropdown never sees an entry that costs 25%
#: more for nothing. Adding it back is one save on the platform-settings screen,
#: which is what makes it a decision somebody makes rather than a default nobody
#: chose.
SEEDED_MODEL_ALLOWLIST: tuple[str, ...] = tuple(
    name for name in DEFAULT_MODEL_ALLOWLIST if name not in SUPERSEDED_BY and name not in RETIRED_MODELS
)


#: What a node the platform drafted runs on until somebody chooses otherwise.
#:
#: Cheap tier, for the reason a draft is a draft: it exists to be read and
#: edited, and starting every generated workflow on the platform's most expensive
#: model would bill a creator for the Generator's convenience.
#:
#: **The strongest of the cheap ones, though, and named rather than derived.**
#: This used to be "the first cheap entry in `ASSIGNABLE_MODELS`", which is a
#: fact about dict ordering and not a judgement about anything — it happened to
#: mean `gpt-4o-mini` on every workflow the platform has ever produced. A creator
#: who runs their draft and finds the answers thin does not conclude that the
#: default model is provisional; they conclude the platform is bad at this. The
#: difference in price between the cheap entries is small next to that.
DEFAULT_NODE_MODEL = "gpt-4.1-mini"


def undominated(model: str, allowlist: Sequence[str]) -> str:
    """`model`, or the entry that strictly dominates it when that one is allowed too.

    The seam exists because *persuasion did not work*. The composer prompt was
    rewritten to order the models cheapest-first and to say "step one down, not
    to the bottom"; measured end to end three times running, it obeyed for every
    cheap node and still wrote `gpt-4o` on the one node it judged to need a
    strong model. Which is not surprising — a model picking SKUs by name is
    reaching for the name it has read most often, not reading a price list.
    Naming the *tier* is a judgement the composer is qualified to make; picking
    which concrete SKU realises that tier is the platform's job, and this is the
    platform doing it.
    """
    retired = RETIRED_MODELS.get(model)
    if retired is not None:
        # Not conditional on the allowlist, unlike a dominated name: a retired
        # model left in place is a run that 404s, which no allowlist asked for.
        return retired if retired in allowlist else default_node_model(allowlist)
    better = SUPERSEDED_BY.get(model)
    return better if better is not None and better in allowlist and better in ASSIGNABLE_MODELS else model


def default_node_model(allowlist: Sequence[str]) -> str:
    """The model a drafted node gets, narrowed to what this platform's admin allows.

    Three steps down, and each one is a weaker claim than the last:
    `DEFAULT_NODE_MODEL` if it is allowed, else the cheapest-tier model that is,
    else simply the first allowed model. An admin who disallowed the default did
    not thereby ask for a workflow naming a model their own runtime will refuse —
    which is what a hard-coded default produced before this function existed.

    An empty allowlist returns `""`, the blank every other unfilled config field
    already is: the canvas rings it and confirmation refuses it, which is the
    honest outcome when the platform has no model to offer. Raising here would
    turn an admin's configuration into a generation the creator cannot even see.
    """
    if DEFAULT_NODE_MODEL in allowlist:
        return DEFAULT_NODE_MODEL
    allowed = [name for name in allowlist if name in ASSIGNABLE_MODELS and name not in RETIRED_MODELS]
    cheap = [name for name in allowed if ASSIGNABLE_MODELS[name].tier is ModelTier.CHEAP]
    return next(iter(cheap), next(iter(allowed), ""))


def is_openrouter_model(model: str) -> bool:
    """Whether `model` is served by OpenRouter rather than by OpenAI directly.

    The one predicate `src.services.llm._build_chat_model` routes on. A function
    rather than an inline `in` check so the rule has a name at the call site: the
    question being asked there is "whose endpoint answers for this, and whose key
    does it take?", which is not obviously the same question as "is this string in
    that set?"
    """
    return model in OPENROUTER_MODELS


def info_of(model: str) -> ModelInfo | None:
    """Everything known about `model`, or `None` if it is not in the catalog at all."""
    return MODEL_CATALOG.get(model)


def tier_of(model: str) -> ModelTier | None:
    """The tier of `model`, or `None` if it is not in the catalog at all."""
    info = MODEL_CATALOG.get(model)
    return info.tier if info is not None else None


#: What a provider charges for an input token it had already seen — a quarter of
#: the fresh rate, as OpenAI prices prompt caching across the 4.1 family.
#:
#: A fraction of the catalog rate rather than a third column, because that is how
#: it is actually billed: caching is a discount on the input price, not a
#: separate price. A model that does not cache simply reports zero cached tokens
#: and this number never comes up.
CACHED_INPUT_RATE = 0.25


def usd_cost(model: str, *, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float | None:
    """Price `input_tokens`/`output_tokens` at `model`'s catalog rate, or `None` if `model` is unpriced.

    The one function every cost figure in the system — run spend (#11 /
    ADR-0009), ingest spend (#101) and the platform's own calls
    (`src.services.platform_spend`) alike — turns tokens into money through, so
    this table stays the single place a price change or a units bug can happen.
    `None` rather than `0.0` for an unpriced model, for ADR-0009's own reason: a
    silent zero reads as "this really was free."

    `cached_input_tokens` is a **subset of** `input_tokens`, not an addition to
    it: providers report the cached count inside the total, and adding them would
    bill the same tokens twice. Zero by default, so every existing caller prices
    exactly as it did before.
    """
    info = info_of(model)
    if info is None:
        return None
    fresh_input = max(0, input_tokens - cached_input_tokens)
    return (
        fresh_input * info.usd_per_million_input_tokens
        + cached_input_tokens * info.usd_per_million_input_tokens * CACHED_INPUT_RATE
        + output_tokens * info.usd_per_million_output_tokens
    ) / 1_000_000
