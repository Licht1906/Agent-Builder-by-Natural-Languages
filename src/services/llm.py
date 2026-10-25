"""The one seam allowed to construct a chat model.

Two call sites will need an LLM: the meta-agent (spec generation) and the
runtime (per-step execution). If each built its own `ChatOpenAI`, tests would
have to fake out two places — or hit the real API and become slow and
flaky — instead this one factory is the single override point for both,
via `app.dependency_overrides[get_chat_model_factory]`.

Two *providers* answer through it, too: OpenAI directly for the meta-agent and
the assignable GPT models, OpenRouter for the agent con (§6.3). Which one a
given model goes to is decided by `model_catalog.is_openrouter_model` and
nowhere else — see `_build_chat_model`. Being one seam is what makes that
possible: a second factory would mean a second place to get the routing wrong.

It is declared as a *factory* dependency, not a model instance, because the
model id is a per-step choice (`AgentSpec.Step.model`) — not something FastAPI
can inject from the request.
"""

import logging
from collections.abc import Callable
from typing import Any

import httpx
import openai
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from src.config import get_settings
from src.models.model_catalog import (
    DOCUMENT_MODEL,
    META_AGENT_MODEL,
    ModelTier,
    is_openrouter_model,
    served_model,
    tier_of,
)

ChatModelFactory = Callable[[str], BaseChatModel]

#: The two model ids the platform calls *on its own behalf* — one to decide, one
#: to write. Both are reserved ids, out of `ASSIGNABLE_MODELS`, and that is what
#: makes this set usable: `DOCUMENT_MODEL` is the same weights as the assignable
#: `gpt-4.1-mini`, so a creator's node running that model must keep the
#: creator's temperature while the platform's own call gets zero. The factory is
logger = logging.getLogger(__name__)

#: handed a bare id and has nothing else to tell the two apart by.
_PLATFORM_OWN_MODELS: frozenset[str] = frozenset({META_AGENT_MODEL, DOCUMENT_MODEL})

#: The classes a provider failure arrives as, for `src.main` to hang a handler
#: on. Named here rather than imported there, because this module is the one
#: place allowed to know which SDK answers for a model — a second import of
#: `openai` elsewhere is the beginning of a second opinion about that.
#:
#: Both providers route through `ChatOpenAI` (OpenRouter speaks the same wire
#: protocol), so `openai.OpenAIError` covers both; `httpx` is under it and gets
#: to the socket errors the SDK does not wrap.
PROVIDER_ERROR_TYPES: tuple[type[BaseException], ...] = (openai.OpenAIError, httpx.HTTPError)


#: What the platform's own calls run at, and it is not `llm_temperature`.
#:
#: Every one of them asks for a **JSON document against a schema** — a spec, a
#: graph, a template choice, a reading of an interview — and not one of them
#: wants variety. At the creator-facing temperature the same confirmed spec
#: produces a different workflow on Tuesday than it did on Monday, which is a
#: platform nobody can debug and nobody can demo. `llm_temperature` goes on
#: being what a creator's *agent steps* run at, where warmth is the point.
#:
#: Zero rather than a setting, for `meta_agent_model`'s reason: how the platform
#: writes its own documents is the team's decision, not an admin's.
META_AGENT_TEMPERATURE = 0.0


def _is_reasoning_family(model: str) -> bool:
    """Whether `model` is a GPT-5- or GPT-6-family id whose provider reasons by default."""
    lowered = model.lower()
    return lowered.startswith(("gpt-5", "gpt-6")) and "chat" not in lowered


