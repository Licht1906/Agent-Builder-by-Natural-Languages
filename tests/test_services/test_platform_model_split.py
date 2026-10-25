"""Which model the platform calls for which job — `src.services.llm`.

The platform makes model calls on its own behalf for two different jobs, and
until this split it made both on `gpt-4.1` at $2/$8. ADR-0010 §1's argument for
that is about **tool calling** — *"không dùng model gọi tool kém tin cậy"* — and
it is untouched: a loop that decides to call a tool has nothing between a bad
decision and a real action. Writing a JSON document is the other job, and there
`src.workflow.validation` and `AgentSpec` are the quality floor while
`write_document` re-asks with the complaint up to three times.

Measured before it was changed, on four real specs through the real validator,
twelve compositions per arm: `gpt-4.1` **10/12** valid at 1.50 attempts,
`gpt-4.1-mini` **12/12** at 1.33 — at a fifth of the price.

What these tests hold is the part a future edit could quietly undo: that the
document model stays cheap, that the tool loop stays strong, and — the one that
bit during the change itself — that a **creator's** node running the same
weights keeps the creator's temperature.
"""

import logging

from src.config import get_settings
from src.models.model_catalog import (
    ASSIGNABLE_MODELS,
    DEFAULT_NODE_MODEL,
    DOCUMENT_MODEL,
    META_AGENT_MODEL,
    MODEL_CATALOG,
    ModelTier,
    tier_of,
)
from src.services.llm import META_AGENT_TEMPERATURE, _build_chat_model, document_model, meta_agent_model


def test_documents_are_written_by_a_cheap_model_and_decisions_by_a_strong_one() -> None:
    """The split itself, and the reason it is a split rather than one constant.

    A single "platform model" is what made every spec, graph, template filling
    and eval-case batch cost five times what it needed to: they were priced as if
    a wrong answer were unrecoverable, and a wrong answer there is refused by the
    validator and asked again.
    """
    assert tier_of(document_model()) is ModelTier.CHEAP
    # The meta-agent used to be asserted STRONG here, and that assertion was the
    # test form of ADR-0010 §1's "luôn dùng model mạnh". The team moved it to
    # the cost-optimised Luna, so the guarantee is gone and pretending otherwise
    # would make this file lie. What is still held is that the platform *says so
    # out loud* — see the test below. And `document_model() != meta_agent_model()`
    # used to be asserted too: since 2026-10-25 both are `gpt-6-luna`, the
    # team's call, so the two ids are one again by decision rather than accident.


def test_the_meta_agent_is_now_the_cheaper_half_of_the_split() -> None:
    """Not a tautology about tiers — the numbers on the bill, both directions.

    Tier is this codebase's word for a judgement; price is what the provider
    charges. A future catalog edit could keep the tiers and lose the saving, and
    the saving is the entire reason this split exists.
    """
    document, meta = MODEL_CATALOG[document_model()], MODEL_CATALOG[meta_agent_model()]
    # **Inverted, and recorded rather than hidden.** The split existed because
    # the meta-agent's model cost five times the document writer's; once the
    # meta-agent moved to Luna it became the *cheaper* half, and since
    # 2026-10-25 both run `gpt-6-luna` ($0.10/$0.50), so the two are equal.
    assert meta.usd_per_million_input_tokens <= document.usd_per_million_input_tokens
    assert meta.usd_per_million_output_tokens <= document.usd_per_million_output_tokens


def test_neither_of_the_platforms_own_models_is_reachable_by_an_admin() -> None:
    """Reserved ids, exactly like `META_AGENT_MODEL` always was.

    `ASSIGNABLE_MODELS` is the set every admin configuration surface is built
    from, so "an admin cannot point the platform's own calls somewhere else" is a
    property of the data rather than a rule somebody has to remember.
    """
    assert META_AGENT_MODEL not in ASSIGNABLE_MODELS
    assert DOCUMENT_MODEL not in ASSIGNABLE_MODELS


def test_a_creators_node_keeps_the_creators_temperature_on_the_same_weights() -> None:
    """The bug this split nearly shipped with, pinned.

    `DOCUMENT_MODEL` was once the same weights as the assignable `gpt-4.1-mini`
    — `DEFAULT_NODE_MODEL`, on almost every drafted node — and for one commit
    every generated workflow quietly started running at temperature 0, because
    the factory is handed a bare id and the platform's temperature leaked onto
    the creator's node. `DOCUMENT_MODEL` is `gpt-6-luna` now, but the default
    node is still the case a temperature rule can get wrong.
    """
    assert DEFAULT_NODE_MODEL in ASSIGNABLE_MODELS
    assert DEFAULT_NODE_MODEL not in (META_AGENT_MODEL, DOCUMENT_MODEL)
    assert _build_chat_model(DEFAULT_NODE_MODEL).temperature == get_settings().llm_temperature


def _params_of(model) -> dict:
    """The request parameters a bound-or-bare model would actually send."""
    inner = getattr(model, "bound", None) or getattr(model, "steps", [model])[0]
    return inner._default_params


