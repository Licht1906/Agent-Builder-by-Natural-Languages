"""UC1-10 (#124) — `compile_spec`, the one function that turns an `AgentSpec`
into a runnable `StateGraph`, plus the frozen policy-input spec every PEP call
made through that graph carries.

Two design choices carry the whole ticket:

**The PEP input is frozen at compile time, independent of the spec's real
lifecycle status.** `authz.rego` denies outright unless `spec.metadata.status
== "deployed"` — but §8's own step order compiles a spec *before* HITL #2, so
a sandbox chat (UC1-38 / #126) against a `draft`/`pending_approval` version
would get nothing but denials if it sent the spec's real status. §7's "đóng
gói spec (status: deployed) làm input cố định cho PEP" resolves this: compile
produces a *copy* of `spec` with `metadata.status` forced to `deployed`
— `CompiledAgent.policy_spec` — and every tool call this graph ever makes
sends that copy, never the live one. Whether the version may serve *real*
production traffic yet is a question `src.agentspec.lifecycle` answers
separately; this module answers "does the graph itself behave as its signed
spec says", which is true starting the moment it compiles.

**A tool is bound to a name in `capabilities[]`, never the other way round.**
`_bound_tools` walks `spec.capabilities` and looks up an implementation for
each — a name compiled in that the spec never granted cannot exist, because
nothing here ever iterates `tool_implementations` looking for extras to add.
"""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agentspec.spec import AgentSpec, Connection, Fallback, SpecStatus
from src.agentspec.views import DisallowedBaseTableError, assert_allowed_objects_are_views
from src.compiler import guardrails
from src.compiler.state import (
    EMPTY_PROVENANCE,
    EMPTY_SESSION,
    AgentState,
    PendingActionState,
    ProvenanceState,
    SessionState,
    TranscriptStep,
    merge_provenance,
    merge_session,
)
from src.compiler.tools import (
    TOOL_SCHEMAS,
    ApprovalRequiredError,
    BoundTool,
    ToolCallContext,
    ToolDeniedError,
    ToolImplementation,
    execute_tool_call,
)
from src.models.model_catalog import AGENT_MODEL as AGENT_MODEL
from src.policy.pep import AuditSink, PEPClient, Principal
from src.services.tool_call_repair import repair_tool_call_arguments

#: `AGENT_MODEL` is re-exported (above) from `src.models.model_catalog`, where it
#: now lives so `src.services.llm` can route it to OpenRouter without importing
#: this module. Still importable from here because every caller that compiles a
#: spec already reaches for `src.compiler.graph.AGENT_MODEL`, and this is the
#: module that says what a compiled agent runs on.

#: A nominal per-model-call price — an ESTIMATE, in the sense
#: `src.agents.costs` already gives that word a precise meaning: a guess, not
#: a measurement. `AGENT_MODEL` now *has* a real per-token rate in
#: `MODEL_CATALOG` (OpenRouter publishes one), so what is still missing is the
#: other half: the per-call token counts to multiply it by, which are
#: observability wiring (UC1-31, Langfuse) that has not landed yet. Until they
#: do, this is what keeps a sandbox transcript (UC1-38 / #126) from reporting a
#: run that plainly cost something as a flat zero.
NOMINAL_MODEL_CALL_COST_USD = 0.002

#: Substrings `authz.rego`'s own two budget-deny messages both contain
#: ("session tool-call budget exhausted (%v)", "per-tool budget exhausted for
#: %q (%v)") — how the tools node recognises "this turn just hit its ceiling"
#: without re-deriving OPA's own wording.
BUDGET_DENIAL_MARKER = "budget exhausted"

_AGENT_NODE = "agent"
_TOOLS_NODE = "tools"


class UnboundCapabilityError(ValueError):
    """`spec.capabilities` names a tool `tool_implementations` has no callable for.

    Distinct from `is_known_tool` (UC1-07 / #121, checked by `validate_spec`
    long before compile): that check is "does the platform's catalogue know
    this name at all". This one is "does *this* compile call have a real
    implementation to bind it to" — a gap in `tool_implementations`, not a
    spec that invented a tool.
    """

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(f"capability {tool!r} không có implementation để bind")