class _ReasoningOffWhenToolsAreBound:
    """A GPT-5-family model that switches reasoning off the moment it is given tools.

    OpenAI refuses tools *and* reasoning together on `/v1/chat/completions`:
    `"Function tools with reasoning_effort are not supported for gpt-5.6-luna
    ... use /v1/responses or set reasoning_effort to 'none'"`. Nothing here ever
    set `reasoning_effort` — the family reasons by default, so the rejected
    combination arrives without anyone asking for it. Every `workbench/turn` was
    a 400 from the day `src.metaagent.loop` started binding tools (ADR-0050) on
    a model that had been serving every other job for eight days without one.

    **Why `bind_tools` and not the constructor.** Those eight days are the
    argument. Spec writing, workflow generation and prompt writing bind no tools,
    reason happily, and were reported good — so switching reasoning off for the
    whole model would be changing what works to fix what does not. `bind_tools`
    is where the provider's actual rule bites, so it is where the rule is
    written down.

    A wrapper rather than a `.bind(...)` because `reasoning_effort` has to be set
    at *construction*: `langchain_openai._validate_temperature` drops
    `temperature` for a `gpt-5*` model while reasoning is on, so a model built
    with reasoning and patched afterwards would take tools and still silently
    throw `META_AGENT_TEMPERATURE` away. Rebuilding is what gets the meta agent
    the determinism `META_AGENT_TEMPERATURE` exists to promise — on the one path
    that runs a loop and can least afford to wander.

    `__getattr__` forwards everything else, `_MeteredChatModel`'s reason: this
    exists to change one method and must not become an opinion about the rest.
    """

    def __init__(self, inner: BaseChatModel, model: str) -> None:
        self._inner = inner
        self._model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def bind_tools(self, *args: Any, **kwargs: Any) -> Any:
        return _build_chat_model(self._model, _reasoning="none").bind_tools(*args, **kwargs)


def _build_chat_model(model: str, *, _reasoning: str | None = None) -> BaseChatModel:
    """Build `model`, routed to whichever provider actually serves it.

    Two providers now answer through this one factory, and which one is a fact
    about the model (`is_openrouter_model`), never about the caller — see that
    predicate's own note on why the routing table cannot live at the call sites.

    Still `ChatOpenAI` for both, because OpenRouter speaks the OpenAI wire
    protocol; only the base URL and the key differ. That is also why this is a
    branch on two arguments rather than two classes: a second client type would
    have to re-derive the temperature rule, the tool-binding behaviour, and every
    LangChain integration detail that `ChatOpenAI` already gets right.
    """
    # A stored node naming a model the provider has retired runs on its
    # replacement rather than 404ing mid-run — see `RETIRED_MODELS`.
    model = served_model(model)
    settings = get_settings()
    if is_openrouter_model(model):
        # Fail here, loudly, rather than sending an unauthenticated request. This
        # key now gates every agent con turn (§6.3), so an unset one is a
        # misconfigured deployment, not a degraded feature — and OpenRouter's own
        # 401 surfaces deep inside a LangGraph node, where it reads as "the agent
        # broke" rather than "nobody filled in the .env".
        if not settings.openrouter_api_key:
            raise RuntimeError(
                f"Model {model!r} chạy qua OpenRouter nhưng OPENROUTER_API_KEY chưa được đặt — xem .env.example"
            )
        return ChatOpenAI(
            model=model,
            base_url=settings.openrouter_base_url,
            api_key=SecretStr(settings.openrouter_api_key),
            temperature=settings.llm_temperature,
        )
    if _is_reasoning_family(model) and _reasoning is None:
        return _ReasoningOffWhenToolsAreBound(_build_chat_model(model, _reasoning=""), model)  # type: ignore[return-value]
    # A reasoning model accepts only the default temperature while reasoning is
    # on. `langchain_openai` drops a non-default one for `gpt-5*` itself, but not
    # for `gpt-6*` — measured: `gpt-6-luna` answers `temperature=0` with a 400 —
    # so it is left unset here for the whole family rather than per prefix.
    reasoning_on = _is_reasoning_family(model) and _reasoning != "none"
    return ChatOpenAI(
        model=model,
        api_key=SecretStr(settings.openai_api_key),
        **({"reasoning_effort": _reasoning} if _reasoning else {}),
        # Both of the platform's own models, not just the meta agent's. When
        # document writing moved to `DOCUMENT_MODEL` this branch still named one
        # of them, so every spec and every graph quietly started being written at
        # the *creator's* temperature — the exact non-determinism the constant
        # above exists to refuse, arriving as a side effect of a price change.
        **(
            {}
            if reasoning_on
            else {"temperature": META_AGENT_TEMPERATURE if model in _PLATFORM_OWN_MODELS else settings.llm_temperature}
        ),
    )


def get_chat_model_factory() -> ChatModelFactory:
    """FastAPI dependency: the only function in the system allowed to build a chat model."""
    return _build_chat_model


