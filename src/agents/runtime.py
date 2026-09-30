"""The runtime interpreter — the fixed engine that executes an `AgentSpec`.

See ADR-0003. `AgentSpec` is data, never code: this module is the one place
that turns that data into an executing LangGraph, so every safety property the
spec declares (allowed tools, per-step model, iteration ceiling) is enforced
here rather than trusted from the document.

Each `Step` becomes a node. A step with tools runs a small ReAct loop bounded
by its own `max_iterations` before the graph moves on. Edges follow `next` via
`AgentSpec.next_step_id` — this module never re-derives that rule.

Conversation memory (#6, ADR-0004) is a checkpointer plus a `ThreadRef`: the
shared transcript is no longer built fresh per call but loaded from, and saved
back to, whichever thread the turn belongs to.

What a run reports about itself while it is still running (#8, ADR-0006) is a
stream of `AgentEvent`s: each step as it starts, the answer as it is written,
each tool as it is reached for, and each step as it completes.

Sandbox mode (#9, ADR-0007) is a single flag carried down to the one line that
picks a tool's implementation. Nothing else in this module knows about it: a
trial run and a production run compile the same graph and emit the same events,
which is what makes a trial run trustworthy evidence of what production would do.

Budget enforcement (#10, ADR-0008) is a `BudgetLedger` shared by every step of a
run. The ledger decides *whether* a run may continue; this module decides what
that looks like from outside — a `budget_exceeded` event and a stream that ends.
Because both the chat route and the sandbox go through `stream_agent_reply`, a
trial run and a production run are stopped by the same code (ADR-0008 §3).

Cost and tracing (#11, ADR-0009) land in the same one place, for the same
reason. Every run ends with a `cost` event carrying what it measurably spent —
and reports that same figure as it grows, one `usage` event per charged call, so
a watching screen has a counter rather than a total that arrives at the end
(#21, ADR-0019). Every event it produced is teed into its `RunTrace` on the way
out — so "every run, trial and production, is traced" is true by construction
rather than by two callers each remembering to. A trace that fails is swallowed here and nowhere
else: the run is what matters, and observability that can break it is worse than
none.
"""

import asyncio
import inspect
import logging
import operator
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from functools import partial, reduce
from typing import Annotated, Any, Literal, TypedDict, cast

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from src.agents.budget import BudgetExceededError, BudgetLedger, BudgetLimit
from src.agents.costs import measured_cost
from src.agents.threads import ThreadRef
from src.agents.tools import TOOL_REGISTRY
from src.agents.tools.registry import ToolSpec
from src.models.agent_spec import AgentSpec, Step
from src.services.llm import ChatModelFactory
from src.services.tool_call_repair import repair_tool_call_arguments
from src.services.tracing import RunTrace

logger = logging.getLogger(__name__)


class RuntimeState(TypedDict):
    """The run's shared transcript: the user's message, then one `AIMessage` per completed step.

    A step's own tool-call/tool-result exchange stays local to that step's loop
    and is not added here — only its concluding answer feeds the next step.

    With a checkpointer attached this is also the *thread's* transcript: it
    arrives already holding every earlier turn of the same conversation, which
    is what makes a follow-up question like "còn cái kia thì sao?" answerable.
    """

    messages: Annotated[list[BaseMessage], add_messages]


#: The `RuntimeState` key the transcript lives under. Named here because
#: `Conversation.transcript` has to reach into a raw checkpoint, where the state
#: is a plain dict of channel names and this key is the only way back in.
MESSAGES_CHANNEL = "messages"

#: Where a step stamps its id on its own answer. Deliberately `response_metadata`
#: and not `AIMessage.name`: `name` is a wire field, replayed to the provider on
#: every later turn of the thread, and OpenAI constrains it to `[a-zA-Z0-9_-]+`
#: while `Step.id` has no charset rule at all (see `src.models.agent_spec`). A
#: meta-agent-generated id like "Trích xuất" would be accepted by the spec,
#: checkpointed, and then rejected by the provider on the *second* turn — a
#: failure no fake-model test can reproduce. `response_metadata` never leaves us.
STEP_ID_METADATA_KEY = "step_id"