@dataclass(frozen=True, slots=True)
class CompiledAgent:
    """One version's compiled artifact — §7's "policy bundle" and executable graph together.

    `agent_id`/`version` are what a caller keys a `CompiledAgentRegistry` by;
    `policy_spec` is what every tool call this `graph` makes sends the PEP.
    """

    agent_id: str
    version: int
    policy_spec: AgentSpec
    graph: CompiledStateGraph
    bound_tool_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class CompileDependencies:
    """Everything about *how* to run the graph, bundled into one argument.

    `compile_spec(spec, model, tool_implementations, pep_client, checkpointer)`
    broke the 3-parameter rule (`docs/code-style/python.md` §2); this is
    the same fix `src.workflow.runtime.compile_workflow_graph` already applies
    with `WorkflowRunContext` — everything fixed for the whole of one compile
    call, as one argument, leaving `spec` as the one thing that varies on its
    own axis (which spec) rather than lumping it in with the deps.
    """

    model: BaseChatModel
    tool_implementations: Mapping[str, ToolImplementation]
    pep_client: PEPClient
    #: UC1-32 (#144) — threaded straight into every `ToolCallContext` the
    #: compiled graph's tools node builds (`_tools_node`/`_run_one_call`
    #: below). Required for the same reason `ToolCallContext.audit_sink` is:
    #: a compile call that forgot to wire one would compile a graph that
    #: makes real PEP decisions with no audit trail.
    audit_sink: AuditSink
    checkpointer: BaseCheckpointSaver | None = None
    #: FR-24 at compile time, not only at probe time (UC1-09 / #123's own
    #: "tại thời điểm probe/compile"): `connection.id -> the view names its
    #: most recent successful probe actually confirmed`
    #: (`src.workflow.uc1_connections.verified_objects_for_spec`). Empty by
    #: default — a caller that never supplies this (every test in this repo
    #: that compiles a spec with no `connections[]`) gets no check at all,
    #: which is correct: there is nothing to verify. A caller compiling a
    #: real spec that *does* declare connections must supply this
    #: deliberately; the empty default never silently waves one through.
    verified_connection_objects: Mapping[str, Sequence[str]] = field(default_factory=dict)


def compile_spec(spec: AgentSpec, deps: CompileDependencies) -> CompiledAgent:
    """`spec` → a `CompiledAgent`. Never mutates `spec`; `policy_spec` is a fresh copy.

    `deps.model` arrives already resolved to a real `BaseChatModel` (the
    caller goes through `ChatModelFactory(AGENT_MODEL)`, mirroring
    `meta_agent_model`'s own seam) rather than a factory, because a compile
    call has exactly one model to bind tools to and no per-step choice to
    make (unlike V2's per-`Step` model — this spec has no such field, §7's
    table only names a single agent con).

    Raises `UnverifiedConnectionObjectsError` if any connection's
    `allowed_objects` names something its last successful probe did not
    confirm as a view — a `DisallowedBaseTableError` subclass, so a caller
    still handles one exception type regardless of which of the two moments
    (probe or compile) caught the problem (FR-24), while the message stays
    honest about which one did.
    """
    _assert_connections_probed_as_views(spec, deps.verified_connection_objects)
    policy_spec = spec.model_copy(update={"metadata": spec.metadata.model_copy(update={"status": SpecStatus.DEPLOYED})})
    bound_tools = _bound_tools(spec, deps.tool_implementations)

    graph: StateGraph[AgentState] = StateGraph(AgentState)
    graph.add_node(_AGENT_NODE, _agent_node(deps.model, spec))
    graph.add_node(
        _TOOLS_NODE,
        _tools_node(
            bound_tools, _ToolsNodeConfig(policy_spec=policy_spec, pep_client=deps.pep_client, audit_sink=deps.audit_sink)
        ),
    )
    graph.set_entry_point(_AGENT_NODE)
    graph.add_conditional_edges(_AGENT_NODE, _route_after_agent, {_TOOLS_NODE: _TOOLS_NODE, END: END})
    # Not a fixed edge: a tools pass that just hit `authority.budget` sets
    # `budget_exceeded` and this turn has to stop right there (FR-39's "dừng
    # đúng lúc") rather than loop back to an agent node that has no way to
    # know why its last attempt was refused.
    graph.add_conditional_edges(_TOOLS_NODE, _route_after_tools, {_AGENT_NODE: _AGENT_NODE, END: END})

    return CompiledAgent(
        agent_id=spec.metadata.id,
        version=spec.metadata.version,
        policy_spec=policy_spec,
        graph=graph.compile(checkpointer=deps.checkpointer),
        bound_tool_names=frozenset(bound_tools),
    )