def meta_agent_model() -> str:
    """The model the meta-agent runs on — see #12 / ADR-0010 §1.

    A function of nothing: not a setting, not an environment variable, and not
    the admin allowlist. The meta-agent is the hardest part of the system, so
    which model it uses is the team's decision and stays a constant in code — the
    issue is explicit that it *"do đội cố định và luôn dùng model mạnh"*.

    Asking here rather than reading `META_AGENT_MODEL` directly gives the spec
    generator (#16, #17) one place to get its model from, and one place that
    checks the "always strong" half of that promise before handing it over.
    """
    if tier_of(META_AGENT_MODEL) is not ModelTier.STRONG:
        # Safety net: if META_AGENT_MODEL is ever changed back to a CHEAP model,
        # this warns rather than blocking startup — the meta-agent is the hardest
        # part of the system and using a cheap model degrades spec/graph quality.
        # WARNING and not INFO: whoever reads logs after noticing worse output
        # should find this line immediately, not infer it.
        logger.warning(
            "Model của meta-agent (%r) không thuộc tier mạnh — meta-agent là phần khó nhất "
            "của hệ thống; chất lượng spec/graph sinh ra có thể giảm và việc gọi tool kém tin cậy hơn",
            META_AGENT_MODEL,
        )
    return META_AGENT_MODEL


#: The model that *writes the platform's own documents* — a spec, a graph, a
#: template's wording, a set of eval cases, a reading of an interview.
#:
#: Split out of `meta_agent_model` rather than added beside it, because the two
#: names answer two different questions and ADR-0010 §1 only ever answered one
#: of them. That ADR's argument is about **tool calling**: *"không dùng model gọi
#: tool kém tin cậy"* — a loop that decides to call a tool has nothing standing
#: between a bad decision and the action, so the model is the only quality floor
#: there is. Writing a document is not that job. `src.workflow.validation`
#: decides whether a graph can run, `AgentSpec` decides whether a spec is
#: coherent, and `write_document` re-asks with the complaint in hand up to three
#: times. **The validator is the floor, not the model** — which is the whole
#: argument of ADR-0041, applied to the model choice it never made.
#:
#: Measured before it was changed, on four real specs through the real
#: validator: `gpt-4.1` drew a valid graph 4/4 at 1.25 attempts and $0.0147 a
#: graph; `gpt-4.1-mini` drew a valid graph 4/4 at 1.50 attempts and $0.0033 —
#: **4.5× cheaper for the same thing**. The extra attempt is exactly what the
#: retry loop is for, and it is included in that price. What it does cost is
#: wall-clock: 17s against 8s a graph, because a second attempt is a second
#: round trip. That is the trade, stated: a creator waits nine seconds longer at
#: a spinner they already expected to wait at, once per version.
def document_model() -> str:
    """The model documents are written by — see `DOCUMENT_MODEL`.

    A function rather than a bare constant for `meta_agent_model`'s reason: this
    is the one place that checks the promise before handing the name over. The
    promise here is the opposite one, and it is worth failing loudly on. A
    *strong* model would silently work and silently cost five times as much, so
    what this refuses is the accident where somebody points documents back at the
    expensive model without meaning to.
    """
    if tier_of(DOCUMENT_MODEL) is not ModelTier.CHEAP:
        raise RuntimeError(
            f"Model viết tài liệu ({DOCUMENT_MODEL!r}) phải thuộc tier rẻ — "
            "tài liệu đã có validator chặn ở cuối, model mạnh chỉ đắt hơn chứ không đúng hơn"
        )
    return DOCUMENT_MODEL