class TranscriptTurn(TypedDict):
    """One replayable line of a thread's history — see `Conversation.transcript`.

    `step_id` is what makes the readback useful for tracing rather than merely
    for display: it says which step of the spec produced the line.

    The workflow runtime stamps its lines differently — `node_id` and
    `node_type` (`src.workflow.runtime`), plus `tool`/`ok` on an `MCP` note —
    and those ride along here unchanged. They are what lets a chat screen
    rebuild a conversation the way it was first shown: an `agent_llm` line is
    something the agent *said*; an `mcp` or `human_approval` line is something
    the run *did*, to be drawn as activity rather than read out as prose. A line
    with none of them (V1, or a checkpoint older than the stamp) is treated as
    speech, which is exactly how it was always treated.
    """

    role: Literal["user", "agent"]
    content: str
    step_id: str | None
    node_id: str | None
    node_type: str | None
    tool: str | None
    ok: bool | None


class StepEvent(TypedDict):
    """One node's outcome — the unit both the chat stream and the diagram overlay consume."""

    step_id: str
    title: str
    content: str


class StepStartedEvent(TypedDict):
    """A node saying it has begun, before it has anything to show for it — see #21.

    The counterpart of `StepEvent`, and it earns its place twice. A step whose
    first model call is slow, or whose whole job is one long tool call, emits no
    `token` for as long as that takes: without this the flow diagram leaves that
    node grey for exactly the stretch a reader is watching it.

    And it is the only thing that can name the step a *failure* happened at. An
    exception unwinds from wherever it occurred and carries no step with it
    (unlike `BudgetExceededError`, which is raised by the ledger and knows), so
    "which step broke" is answerable only as "the one that had started".
    """

    step_id: str
    title: str


class TokenEvent(TypedDict):
    """A fragment of a step's answer, the moment the model produced it.

    Exists so a chat window has something to render before the step is over: a
    one-step agent otherwise leaves the reader in front of a blank screen for
    the whole run (#8).
    """

    step_id: str
    text: str


class ToolCallEvent(TypedDict):
    """A tool the agent has decided to reach for, announced before it runs.

    Before, not after, on purpose — a tool that hangs or fails is exactly when a
    reader most needs to know what was being attempted.

    `simulated` is read off the implementation that is about to be called, not
    re-derived from the run's mode, so the event cannot claim one thing while
    another runs.
    """

    step_id: str
    tool: str
    args: dict[str, Any]
    simulated: bool


class ToolResultEvent(TypedDict):
    """What a tool gave back. `ok` is false when the call raised.

    A failed tool does not fail the run (ADR-0003 §4: the model gets the failure
    back and reacts to it), so this event is the only place the failure is
    visible from outside.

    `simulated` says whether `result` describes something that happened or
    something that merely *would* have happened — the distinction the whole
    sandbox exists to make (#9).
    """

    step_id: str
    tool: str
    ok: bool
    result: str
    simulated: bool


class BudgetExceededEvent(TypedDict):
    """Why a run stopped short of its last step — see #10 / ADR-0008.

    The numbers travel with it, not just the message: "my agent loops forever"
    and "my ceiling is too tight for the work I asked for" need different fixes,
    and a client that is only handed prose cannot tell a reader which one they
    have.

    `step_id` is where the run was when it ran out, which is what makes the flow
    diagram (#21) able to point at it.
    """

    step_id: str
    limit: BudgetLimit
    limit_value: float
    used: float
    message: str


#: The names an `AgentEvent` can carry. These are the SSE event names on the
#: wire — the HTTP layer relabels nothing, so a reader of this list knows what
#: a browser will see.
AgentEventName = Literal["step_started", "token", "tool", "tool_result", "step", "budget_exceeded", "usage", "cost"]

#: The summary every run ends with, whatever else happened to it — see #11 /
#: ADR-0009. Its payload is `CostFigure.as_payload()`, so it carries the words
#: saying what kind of number it is and cannot be built without them.
#:
#: Named for the same reason `BUDGET_EXCEEDED_EVENT` is: both callers recognise
#: it to record what the run spent, and a literal in three modules is a literal
#: that will drift.
COST_EVENT: AgentEventName = "cost"