class UnverifiedConnectionObjectsError(DisallowedBaseTableError):
    """`allowed_objects` names something this connection's last successful probe did not confirm.

    A subclass, so every caller that already catches FR-24 at compile time
    (`src.api.sandbox_routes`, `src.api.uc1_agent_chat_routes`, both of which
    surface `str(exc)` as a 422 detail) keeps catching it unchanged — but with
    its own wording, because at compile time the parent's is a lie about the
    cause. `assert_allowed_objects_are_views` reads `information_schema.views`
    only when the *probe* calls it; here its `list_views` is "whatever the last
    successful probe recorded" (`verified_objects_for_spec`), so a name lands
    in `offending` when nobody probed the connection, when the connection row
    is named something other than `credential_ref` says, or when the probe ran
    before `allowed_objects` grew — none of which is a base table, and all of
    which sent readers to look for one. `v_ar_open`/`v_customer_safe` are
    views (migration `a4c2c2363959`); being told they are not is the symptom
    of an unverified connection, never of the objects themselves.
    """

    def __init__(self, *, connection: Connection, objects: Sequence[str], verified: Sequence[str]) -> None:
        self.connection_id = connection.id
        self.verified: list[str] = list(verified)
        confirmed = (
            "probe gần nhất chưa xác nhận view nào (chưa probe, probe fail, hoặc "
            f"chưa có kết nối nào tên {connection.credential_ref.removeprefix('vault://conn/')!r})"
            if not verified
            else f"probe gần nhất chỉ xác nhận: {', '.join(verified)}"
        )
        ValueError.__init__(
            self,
            f"connection {connection.id!r} khai allowed_objects chưa được xác thực: {', '.join(objects)} — "
            f"{confirmed}. Compiler không tự đọc information_schema (FR-24): probe lại kết nối "
            f"{connection.credential_ref} với đúng danh sách object rồi compile lại.",
        )
        self.objects: list[str] = list(objects)


def _assert_connections_probed_as_views(spec: AgentSpec, verified_objects: Mapping[str, Sequence[str]]) -> None:
    """FR-24: refuse to compile unless every connection's `allowed_objects` is inside the set
    its most recent successful probe confirmed — never dials Vault or Postgres itself
    (`deps.model`'s own docstring: credentials resolve at runtime, never at compile time).
    An unprobed connection's `verified_objects.get(id, ())` is empty, which fails exactly
    like a bare base table would — an unverified claim is not one this compiler accepts fresh.

    Still routed through `assert_allowed_objects_are_views` so probe and compiler cannot
    drift apart on what "inside the confirmed set" means (`src.agentspec.views`' own
    "two different callers that must never drift"); only the *message* is re-labelled,
    by `UnverifiedConnectionObjectsError` above.
    """
    for connection in spec.connections:
        verified = verified_objects.get(connection.id, ())
        try:
            assert_allowed_objects_are_views(connection.allowed_objects, list_views=lambda _schema: verified)  # noqa: B023
        except DisallowedBaseTableError as exc:
            raise UnverifiedConnectionObjectsError(
                connection=connection, objects=exc.objects, verified=verified
            ) from None


def _bound_tools(spec: AgentSpec, tool_implementations: Mapping[str, ToolImplementation]) -> dict[str, BoundTool]:
    bound: dict[str, BoundTool] = {}
    for capability in spec.capabilities:
        implementation = tool_implementations.get(capability.tool)
        if implementation is None:
            raise UnboundCapabilityError(capability.tool)
        bound[capability.tool] = BoundTool(
            name=capability.tool,
            scope=capability.scope,
            connection_id=_connection_id_for(spec, capability.tool),
            implementation=implementation,
        )
    return bound