#: One sentence per way the provider can refuse, keyed by cause rather than by
#: the provider's own prose — that prose is English, changes without notice, and
#: points at an OpenAI settings page only the operator can open.
#:
#: All five say the same two things, because both are what the creator on the
#: screen actually needs: **this is not your fault**, and **what you typed is
#: still there**. Without them the sentence reads as a verdict on the spec they
#: just wrote, and the creator's next move is to rewrite a document that was
#: fine.
_PROVIDER_REFUSALS = {
    "quota": (
        "Tài khoản model của nền tảng đã chạm trần chi tiêu nên chưa gọi được model nào. "
        "Đây là lỗi cấu hình của nền tảng, không phải của bạn — báo quản trị viên nâng trần "
        "rồi bấm lại; những gì bạn đã nhập vẫn còn nguyên."
    ),
    "rate": ("Nhà cung cấp model đang quá tải. Chờ một hai phút rồi bấm lại — những gì bạn đã nhập vẫn còn nguyên."),
    "auth": (
        "Khoá API của nền tảng bị từ chối nên chưa gọi được model nào. "
        "Đây là lỗi cấu hình của nền tảng, không phải của bạn — báo quản trị viên."
    ),
    "network": (
        "Không kết nối được tới nhà cung cấp model. Thử lại sau ít phút; "
        "nếu vẫn vậy thì báo quản trị viên. Những gì bạn đã nhập vẫn còn nguyên."
    ),
    "other": (
        "Nhà cung cấp model trả về lỗi nên bước này chưa chạy được. Thử lại sau ít phút; "
        "nếu vẫn vậy thì báo quản trị viên. Những gì bạn đã nhập vẫn còn nguyên."
    ),
}

#: The sentence for a provider failure nothing more specific fits. Public so the
#: handler in `src.main` has something to say without keeping a second copy of a
#: sentence — two copies of a refusal is two refusals that drift apart.
PROVIDER_REFUSAL_UNKNOWN = _PROVIDER_REFUSALS["other"]


def provider_failure_of(exc: BaseException) -> str | None:
    """The sentence for a model-provider refusal, or `None` if `exc` is not one.

    The platform has exactly one way to *reach* a model — `_build_chat_model`
    above — and had no way at all to fail to reach one. An `openai.APIError`
    escaped `write_document`, escaped `compose_workflow`, escaped the fallback in
    `src.workflow.generator` (which catches `GraphCompositionError` and nothing
    else), escaped the route, and arrived at the creator as a bare
    `500 Internal Server Error` with no `detail` for the frontend to read. That
    is the worst available answer to the most ordinary outage: 500 is the one
    status that means *this platform is broken* when the truth was *the bill is
    capped*, and the creator who reads it goes back to rewrite a spec that was
    never the problem.

    Recognised by walking `__cause__`/`__context__` as well as the exception
    itself. LangChain passes the provider's error straight through on the paths
    measured here, but not on all of them, and a translation that only worked
    unwrapped would hold on the composer and quietly stop holding on chat.

    Deliberately **not** a retry, and deliberately not a fallback either. A quota
    stop retried is a quota stop three times slower; and the template fallback
    the composer would otherwise reach for needs the very model that just
    refused, so falling back would only move the crash one call to the right.
    """
    return next((_refusal_for(error) for error in _causes(exc) if _is_provider_error(error)), None)


def _is_provider_error(error: BaseException) -> bool:
    """True for anything raised by the provider SDK or the HTTP client under it."""
    return type(error).__module__.split(".")[0] in {"openai", "httpx"}


def _causes(exc: BaseException) -> list[BaseException]:
    """`exc` and everything it was raised from, outermost first. Cycles cannot loop it."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _refusal_for(error: BaseException) -> str:
    """Which of `_PROVIDER_REFUSALS` this one is.

    `code` before class name, in that order of trust for one specific reason:
    *the account is out of money* and *you are asking too fast* both arrive as
    `RateLimitError`, and only `code` tells them apart — while the advice on the
    screen is opposite for the two (fetch an admin, versus wait a minute).
    """
    body = error.body if isinstance(getattr(error, "body", None), dict) else {}
    code = str(getattr(error, "code", None) or body.get("code") or body.get("type") or "")
    name = type(error).__name__
    if "quota" in code or "spend_limit" in code or "billing" in code:
        return _PROVIDER_REFUSALS["quota"]
    if name == "RateLimitError":
        return _PROVIDER_REFUSALS["rate"]
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return _PROVIDER_REFUSALS["auth"]
    if name in {"APIConnectionError", "APITimeoutError"} or name.startswith(("Connect", "Read", "Write", "Pool")):
        return _PROVIDER_REFUSALS["network"]
    return _PROVIDER_REFUSALS["other"]