#: What the run has spent *so far*, reported after each charged model call — see
#: #21 / ADR-0019. The same `CostFigure.as_payload()` shape as `COST_EVENT`, and
#: read off the same ledger, so the last reading of the counter and the run's
#: final bill are one number rather than two that could disagree.
#:
#: A partial measurement rather than a fourth kind of figure: it is basis
#: `measured` because it reports calls that have actually been billed (ADR-0009).
#: A run that ends between two of them simply has a counter one call behind, and
#: the closing `cost` event is what settles it.
USAGE_EVENT: AgentEventName = "usage"

#: The event a run stopped by its own ceiling ends with. Named because the
#: sandbox recognises it (a stopped run is not evidence — see `src.agents.sandbox`)
#: and a string literal in two modules is a string literal that will drift.
BUDGET_EXCEEDED_EVENT: AgentEventName = "budget_exceeded"


class AgentEvent(TypedDict):
    """One thing worth telling the client about, tagged with what kind of thing it is.

    A single stream rather than one per concern: the chat window, the flow
    diagram overlay (#21) and the sandbox transcript (#9) all read the same
    ordering, and an ordering only exists if there is one stream to have it.
    """

    event: AgentEventName
    data: dict[str, Any]


#: How a step reports something the moment it happens, rather than on return.
EventSink = Callable[[AgentEvent], None]


@dataclass(frozen=True, slots=True)
class RunServices:
    """The outside systems a run talks to, and therefore its test seams.

    All three are supplied by the caller rather than reached for from inside, and
    all three are FastAPI dependencies (`get_chat_model_factory`, `get_tracer`,
    `get_clock`) so a test replaces them the same way it replaces anything else.
    They travel together because every entry point into the runtime needs all of
    them and none is ever chosen independently of the others.

    `sandbox` is deliberately *not* here. It looks like it belongs — it is fixed
    for a run too — but a caller that could set it wrong would be a caller that
    could run a trial against the real world. `stream_sandbox_run` sets it itself,
    which is what keeps that impossible (#9).

    `clock` feeds `BudgetLedger`'s own clock (#58). Defaulting to `time.monotonic`
    keeps every production caller unchanged; a test overrides `get_clock` the same
    way it overrides `get_chat_model_factory`, rather than sleeping for real past
    a `max_wall_seconds` it set — see `src.deployments.rate_limit` for the same
    trade made for the rate limiter (#51).
    """

    model_factory: ChatModelFactory
    trace: RunTrace
    clock: Callable[[], float] = time.monotonic


def get_clock() -> Callable[[], float]:
    """FastAPI dependency: the clock a run's `BudgetLedger` is measured against.

    The real clock outside a test, always — nothing in this module or its
    callers ever has a reason to want a different one. A test that does (#58's
    "a slow reviewer must not cost a run its budget") overrides this the same
    way `get_chat_model_factory` is overridden, in `app.dependency_overrides`.
    """
    return time.monotonic


@dataclass(frozen=True, slots=True)
class RunContext:
    """Everything fixed for the whole of one run, as one argument.

    These travelled together through every layer of this module — node, ReAct
    loop, tool dispatch — and grew by one each time a ticket landed (`sandbox`
    with #9, `ledger` with #10, the trace with #11). They share one lifetime, one
    run, so they are one type; `Conversation` below is the same idea for the
    thread half.

    Frozen so no step can change the terms of the run it is part of: which model
    factory, which domains, which side of the sandbox, which ledger. The ledger
    it points at is itself mutable and deliberately shared — that shared instance
    is what makes the budget cumulative (ADR-0008 §1). Freezing the binding and
    mutating the ledger are not in tension: the run cannot be handed a *different*
    ledger, which is the thing worth preventing.
    """

    services: RunServices
    allowed_domains: tuple[str, ...]
    sandbox: bool
    ledger: BudgetLedger


def _noop_sink(_event: AgentEvent) -> None:
    """What a step reports to when nobody is streaming it."""


def _event(
    name: AgentEventName,
    payload: TokenEvent | ToolCallEvent | ToolResultEvent | StepStartedEvent | StepEvent | BudgetExceededEvent,
) -> AgentEvent:
    """`payload` tagged with the name it travels under.

    The `dict()` is not decoration: `AgentEvent.data` is `dict[str, Any]`, and a
    `TypedDict` is not assignable to that. Doing it here means the four call
    sites do not each have to.
    """
    return AgentEvent(event=name, data=dict(payload))