def _connection_id_for(spec: AgentSpec, tool: str) -> str:
    """The one `connections[].id` a tool's domain names, or `""` if none does.

    UC-1's own catalogue ties every tool name to a domain prefix
    (`erp.*` → `conn_erp`, `mail.*` → `conn_mail`, `task.*` → `conn_task`) — a
    convention this function reads off the connection's own `type`/`id`
    rather than hard-coding the tool-name prefix, so a spec that names its
    connections differently from `agentspec.yaml`'s own example still
    resolves correctly.
    """
    domain = tool.split(".", 1)[0]
    for connection in spec.connections:
        if domain in connection.id:
            return connection.id
    return ""


def _system_prompt(spec: AgentSpec) -> str:
    """A minimal briefing for the agent con — capability names and the citation guardrail.

    Not the full prompt-engineering surface a production agent con needs
    (guardrail pipeline wiring is UC1-27, E11 — a separate ticket); enough
    for `spec.md`'s own worked example ("Khách nào quá hạn trên 30 ngày?") to
    produce a plausible tool call in a real run, and irrelevant to what a
    test's `ScriptedToolModel`-style fake reads at all.
    """
    tool_names = ", ".join(capability.tool for capability in spec.capabilities)
    return (
        f"Bạn là {spec.metadata.name}. Chỉ được dùng các tool: {tool_names}. "
        "Câu trả lời về số liệu phải có trích dẫn nguồn — dùng đúng giá trị "
        "'source' mà kết quả tool trả về, ví dụ 'nguồn: v_ar_open'."
    )


def _agent_node(model: BaseChatModel, spec: AgentSpec):
    """Returns the per-graph closure LangGraph will call as the `agent` node.

    No explicit `Callable[[AgentState], dict[str, Any]]` return annotation —
    `add_node`'s overloads are resolved against the closure's own inferred
    type, and an explicit `Callable[...]` alias does not structurally match
    them the way `src.workflow.runtime.compile_workflow_graph`'s equivalent
    (`_build_node`) already relies on being left unannotated.
    """
    system_message = SystemMessage(content=_system_prompt(spec))
    tool_specs = [_tool_spec(capability.tool, capability.scope.value) for capability in spec.capabilities]
    bound_model = model.bind_tools(tool_specs)
    deny_topics = spec.prohibitions.deny_topics
    fallback = spec.guardrails.fallback
    confidence_threshold = spec.guardrails.confidence_threshold

    def _node(state: AgentState) -> dict[str, Any]:
        started = time.monotonic()
        messages = state["messages"]

        blocked_by = _blocked_by_input_guardrails(messages, deny_topics=deny_topics)
        if blocked_by is not None:
            reply: AIMessage = AIMessage(content=guardrails.fallback_message(fallback))
        else:
            outbound_messages = _redact_last_human_message(messages)
            reply = repair_tool_call_arguments(bound_model.invoke([system_message, *outbound_messages]))
            if not reply.tool_calls:
                reply = _apply_output_guardrails(reply, confidence_threshold=confidence_threshold, fallback=fallback)

        latency_ms = (time.monotonic() - started) * 1000
        step: TranscriptStep = {
            "node": _AGENT_NODE,
            "tool": None,
            "args": None,
            "result": (f"guardrail:{blocked_by}: {reply.content}" if blocked_by is not None else str(reply.content))
            if reply.content
            else None,
            "tokens": _tokens_of(reply),
            "latency_ms": latency_ms,
            "cost_usd": 0.0 if blocked_by is not None else NOMINAL_MODEL_CALL_COST_USD,
            "row_filter": None,
        }
        return {"messages": [reply], "transcript": [step]}

    return _node


def _last_human_text(messages: Sequence[BaseMessage]) -> str | None:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return None


def _blocked_by_input_guardrails(messages: Sequence[BaseMessage], *, deny_topics: Sequence[str]) -> str | None:
    """`prompt_injection_detector`/`topic_classifier` (§11.1, input): the name of
    whichever one fires first against the turn's own latest human message, or
    `None`. Neither inspects a `ToolMessage` — that is §10.3's provenance
    tracker's job, not this pair's; these two only ever read what the *user*
    just typed.
    """
    text = _last_human_text(messages)
    if text is None:
        return None
    if guardrails.detect_prompt_injection(text):
        return "prompt_injection_detector"
    topic = guardrails.classify_topic(text, deny_topics)
    if topic is not None:
        return f"topic_classifier:{topic}"
    return None