def test_the_meta_agent_can_be_given_tools_at_all() -> None:
    """The meta agent is an agent with tools (ADR-0050), on a family whose provider
    refuses tools and reasoning together.

    Not a hypothetical, and not a regression from choosing Luna either — the two
    are eight days apart. `META_AGENT_MODEL` moved to this family on 2026-08-13
    and served every job fine; `src.metaagent.loop` began binding tools on
    2026-08-21, and from that commit every `workbench/turn` was
    `"Function tools with reasoning_effort are not supported for gpt-5.6-luna in
    /v1/chat/completions"`. Nothing in this repo ever set `reasoning_effort` —
    the family reasons by default — which is why no test caught it: there was no
    line to review.
    """
    assert META_AGENT_MODEL.startswith(("gpt-5", "gpt-6")), "test này mô tả họ gpt-5/gpt-6; hãy sửa nếu model đã đổi"

    with_tools = _params_of(_build_chat_model(META_AGENT_MODEL).bind_tools([{"name": "x", "parameters": {}}]))

    assert with_tools["reasoning_effort"] == "none"


def test_binding_tools_is_also_what_gets_the_meta_agent_its_temperature_back() -> None:
    """`langchain_openai` drops `temperature` for a `gpt-5*` model *while reasoning is on*.

    An earlier test here pinned the loss as permanent — *"on the GPT-5 family
    that determinism is simply not on offer"* — from a measurement that was
    right and a reading that was not: `_validate_temperature` drops it only
    while `reasoning_effort != "none"`. So the switch that makes tools work is
    also the switch that makes `META_AGENT_TEMPERATURE` real, and it lands on
    the one path that runs a loop of up to a dozen round trips.
    """
    with_tools = _params_of(_build_chat_model(META_AGENT_MODEL).bind_tools([{"name": "x", "parameters": {}}]))

    assert with_tools["temperature"] == META_AGENT_TEMPERATURE


def test_a_model_that_binds_no_tools_keeps_the_reasoning_it_had() -> None:
    """The fix is scoped to `bind_tools`, and this is the half that says why.

    Spec writing, workflow generation and prompt writing bind no tools and reason
    happily; they were doing so, reportedly well, for the eight days before the
    tool loop existed. Switching reasoning off for the whole model would have
    been changing what works in order to fix what does not — so the plain path
    still sends no `reasoning_effort`, and still pays the family's temperature
    price for it.
    """
    plain = _params_of(_build_chat_model(META_AGENT_MODEL))

    assert "reasoning_effort" not in plain
    assert "temperature" not in plain, "họ gpt-5/gpt-6 bỏ temperature khi reasoning còn bật — xem docstring"


def test_a_gpt_6_model_is_never_sent_a_temperature_while_it_reasons() -> None:
    """Measured on `gpt-6-luna`: `temperature=0` is a 400, "Only the default (1) value is supported".

    `langchain_openai` strips it for `gpt-5*` only, so for the 6 family the
    factory is the one that has to leave it out.
    """
    assert "temperature" not in _params_of(_build_chat_model("gpt-6-luna"))
    assert _build_chat_model("gpt-6-luna").temperature is None


def test_models_outside_the_reasoning_family_are_untouched() -> None:
    """A wrapper that changed every model would be a second opinion about all of them."""
    assert "reasoning_effort" not in _params_of(_build_chat_model(DEFAULT_NODE_MODEL))
    assert _build_chat_model(DEFAULT_NODE_MODEL).temperature == get_settings().llm_temperature


def test_the_document_model_is_priced_in_the_one_table() -> None:
    """A model the platform calls and does not price is a bill nobody can read.

    `src.agents.costs` and the PM cost board both read `MODEL_CATALOG` and
    nothing else, so a reserved id missing from it spends money that appears in
    no total.
    """
    assert DOCUMENT_MODEL in MODEL_CATALOG


def test_a_weak_meta_agent_model_is_announced_rather_than_refused(caplog) -> None:
    """The guarantee ADR-0010 §1 gave is gone; the *notice* is what replaced it.

    `meta_agent_model` used to raise when the constant was not strong-tier,
    which made "the meta-agent runs on a strong model" something the process
    could not start without. The team chose `gpt-5.6-luna` — the nano-tier
    member of its family — with that trade understood, so the check now reports
    the departure instead of blocking it.

    Pinned at WARNING rather than INFO on purpose: this changes what the
    platform is, and whoever reads a log after noticing that generated specs got
    worse should find the reason there rather than have to infer it. If the
    meta-agent is ever moved back to a strong model, this test says so by
    failing — which is the right way to find out the warning has become noise.

    `.disabled = False` for the same reason `tests/test_services/test_uc1_tracing`
    does it, and that module's fixture is where the full explanation lives:
    alembic's `fileConfig` runs with `disable_existing_loggers=True` on every
    real migration, which silently disables every logger `alembic.ini` does not
    name — process-wide, for the rest of the session, and past `caplog.at_level`,
    which sets the level but never clears `.disabled`. This test passes alone and
    fails in a full run without it.
    """
    assert tier_of(META_AGENT_MODEL) is not ModelTier.STRONG, (
        "meta-agent đã là model tier mạnh — hãy khôi phục chốt chặn cứng thay vì cảnh báo"
    )

    logging.getLogger("src.services.llm").disabled = False
    with caplog.at_level(logging.WARNING, logger="src.services.llm"):
        assert meta_agent_model() == META_AGENT_MODEL

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "không có cảnh báo nào khi meta-agent chạy model tier rẻ"
    assert META_AGENT_MODEL in warnings[0].getMessage()