def text_of(content: Any) -> str:
    """The human-readable text of a message's content, whatever shape it came in.

    Providers differ: a plain string for most, a list of typed blocks for some.
    Anything that is not text (a tool-use block, say) contributes nothing —
    this is what gets shown to a person.

    Public, and the only place that knows those shapes: eval (#13) reads a
    model's reply too, and a second copy of this would be a second thing to fix
    the day a provider changes how it packs its content.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block if isinstance(block, str) else str(block.get("text", "")) for block in content if block is not None
        )
    return ""


def _as_langchain_tool(tool: ToolSpec) -> StructuredTool:
    """A tool-calling schema for the model to choose from.

    The function backing it is never invoked by LangChain: `_run_step_tool_loop`
    reads `AIMessage.tool_calls` itself and dispatches through `TOOL_REGISTRY`, so
    the model only needs `name` and `args_schema` to decide what to call.
    """
    return StructuredTool.from_function(
        func=lambda **_kwargs: None,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


def invoke_tool(
    tool: ToolSpec,
    raw_args: dict[str, Any],
    *,
    allowed_domains: tuple[str, ...],
    implementation: Callable[..., Any],
) -> str:
    """Validate `raw_args` and call `implementation` with them.

    Which implementation to call is the caller's decision (see
    `ToolSpec.implementation`) — this function's job is only to get the
    arguments into whatever shape that callable wants.

    Public because V2's `WebSearch` node calls the same registry entry through
    the same shaping (`src.workflow.runtime`): two copies of "how does a tool's
    arguments reach its implementation" would eventually disagree about one
    tool, and the one they disagreed about would be the one with the unusual
    signature this docstring is about.

    Most tools take their validated args unpacked as keyword arguments;
    `http_request` takes the validated model as a positional `args` plus
    `allowed_domains`. Reading the implementation's own signature avoids
    hard-coding either tool's name here — and a tool's simulation has the same
    signature as its real counterpart, so the same dispatch serves both.
    """
    validated = tool.args_schema.model_validate(raw_args)
    parameters = inspect.signature(implementation).parameters
    extra_kwargs: dict[str, Any] = {"allowed_domains": allowed_domains} if "allowed_domains" in parameters else {}
    if "args" in parameters:
        return str(implementation(validated, **extra_kwargs))
    return str(implementation(**validated.model_dump(), **extra_kwargs))


async def charged_model_call(
    bound_model: Runnable[LanguageModelInput, BaseMessage],
    messages: list[BaseMessage],
    *,
    unit_id: str,
    model_id: str,
    ledger: BudgetLedger,
    on_text: Callable[[str], None],
) -> AIMessage:
    """One model call: streamed as it arrives, cut off by the run's clock, and charged.

    The one place in the platform a model is called on a run's behalf, shared by
    V1's step loop and V2's `AgentLlm` node (`src.workflow.runtime`). Three
    things are wrapped up in it, and each is a thing a second copy would get to
    answer differently:

    - **The deadline** is what makes the time ceiling real rather than advisory.
      `ledger.check` can only fire in the gaps between calls, so a provider that
      accepts the request and then stops answering would sit inside one call for
      as long as it liked — the hang the ticket rules out ("không phải treo")
      rather than the overrun it describes. A call cut off this way is not
      charged: nothing came back to read a token count off, and the run is ending
      on the clock anyway.
    - **The charge** is filed under `model_id` — the caller's *declared* model,
      never one read back off the response — so the ledger knows not only how
      much a run spent but on what (#11), and a provider that renamed the model
      in its reply cannot produce spend belonging to no priced model.
    - **`unit_id`** is what a wall-clock overrun is reported against: a `Step.id`
      for V1, a `NodeInstance.id` for V2. The ledger's own vocabulary is V1's
      (`step_id`), which is why this parameter is named for neither.

    What is *not* here is the usage event. Both callers announce the charge —
    it is the only place a run's spend changes, so it is the only place that can
    say so while the run is still going (#21) — but they announce it in their own
    event vocabulary, and that is the one thing the two runtimes deliberately do
    not share.

    `on_text` rather than an event sink for the same reason: V1 tags a token with
    a `step_id` and V2 with a `node_id`.
    """
    # Snapshotted before the call, because the prompt this call was actually
    # billed for is this list — `messages` has grown by the time it returns.
    request = list(messages)
    try:
        async with asyncio.timeout(ledger.seconds_remaining):
            response = await _stream_model_answer(bound_model, messages, on_text=on_text)
    except TimeoutError:
        raise ledger.wall_clock_exceeded(unit_id) from None
    ledger.record_model_call((request, response), model=model_id)
    return response


async def _stream_model_answer(
    bound_model: Runnable[LanguageModelInput, BaseMessage],
    messages: list[BaseMessage],
    *,
    on_text: Callable[[str], None],
) -> AIMessage:
    """One model call, reported as it arrives rather than when it is finished.

    `astream` and not `ainvoke` so the pieces exist to report at all: a model
    asked to answer in one shot has nothing to say until it has said everything.
    A model that does not implement streaming still works — LangChain falls back
    to a single chunk holding the whole answer, which is the old behaviour.

    Chunks are merged rather than concatenated by hand because a tool call is
    also delivered in pieces, and only `AIMessageChunk`'s own addition knows how
    to reassemble partial tool arguments.

    Reassembled is not the same as well-formed, which is what the repair on the
    way out is for: `repair_tool_call_arguments` runs on the merged message
    because merging is the first moment a tool call is whole enough to judge —
    a partial `arguments` string mid-stream is not malformed, it is unfinished.
    Placing it here rather than in each caller is what makes the guarantee hold
    for both runtimes at once: `charged_model_call` is the one place a model is
    called on a run's behalf, so this is the one place a malformed call can
    enter either runtime's message history.
    """
    chunks: list[BaseMessage] = []
    async for chunk in bound_model.astream(messages):
        chunks.append(chunk)
        text = text_of(chunk.content)
        if text:
            on_text(text)

    if not chunks:
        return AIMessage(content="")
    merged = reduce(operator.add, chunks) if len(chunks) > 1 else chunks[0]
    if not isinstance(merged, AIMessage):
        return AIMessage(content=text_of(merged.content))
    return repair_tool_call_arguments(merged)


def _run_tool_call(call: ToolCall, *, step_id: str, run: RunContext, emit: EventSink) -> str:
    """Announce one tool call, run it, announce what came back — and return that.

    Which implementation runs is resolved once, here, and the `simulated` flag
    on both events is read off that same decision rather than recomputed
    somewhere else. A transcript line claiming a tool was simulated while a
    different line of code chose what actually ran is what would make the
    sandbox's evidence a lie (#9).

    A call that raises comes back as text describing the failure rather than
    propagating: the model reacts to a broken tool the way it reacts to a
    result (ADR-0003 §4), and the `tool_result` event is where the failure
    becomes visible from outside.
    """
    tool = TOOL_REGISTRY.get(call["name"])
    implementation = tool.implementation(sandbox=run.sandbox)
    simulated = implementation is tool.simulate
    emit(_event("tool", ToolCallEvent(step_id=step_id, tool=tool.name, args=dict(call["args"]), simulated=simulated)))
    try:
        result = invoke_tool(tool, call["args"], allowed_domains=run.allowed_domains, implementation=implementation)
        succeeded = True
    except Exception as exc:  # noqa: BLE001 — a tool failure is model-visible, not a crash
        result = f"Lỗi khi gọi tool {tool.name!r}: {exc}"
        succeeded = False
    emit(
        _event(
            "tool_result",
            ToolResultEvent(step_id=step_id, tool=tool.name, ok=succeeded, result=result, simulated=simulated),
        )
    )
    return result


async def _answer_within_deadline(
    bound_model: Runnable[LanguageModelInput, BaseMessage],
    messages: list[BaseMessage],
    *,
    step: Step,
    run: RunContext,
    emit: EventSink,
) -> AIMessage:
    """`charged_model_call` for one step, announced in this runtime's vocabulary.

    The charge is announced here rather than inside the shared call (#21): this
    is the only place a step's spend changes, so it is the only place that can
    say so while the run is still going — and it says it as an `AgentEvent`,
    which is what a V1 client is reading.
    """
    response = await charged_model_call(
        bound_model,
        messages,
        unit_id=step.id,
        model_id=step.model,
        ledger=run.ledger,
        on_text=lambda text: emit(_event("token", TokenEvent(step_id=step.id, text=text))),
    )
    emit(AgentEvent(event=USAGE_EVENT, data=measured_cost(run.ledger).as_payload()))
    return response


async def _run_step_tool_loop(
    step: Step,
    history: list[BaseMessage],
    *,
    run: RunContext,
    emit: EventSink = _noop_sink,
) -> AIMessage:
    """One step's bounded ReAct loop: call the model, run any tool calls, repeat.

    Bounded three times over. `step.max_iterations` caps this step's model calls,
    and nothing upstream can extend that ceiling past what the spec declared. On
    top of it `run.ledger` caps what the *whole run* may spend, which is the only
    bound that reaches the failure this loop can produce: a model that keeps
    asking for a tool, inside a spec whose every step is individually modest
    (#10). And each single call carries a deadline, so one call that never
    returns cannot outlast the run's time ceiling either.

    The ledger is asked before each model call and before each tool call, and
    never immediately after charging one: a call already paid for keeps its
    answer, and whatever the run tries next is soon enough to stop it (ADR-0008
    §2). That is what makes an interrupted step's answer survive in the
    transcript rather than being thrown away after being billed for.

    Everything the loop does is reported through `emit` as it happens (#8): the
    answer as it is written, and each tool both before and after it runs.
    """
    tools = [TOOL_REGISTRY.get(name) for name in step.tools]
    model = run.services.model_factory(step.model)
    bound_model = model.bind_tools([_as_langchain_tool(tool) for tool in tools]) if tools else model

    messages: list[BaseMessage] = [SystemMessage(content=step.prompt), *history]
    response = AIMessage(content="")
    for _ in range(step.max_iterations):
        run.ledger.check(step.id)
        response = await _answer_within_deadline(bound_model, messages, step=step, run=run, emit=emit)
        messages.append(response)
        tool_calls = response.tool_calls
        if not tool_calls:
            break
        for call in tool_calls:
            run.ledger.check(step.id)
            result = _run_tool_call(call, step_id=step.id, run=run, emit=emit)
            messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
    return response


def _build_step_node(step: Step, run: RunContext):
    async def node(state: RuntimeState) -> RuntimeState:
        # LangGraph's own channel for "something happened mid-node". Taking it
        # rather than building a side channel is what keeps mid-run events and
        # end-of-node events in one ordered stream — see ADR-0006.
        emit: EventSink = get_stream_writer()
        # Before anything is attempted, so this is the last thing said about a
        # step that dies on its first call — see `StepStartedEvent`.
        emit(_event("step_started", StepStartedEvent(step_id=step.id, title=step.title)))
        response = await _run_step_tool_loop(step, state["messages"], run=run, emit=emit)
        # Stamped, not merely returned: once this message is checkpointed it
        # outlives the run, and `Conversation.transcript` has no other way to
        # say which step produced a line months later.
        stamped = response.model_copy(
            update={"response_metadata": {**response.response_metadata, STEP_ID_METADATA_KEY: step.id}}
        )
        return {"messages": [stamped]}

    return node


def compile_agent_graph(
    spec: AgentSpec,
    *,
    checkpointer: BaseCheckpointSaver,
    run: RunContext,
) -> CompiledStateGraph:
    """Compile `spec` into a fresh graph that persists its state to `checkpointer`.

    Rebuilt on every call rather than cached per spec: every node closes over the
    same `run`, and a chat request's model factory can be a per-test fake (see the
    model-factory seam in ADR-0003) — caching by spec id would let one request's
    fake leak into another's run. Everything else on `RunContext` is fixed for the
    graph's life for the same reason: no step can change the sandbox mode, the
    allowed domains, or the ledger partway through a run.

    `run.ledger` in particular is one instance shared by every node, so all of
    them charge one total — that sharing *is* the cumulative budget (#10). It is
    passed in rather than built here so that its lifetime is a run rather than a
    graph; the two coincide today, and a caller that ever compiles once and runs
    twice should have to say which it meant.
    """
    graph = StateGraph(RuntimeState)
    for step in spec.steps:
        graph.add_node(step.id, _build_step_node(step, run))
    graph.set_entry_point(spec.steps[0].id)
    for step in spec.steps:
        target = spec.next_step_id(step.id)
        graph.add_edge(step.id, target if target is not None else END)
    return graph.compile(checkpointer=checkpointer)


@dataclass(frozen=True, slots=True)
class Conversation:
    """A thread bound to the store that remembers it.

    The two always travel together — a thread id means nothing without the
    checkpointer holding it, and the checkpointer answers nothing without a
    thread — so they are one argument, and the LangGraph config they produce is
    built in exactly one place. A turn can never be written under a key the
    readback would look for somewhere else.
    """

    thread: ThreadRef
    checkpointer: BaseCheckpointSaver

    @property
    def config(self) -> RunnableConfig:
        """How LangGraph addresses this conversation."""
        return {"configurable": {"thread_id": self.thread.checkpoint_thread_id}}

    async def transcript(self) -> list[TranscriptTurn]:
        """Everything this conversation remembers, oldest first — empty if it was never used.

        Reads the checkpoint directly rather than through a compiled graph:
        tracing a conversation should not require the spec that produced it,
        which by then may have been superseded by a newer version (#7).

        Only the user's messages and the steps' answers appear. A step's
        tool-call scratch work never reaches the shared transcript in the first
        place (ADR-0003), so there is nothing here to filter it out of — and
        nothing here to trace a tool failure with. That is #9's transcript and
        #11's traces, not this.
        """
        snapshot = await self.checkpointer.aget_tuple(self.config)
        if snapshot is None:
            return []

        messages: list[BaseMessage] = snapshot.checkpoint.get("channel_values", {}).get(MESSAGES_CHANNEL, [])
        transcript: list[TranscriptTurn] = []
        for message in messages:
            if isinstance(message, HumanMessage):
                transcript.append(
                    TranscriptTurn(
                        role="user",
                        content=str(message.content),
                        step_id=None,
                        node_id=None,
                        node_type=None,
                        tool=None,
                        ok=None,
                    )
                )
            elif isinstance(message, AIMessage):
                stamped = message.response_metadata
                ok = stamped.get("ok")
                transcript.append(
                    TranscriptTurn(
                        role="agent",
                        content=str(message.content),
                        step_id=stamped.get(STEP_ID_METADATA_KEY),
                        node_id=stamped.get("node_id"),
                        node_type=stamped.get("node_type"),
                        tool=stamped.get("tool"),
                        ok=ok if isinstance(ok, bool) else None,
                    )
                )
        return transcript


def trace_quietly(action: Callable[[], None]) -> None:
    """Do `action`, and let nothing it does reach the run.

    The one place tracing is made safe, so no `RunTrace` implementation has to
    be. A broken observability backend degrades a run to an untraced run and
    never to a failed one — the ticket's *"trace không phải phụ thuộc chặn
    đường"*, enforced rather than promised.

    Public because V2's workflow runtime (`src.workflow.runtime`) records its own
    events through the same `RunTrace` and needs the same guarantee. Two copies
    of this would be two answers to "can observability break a run", and the one
    that mattered would be whichever the failing run happened to go through.

    `Exception` and not a narrower class on purpose: what an HTTP client, an SDK
    or a serialiser raises is not knowledge this module has, and guessing wrong
    would put exactly the failure it is guarding against back on the path.
    """
    try:
        action()
    except Exception:  # noqa: BLE001 — an unrecordable run is still a run
        logger.warning("Ghi trace thất bại — lần chạy vẫn tiếp tục bình thường", exc_info=True)


async def _run_events(stream: AsyncIterator[Any], spec: AgentSpec, run: RunContext) -> AsyncIterator[AgentEvent]:
    """Everything the run itself has to say, up to and including why it stopped.

    Two LangGraph stream modes feed it (ADR-0006): `custom` carries what a step
    reports from inside itself (tokens, tool calls), `updates` carries a step's
    completion. Interleaved by LangGraph in real time, so the order events reach
    a client is the order they actually happened in.

    The ledger is asked once more after the graph finishes, against the last step
    to report. Without it a run whose *final* step went over would end looking
    perfectly ordinary — the ceiling was exceeded and nobody was told, which is
    the same silent-failure shape ADR-0008 §4 refuses for unreported usage.

    Separate from `stream_agent_reply` so that the accounting there wraps it
    exactly once: the tee into the trace, the cost event and the re-raise belong
    to *every* path out of here, and one `async for` is how that gets written down
    once instead of at each `yield`.
    """
    titles = {step.id: step.title for step in spec.steps}
    last_step_id = spec.steps[-1].id
    try:
        async for mode, payload in stream:
            if mode == "custom":
                # Already an `AgentEvent`: the step that wrote it is the only writer
                # on this channel, and it writes nothing else.
                yield cast(AgentEvent, payload)
                continue
            for step_id, node_output in cast(dict[str, RuntimeState], payload).items():
                last_step_id = step_id
                new_messages = node_output["messages"]
                content = text_of(new_messages[-1].content) if new_messages else ""
                yield _event("step", StepEvent(step_id=step_id, title=titles[step_id], content=content))
        run.ledger.check(last_step_id)
    except BudgetExceededError as exceeded:
        yield _event(
            BUDGET_EXCEEDED_EVENT,
            BudgetExceededEvent(
                step_id=exceeded.step_id,
                limit=exceeded.limit,
                limit_value=exceeded.limit_value,
                used=exceeded.used,
                message=exceeded.message,
            ),
        )


async def stream_agent_reply(
    spec: AgentSpec,
    message: str,
    *,
    services: RunServices,
    conversation: Conversation,
    sandbox: bool,
) -> AsyncIterator[AgentEvent]:
    """Run `spec` against `message` on `conversation`, yielding every `AgentEvent` as it happens.

    One event stream serves several audiences — the chat window renders tokens
    and tool calls as they arrive, the flow diagram lights up `step_id` as it
    arrives — so no second execution-tracking mechanism exists (ADR-0003).

    What the run itself says is `_run_events`; this function is what happens
    around it on every path — the trace, the accounting, the re-raise.

    `message` is the only thing seeded into the state: LangGraph loads the rest
    of the conversation's transcript itself, so earlier turns of this
    conversation — and *only* this conversation — are already in context when
    the first step runs.

    `sandbox=True` swaps every side-effecting tool for its simulation (#9).
    There is deliberately no default: both defaults are wrong in one direction
    — `False` lets a forgotten argument send a real email from an unapproved
    draft, `True` lets one silently swallow a real customer's email — so each
    of the two callers states which world its run belongs to.

    A run that spends past `spec.budget` ends here, with a `budget_exceeded`
    event as its last (#10). The overrun arrives as an exception from inside a
    step — the only thing that can unwind a ReAct loop from wherever it happens
    to be — and is turned into an event in this one place, so both callers get
    the same structured stop instead of each recognising an exception for itself.
    Whatever the earlier steps already did stays in the thread: they were
    checkpointed as they completed, and this only stops what comes next.

    Every run ends with a `cost` event and a finished trace (#11) — **every**
    run, including one that crashed, which is why the failure is caught, reported
    and only then re-raised. A run that broke halfway still spent real money, and
    a spend record that quietly skipped the runs that went wrong would understate
    exactly the agents worth looking at.
    """
    ledger = BudgetLedger(spec.budget, now=services.clock)
    run = RunContext(
        services=services,
        allowed_domains=tuple(spec.allowed_domains),
        sandbox=sandbox,
        ledger=ledger,
    )
    graph = compile_agent_graph(spec, checkpointer=conversation.checkpointer, run=run)
    turn: RuntimeState = {"messages": [HumanMessage(content=message)]}
    stream = graph.astream(turn, config=conversation.config, stream_mode=["custom", "updates"])

    # Held rather than propagated on the spot: the run has to be accounted for
    # before it is reported broken. Re-raised below unchanged, so the layer that
    # owns errors still sees exactly what happened.
    failure: Exception | None = None
    try:
        async for event in _run_events(stream, spec, run):
            trace_quietly(partial(services.trace.record, event["event"], event["data"]))
            yield event
    except Exception as exc:  # noqa: BLE001 — re-raised at the end of this function
        failure = exc

    cost = measured_cost(ledger)
    # Before the yield, not after: a client that hangs up mid-stream would never
    # let the last line run, and an unfinished trace of a run that did happen is
    # the one an investigator most wants.
    trace_quietly(partial(services.trace.finish, cost=cost, error=str(failure) if failure is not None else None))
    yield AgentEvent(event=COST_EVENT, data=cost.as_payload())
    if failure is not None:
        raise failure