def _redact_last_human_message(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """`pii_redactor` (§11.1, input): the message list actually sent to the model,
    with the latest human message's PII shapes scrubbed out — FR's own "trước khi
    tới LLM". The `messages` graph state holds is never touched: this is a copy
    built for one `bound_model.invoke` call, not a state mutation.
    """
    outbound = list(messages)
    for index in range(len(outbound) - 1, -1, -1):
        message = outbound[index]
        if isinstance(message, HumanMessage):
            redaction = guardrails.redact_pii(str(message.content))
            if redaction.triggered:
                outbound[index] = HumanMessage(content=redaction.text)
            break
    return outbound


def _apply_output_guardrails(reply: AIMessage, *, confidence_threshold: float, fallback: Fallback) -> AIMessage:
    """`pii_masker`/`citation_required`/`confidence_router` (§11.1, output), run
    only on a tool-call-free reply — a turn that is not finished yet has no
    "answer shown to the user" for these three to hold to any standard.
    """
    confidence = (reply.response_metadata or {}).get("confidence")
    if guardrails.route_low_confidence(confidence, threshold=confidence_threshold):
        return AIMessage(content=guardrails.fallback_message(fallback))

    text = str(reply.content) if reply.content else ""
    masked = guardrails.mask_pii(text)
    result_text = masked.text if masked.triggered else text
    if result_text and not guardrails.citation_present(result_text):
        result_text = f"{result_text} (Không có trích dẫn nguồn — vui lòng xác minh lại)"
    if result_text == text:
        return reply
    return AIMessage(content=result_text, response_metadata=reply.response_metadata, usage_metadata=reply.usage_metadata)


def _tokens_of(message: AIMessage) -> int | None:
    """Real token usage when the model reported one (`ChatOpenAI` does); `None` for a fake that never set it."""
    usage = getattr(message, "usage_metadata", None)
    return usage.get("total_tokens") if usage else None


def _tool_spec(name: str, scope: str) -> dict[str, Any]:
    """An OpenAI-function-shaped tool spec — the wire format `bind_tools` expects from a real chat model.

    The description and parameter schema come from `TOOL_SCHEMAS`
    (`src.compiler.tools`), which lives beside the implementations that read
    those arguments — see that table for why the two cannot be allowed to drift
    apart again, and why its numeric fields are advertised as strings.

    A tool with no entry there still gets a spec, and it is still the old
    schema-loose one: a spec may name a capability this repo has no catalogue
    entry for, and refusing to describe it is not a reason to refuse to offer
    it. `scope` is folded into the description in that case only, since it is
    the single true thing left to say about the tool.
    """
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": f"UC-1 tool ({scope})",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
            },
        }
    return {
        "type": "function",
        "function": {"name": name, "description": schema["description"], "parameters": schema["parameters"]},
    }


def _route_after_agent(state: AgentState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return _TOOLS_NODE
    return END


def _route_after_tools(state: AgentState) -> str:
    return END if state["budget_exceeded"] else _AGENT_NODE


@dataclass(frozen=True, slots=True)
class _ToolCallStep:
    """One tool call's outcome, in the shape `_node` needs to fold into both the
    messages it returns and the transcript/provenance/session deltas it accumulates.
    """

    message: ToolMessage
    step: TranscriptStep
    provenance_update: ProvenanceState
    session_update: SessionState
    budget_exceeded: bool
    #: UC1-21 (#137): populated only on the `ApprovalRequiredError` branch — the
    #: `require_approval` decision, captured in the same shape `src.workflow.
    #: uc1_pending_actions.record_pending_actions` persists, for the caller
    #: (`src.api.sandbox_routes`, and eventually a production chat route) to
    #: write into `uc1_pending_actions` once it has a DB session in hand,
    #: which this pure graph closure deliberately never does.
    pending_action: PendingActionState | None = None


@dataclass(frozen=True, slots=True)
class _ToolsNodeConfig:
    """`_tools_node`'s own bundle — the "3 parameters" fix (`docs/code-style/python.md`
    §2) `ToolCallContext`/`CompileDependencies` already apply elsewhere in this module,
    applied here to the three values fixed for the whole compiled graph rather than
    varying per call.
    """

    policy_spec: AgentSpec
    pep_client: PEPClient
    audit_sink: AuditSink


@dataclass(frozen=True, slots=True)
class _CallInput:
    """`_run_one_call`'s own bundle, the same "3 parameters" fix as `_ToolsNodeConfig` —
    everything about *this* call within the turn that is not the call itself.
    `provenance`/`session` are the *live* accumulated values as of this specific call —
    not the state the node started in — which is what lets a second call in the same
    batch see the first one's provenance update (§10.3; see `_node`'s own docstring for
    why that distinction is load-bearing).
    """

    principal: Principal
    request_id: str
    provenance: ProvenanceState
    session: SessionState


def _tools_node(bound_tools: Mapping[str, BoundTool], config: _ToolsNodeConfig):
    """Returns the `tools` node closure — see `_agent_node` for why this is left unannotated."""

    def _run_one_call(call: Mapping[str, Any], turn: _CallInput) -> _ToolCallStep:
        """One `tool_calls` entry, PEP-gated."""
        bound = bound_tools.get(call["name"])
        if bound is None:
            # Not in `capabilities[]` — the model hallucinated a tool name (T9).
            # Answered as a tool error, never executed: there is no `BoundTool`
            # to run, so nothing past this point can call it.
            content = f"tool {call['name']!r} không nằm trong capabilities"
            return _ToolCallStep(
                message=ToolMessage(content=content, tool_call_id=call["id"]),
                step=_step(_StepInput(node=_TOOLS_NODE, tool=call["name"], args=call["args"], result=content, latency_ms=0.0)),
                provenance_update=EMPTY_PROVENANCE.copy(),
                session_update=EMPTY_SESSION.copy(),
                budget_exceeded=False,
            )

        started = time.monotonic()
        ctx = ToolCallContext(
            policy_spec=config.policy_spec,
            principal=turn.principal,
            provenance=turn.provenance,
            session=turn.session,
            pep_client=config.pep_client,
            audit_sink=config.audit_sink,
            request_id=turn.request_id,
        )
        try:
            outcome = execute_tool_call(bound, call["args"], ctx)
        except ToolDeniedError as exc:
            content = f"denied: {'; '.join(exc.reasons)}"
            return _ToolCallStep(
                message=ToolMessage(content=content, tool_call_id=call["id"], name=bound.name),
                step=_step(
                    _StepInput(node=_TOOLS_NODE, tool=bound.name, args=call["args"], result=content, latency_ms=_elapsed_ms(started))
                ),
                provenance_update=EMPTY_PROVENANCE.copy(),
                session_update=EMPTY_SESSION.copy(),
                budget_exceeded=any(BUDGET_DENIAL_MARKER in reason for reason in exc.reasons),
            )
        except ApprovalRequiredError as exc:
            content = f"require_approval: {exc.action_hash}"
            pending_action: PendingActionState = {
                "tool": bound.name,
                "args": dict(call["args"]),
                "action_hash": exc.action_hash,
                "connection_id": bound.connection_id,
                "approver_roles": list(exc.approver_roles),
            }
            return _ToolCallStep(
                message=ToolMessage(content=content, tool_call_id=call["id"], name=bound.name),
                step=_step(
                    _StepInput(node=_TOOLS_NODE, tool=bound.name, args=call["args"], result=content, latency_ms=_elapsed_ms(started))
                ),
                provenance_update=EMPTY_PROVENANCE.copy(),
                session_update=EMPTY_SESSION.copy(),
                budget_exceeded=False,
                pending_action=pending_action,
            )

        return _ToolCallStep(
            message=ToolMessage(content=str(outcome.result), tool_call_id=call["id"], name=bound.name),
            step=_step(
                _StepInput(
                    node=_TOOLS_NODE,
                    tool=bound.name,
                    args=call["args"],
                    result=str(outcome.result),
                    latency_ms=_elapsed_ms(started),
                    row_filter=outcome.row_filter,
                )
            ),
            provenance_update=outcome.provenance_update,
            session_update=outcome.session_update,
            budget_exceeded=False,
        )

    def _node(state: AgentState) -> dict[str, Any]:
        """Runs every tool call in this turn's `AIMessage`, in order.

        Two provenance/session values are tracked, not one, and conflating
        them is exactly the bug a code review caught here: `live_*` is what
        each `_run_one_call` sees, and it has to include *this pass's own*
        earlier calls — a model that calls a read tool and a write tool in
        the same `AIMessage` (parallel tool calls) must have the write see
        the read's provenance, or §10.3's "lan sang mọi tool call phát sinh
        sau đó cùng lượt" silently fails to hold within a single batch.
        `delta_*` is what gets *returned* to LangGraph's reducers
        (`merge_provenance`/`merge_session`, `src.compiler.state`) — those
        already fold in whatever `state` held coming into this node, so
        returning the live, already-merged value here would double it.
        """
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)  # noqa: S101 — only reachable via `_route_after_agent`'s own check

        messages: list[BaseMessage] = []
        transcript: list[TranscriptStep] = []
        live_provenance, live_session = state["provenance"], state["session"]
        delta_provenance, delta_session = EMPTY_PROVENANCE.copy(), EMPTY_SESSION.copy()
        budget_exceeded = False
        pending_actions: list[PendingActionState] = []
        for call in last.tool_calls:
            outcome = _run_one_call(
                call,
                _CallInput(
                    principal=state["principal"],
                    request_id=state["request_id"],
                    provenance=live_provenance,
                    session=live_session,
                ),
            )
            messages.append(outcome.message)
            transcript.append(outcome.step)
            live_provenance = merge_provenance(live_provenance, outcome.provenance_update)
            live_session = merge_session(live_session, outcome.session_update)
            delta_provenance = merge_provenance(delta_provenance, outcome.provenance_update)
            delta_session = merge_session(delta_session, outcome.session_update)
            budget_exceeded = budget_exceeded or outcome.budget_exceeded
            if outcome.pending_action is not None:
                pending_actions.append(outcome.pending_action)
        return {
            "messages": messages,
            "provenance": delta_provenance,
            "session": delta_session,
            "transcript": transcript,
            "budget_exceeded": budget_exceeded,
            "pending_actions": pending_actions,
        }

    return _node


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000


@dataclass(frozen=True, slots=True)
class _StepInput:
    """What `_step` needs to build one `tools`-node `TranscriptStep` — bundled per
    the same "3 parameters" rule (`docs/code-style/python.md` §2)
    `CompileDependencies`/`ToolCallContext` already apply, once `row_filter`
    (UC1-30 / #131) pushed this past three.
    """

    node: str
    tool: str
    args: Mapping[str, Any]
    result: str
    latency_ms: float
    row_filter: Mapping[str, Any] | None = None


def _step(step_input: _StepInput) -> TranscriptStep:
    return {
        "node": step_input.node,
        "tool": step_input.tool,
        "args": dict(step_input.args),
        "result": step_input.result,
        "tokens": None,
        "latency_ms": step_input.latency_ms,
        "cost_usd": 0.0,
        "row_filter": dict(step_input.row_filter) if step_input.row_filter is not None else {},
    }


@dataclass
class CompiledAgentRegistry:
    """Every compiled version, keyed by `(agent_id, version)` — §7's "versioned theo agent_id + version".

    Recompiling one version never touches another's entry: `register` writes
    under this call's own key, so a new draft's compile (v4) cannot reach
    back and mutate the `CompiledAgent` a currently-`deployed` v3 still
    serves traffic through. An in-process dict rather than a database table:
    a compiled graph holds a live `BaseChatModel`/checkpointer, neither of
    which survives a row.
    """

    _by_key: dict[tuple[str, int], CompiledAgent] = field(default_factory=dict)

    def register(self, compiled: CompiledAgent) -> CompiledAgent:
        self._by_key[(compiled.agent_id, compiled.version)] = compiled
        return compiled

    def get(self, agent_id: str, version: int) -> CompiledAgent | None:
        return self._by_key.get((agent_id, version))
