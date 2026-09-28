"""Operational tracing — every run, trial or production, recoverable afterwards.

See issue #11 / ADR-0009. When an agent misbehaves in production, the question is
always the same: *what did it actually do?* The event stream answers it while a
run is happening (#8) and vanishes when the client closes the tab. This module is
where that answer is kept.

Three decisions shape it.

**A trace is built from the run's own event stream**, not from a second layer of
instrumentation threaded through the runtime. The stream already carries every
step and every tool call in the order they happened, and it is the same stream
the chat window renders — so what an investigator reads later cannot disagree
with what the user saw at the time. A parallel set of callbacks would eventually
disagree, and there would be no way to tell which was lying.

**Tracing is never on the critical path.** A run must not fail, stall, or change
its answer because an observability backend is down; the ticket says so outright
(*"trace không phải phụ thuộc chặn đường"*). The guard lives at the caller in
`src.agents.runtime`, which wraps every call into a trace, so a `RunTrace`
implementation is free to be as broken as its backend is.

**Langfuse is behind a seam, like the chat model is.** `get_tracer` is a FastAPI
dependency, so tests replace it the way they replace `get_chat_model_factory` —
and the span-building logic they assert against is `EventTrace` below, the same
code the Langfuse path runs. Without that, tests would pin down a shape nothing
in production produces.
"""

import logging
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.agents.costs import CostFigure
from src.agents.threads import RunIdentity
from src.config import get_settings

logger = logging.getLogger(__name__)

#: Event names this module understands, mirrored from `src.agents.runtime` (V1)
#: and `src.workflow.runtime` (V2). Duplicated rather than imported: both
#: runtimes import *this* module, and the dependency has to point one way. They
#: are the SSE names on the wire, so a change to them is a contract change that
#: breaks a browser first — this is not a constant that can drift quietly.
#: `token` and `usage` are absent on purpose: they open a unit's span like any
#: other event and are then dropped. One span per fragment of an answer would
#: bury the run.
_TOOL, _TOOL_RESULT, _STEP, _BUDGET_EXCEEDED = "tool", "tool_result", "step", "budget_exceeded"
#: V2's three words for the same structure. A V1 run is steps (`step_id`, closed
#: by `step`); a V2 run is nodes (`node_id`, opened by `node_started`, closed by
#: `node`), and a leg can end at `awaiting_approval` — paused, not finished.
#: `src.workflow.runtime`'s own docstring says "what is *not* shared is the
#: event vocabulary"; this module is where the two are folded into one tree,
#: so the Langfuse export and the PM ledger read the same shape for both.
_NODE_STARTED, _NODE, _AWAITING_APPROVAL = "node_started", "node", "awaiting_approval"

#: How a V2 node type is labelled in Langfuse. The SDK's own observation types
#: (`start_observation(as_type=...)`), chosen so the trace viewer shows a model
#: call as a generation and a search as a retrieval instead of a wall of
#: identical chains. Anything not listed — `start`, `end`, `if_else`, `while`,
#: `collect`, `mcp` (whose real work is the `tool` span inside it) — is a chain.
_NODE_OBSERVATION_TYPES: Mapping[str, str] = {
    "agent_llm": "generation",
    "knowledge_search": "retriever",
    "web_search": "retriever",
    "human_approval": "guardrail",
}


@dataclass(slots=True)
class ToolObservation:
    """One tool call, from the moment it was decided on to whatever came back.

    `simulated` travels with it because a trace of a trial run is only useful if
    it distinguishes what the agent *did* from what it *would* have done — the
    same distinction the sandbox transcript exists to make (#9).
    """

    tool: str
    args: dict[str, Any]
    simulated: bool
    started_at: float
    ok: bool | None = None
    result: str | None = None
    ended_at: float | None = None
    #: Opaque to this module: whatever the exporter needs to find its own span
    #: again when the call finishes. Kept on the observation rather than in a
    #: side table so the two can never be matched up wrongly.
    handle: Any = None


@dataclass(slots=True)
class StepObservation:
    """One unit of the run — a V1 step or a V2 node — and every tool it reached for.

    `step_id` keeps its name for V1's sake and holds the `node_id` of a V2 node;
    the two never mix inside one run, and a reader of either runtime's trace
    finds the id they know. The last two fields are V2-only and `None` on a V1
    step: `node_type` is what kind of node this was, `port_taken` which branch
    the run left it by (ADR-0026 §3 — written at the routing decision, never
    re-derived from the edges).
    """

    step_id: str
    started_at: float
    title: str | None = None
    output: str | None = None
    ended_at: float | None = None
    tools: list[ToolObservation] = field(default_factory=list)
    handle: Any = None
    node_type: str | None = None
    port_taken: str | None = None


@dataclass(slots=True)
class RecordedRun:
    """A finished run as the trace saw it — what an investigator gets to read.

    `stopped_by_budget` is separate from `error` for the reason ADR-0008 §3 gave
    the event names: a run that was stopped on purpose and a run that broke are
    different findings, and an investigator who cannot tell them apart cannot
    tell an expensive agent from a broken one. `paused_at` is the third ending,
    V2's: the leg stopped at a `HumanApproval` node and a later leg under the
    same `trace_id` carries on (ADR-0024) — neither broken nor done.
    """

    identity: RunIdentity
    steps: list[StepObservation]
    cost: CostFigure | None = None
    error: str | None = None
    stopped_by_budget: str | None = None
    paused_at: str | None = None


class RunTrace(Protocol):
    """Where one run reports itself. Implementations may fail freely — see the module docstring."""

    def record(self, name: str, data: Mapping[str, Any]) -> None:
        """Take in one event of the run, exactly as the client received it."""

    def finish(self, *, cost: CostFigure, error: str | None) -> None:
        """Close the run out. Called once, on every path, including a run that crashed."""


#: How a caller obtains a trace for the run it is about to start.
Tracer = Callable[[RunIdentity], RunTrace]


class NullTrace:
    """The trace of a run nobody is recording.

    What `get_tracer` returns when no backend is configured — which is the normal
    state in tests and local development, and a perfectly legitimate one in
    production. Doing nothing here is what makes "tracing is optional" true at
    the cheapest possible price, rather than a series of `if enabled` checks
    scattered through the runtime.
    """

    def record(self, name: str, data: Mapping[str, Any]) -> None:
        """Discard the event."""

    def finish(self, *, cost: CostFigure, error: str | None) -> None:
        """Discard the run."""


class EventTrace:
    """Turns a run's event stream back into the shape it was executed in.

    The stream is flat and ordered; a run is units — V1 steps or V2 nodes — each
    containing tool calls. This class is the one place that inversion happens,
    so the Langfuse export and anything else that ever wants a run's structure
    agree on what a unit is.

    A V1 step is opened by the first event that mentions its `step_id` and closed
    by its own `step` event; a V2 node is opened by `node_started` and closed by
    `node`, both carrying `node_id`. Either works because each runtime executes
    one unit at a time (ADR-0003, ADR-0031) — the stream never interleaves two
    of them. `node_started` always opens afresh, even for an id already seen:
    a `While` body re-enters the same nodes, and each pass is its own unit with
    its own duration (ADR-0026 §4 — "successive passes are genuinely different
    facts"). A `budget_exceeded` event names a unit that never ran, so it opens
    nothing: it is the run's ending, not a unit of it. So is `awaiting_approval`.

    Subclasses export by overriding the four hooks. They are called as the run
    happens rather than in a batch at the end, so a backend that records
    durations records the real ones.
    """

    def __init__(self, identity: RunIdentity) -> None:
        self.identity = identity
        self.steps: list[StepObservation] = []
        self._open_step: StepObservation | None = None
        self._open_tools: dict[str, ToolObservation] = {}
        self._stopped_by_budget: str | None = None
        self._paused_at: str | None = None
        self.run_started()

    def record(self, name: str, data: Mapping[str, Any]) -> None:
        """Fold one event into the run's structure."""
        if name == _BUDGET_EXCEEDED:
            # Read off the event rather than waiting to be told at `finish`: the
            # stop already travels with its own explanation (#10), and a second
            # channel for the same fact is a second wording of it.
            self._stopped_by_budget = str(data.get("message", ""))
            return
        if name == _AWAITING_APPROVAL:
            # The leg ends here; `finish` follows. The node that paused is the
            # one already open (it announced itself with `node_started`), so
            # nothing to open — only the fact that the run is waiting.
            self._paused_at = str(data.get("node_id", ""))
            return
        unit_id = str(data.get("step_id") or data.get("node_id") or "")
        if name == _NODE_STARTED:
            self._open_unit(unit_id, node_type=str(data.get("node_type", "")) or None, fresh=True)
            return
        if unit_id:
            self._open_unit(unit_id, node_type=str(data.get("node_type", "")) or None)
        if name == _TOOL:
            self._start_tool(unit_id, data)
        elif name == _TOOL_RESULT:
            self._end_tool(unit_id, data)
        elif name == _STEP:
            self._end_unit(title=str(data.get("title", "")), output=str(data.get("content", "")))
        elif name == _NODE:
            self._end_unit(
                title=str(data.get("node_type", "")) or None,
                output=str(data.get("content", "")),
                port_taken=data.get("port_taken"),
            )
        # `token` and `usage` need no clause: they exist only to open the unit
        # above. A span per fragment of an answer would bury the run it was
        # meant to explain.

    def finish(self, *, cost: CostFigure, error: str | None) -> None:
        """Close whatever is still open and hand the finished run to the exporter.

        A unit or a tool can still be open here: that is exactly what a run cut
        off by its budget, paused for a person, or ended by a crash looks like —
        and the unfinished span is the most informative thing in the trace,
        because it is where the run was when it stopped.
        """
        for tool in self._open_tools.values():
            self.tool_ended(tool)
        self._open_tools.clear()
        if self._open_step is not None:
            self.step_ended(self._open_step)
            self._open_step = None
        self.run_ended(
            RecordedRun(
                identity=self.identity,
                steps=self.steps,
                cost=cost,
                error=error,
                stopped_by_budget=self._stopped_by_budget,
                paused_at=self._paused_at,
            )
        )

    # -- structure ---------------------------------------------------------- #

    def _open_unit(self, unit_id: str, *, node_type: str | None = None, fresh: bool = False) -> None:
        """Make `unit_id` the open unit, closing the previous one if it is still open.

        `fresh` is `node_started`'s word: open a new unit even if one with this
        id is already open, because the runtime said a new pass began. Every
        other event only makes sure *some* unit with that id is open.
        """
        current = self._open_step
        if current is not None and current.step_id == unit_id and not fresh:
            if node_type and current.node_type is None:
                current.node_type = node_type
            return
        if current is not None:
            self.step_ended(current)
        step = StepObservation(step_id=unit_id, started_at=time.time(), node_type=node_type)
        self.steps.append(step)
        self._open_step = step
        self.step_started(step)

    def _start_tool(self, unit_id: str, data: Mapping[str, Any]) -> None:
        tool = ToolObservation(
            tool=str(data["tool"]),
            args=dict(data.get("args") or {}),
            simulated=bool(data.get("simulated")),
            started_at=time.time(),
        )
        if self._open_step is not None:
            self._open_step.tools.append(tool)
        # Keyed by tool name because that is all a `tool_result` event carries to
        # match on, and a unit's calls to one tool are sequential — both runtimes
        # run them one at a time (`_run_step_tool_loop`, `_run_mcp`).
        self._open_tools[f"{unit_id}:{tool.tool}"] = tool
        self.tool_started(tool)

    def _end_tool(self, unit_id: str, data: Mapping[str, Any]) -> None:
        tool = self._open_tools.pop(f"{unit_id}:{data['tool']}", None)
        if tool is None:
            return
        tool.ok = bool(data.get("ok"))
        tool.result = str(data.get("result", ""))
        tool.ended_at = time.time()
        self.tool_ended(tool)

    def _end_unit(self, *, title: str | None, output: str, port_taken: Any = None) -> None:
        step = self._open_step
        if step is None:
            return
        step.title = title
        step.output = output
        if port_taken is not None:
            step.port_taken = str(port_taken)
        step.ended_at = time.time()
        self.step_ended(step)
        self._open_step = None

    # -- hooks -------------------------------------------------------------- #

    def run_started(self) -> None:
        """The run is about to produce events."""

    def step_started(self, step: StepObservation) -> None:
        """A step has begun."""

    def step_ended(self, step: StepObservation) -> None:
        """A step has finished, or the run ended while it was still going."""

    def tool_started(self, tool: ToolObservation) -> None:
        """A tool call has been announced, before it runs."""

    def tool_ended(self, tool: ToolObservation) -> None:
        """A tool call came back, or the run ended before it did."""

    def run_ended(self, run: RecordedRun) -> None:
        """The run is over, however it ended."""


class LangfuseTrace(EventTrace):
    """`EventTrace` exported to Langfuse as one trace per run.

    Nesting is explicit (`parent.start_observation(...)`) rather than through
    Langfuse's context-manager API: a run's events arrive on an async stream that
    suspends between them, so "the currently active span" is not a well-defined
    thing to hang a tool call off.

    Nothing here calls `flush`. The client batches on its own background thread,
    and blocking a user's chat request to push a span is precisely the dependency
    this ticket rules out. The batch is flushed at process shutdown instead
    (`shutdown_tracing`).
    """

    def __init__(self, identity: RunIdentity, client: Any) -> None:
        self._client = client
        self._root: Any = None
        super().__init__(identity)

    def run_started(self) -> None:
        from langfuse.types import TraceContext

        thread = self.identity.thread
        # Trace-level identity, so the Langfuse UI's own group-bys work without
        # anyone parsing metadata: `session_id` groups every run of one
        # conversation, `user_id` answers "whose runs" — the two questions a PM
        # asks before any custom dashboard exists.
        identity = {
            "session_id": f"agent-{thread.agent_id}-{thread.conversation_id}",
            "user_id": thread.caller or f"agent-{thread.agent_id}-owner",
            "tags": [thread.namespace.value],
        }
        with _propagating(identity):
            self._root = self._client.start_observation(
                trace_context=TraceContext(trace_id=self.identity.trace_id),
                name=f"agent-{thread.agent_id}-run",
                as_type="agent",
                metadata={
                    "agent_id": thread.agent_id,
                    "version_id": self.identity.version_id,
                    "conversation_id": thread.conversation_id,
                    # The one attribute an investigator filters on first: a
                    # simulated run and a real one look identical otherwise, and
                    # confusing them is how a trial gets read as a production
                    # incident.
                    "namespace": thread.namespace.value,
                },
            )
        # SDK < 4 set the same three on the root span after the fact; the
        # `hasattr` guard is what lets either SDK run without tracing ever
        # breaking a chat (§6 of the ADR).
        if hasattr(self._root, "update_trace"):
            self._root.update_trace(**identity)

    def step_started(self, step: StepObservation) -> None:
        if self._root is not None:
            step.handle = self._root.start_observation(
                name=step.step_id,
                as_type=_NODE_OBSERVATION_TYPES.get(step.node_type or "", "chain"),
            )

    def step_ended(self, step: StepObservation) -> None:
        if step.handle is None:
            return
        # A V1 step is named by its title; a V2 node keeps its id and says what
        # kind of node it was beside it, so "answer · agent_llm" reads on the
        # span list the way it reads on the canvas.
        name = step.step_id if step.node_type else (step.title or step.step_id)
        metadata = {k: v for k, v in (("node_type", step.node_type), ("port_taken", step.port_taken)) if v}
        step.handle.update(name=name, output=step.output, metadata=metadata or None)
        step.handle.end()

    def tool_started(self, tool: ToolObservation) -> None:
        parent = self._open_step.handle if self._open_step is not None else self._root
        if parent is not None:
            tool.handle = parent.start_observation(name=tool.tool, as_type="tool", input=tool.args)

    def tool_ended(self, tool: ToolObservation) -> None:
        if tool.handle is None:
            return
        tool.handle.update(
            output=tool.result,
            level="WARNING" if tool.ok is False else None,
            metadata={"simulated": tool.simulated},
        )
        tool.handle.end()

    def run_ended(self, run: RecordedRun) -> None:
        if self._root is None:
            return
        cost = run.cost
        self._root.update(
            output=run.error
            or run.stopped_by_budget
            or (f"awaiting approval at {run.paused_at}" if run.paused_at else None),
            level="ERROR" if run.error else None,
            metadata={
                "total_tokens": cost.usage.total_tokens if cost else 0,
                "cost_usd": cost.usd if cost else 0.0,
                "latency_seconds": cost.seconds if cost else None,
                # One machine-readable word for how the run ended, so "success
                # rate by version" is a group-by rather than string-parsing the
                # output field. Four values because four endings are what the
                # trace can distinguish; the finer split the PM board shows
                # (spec_error vs system_error, the trace codes) is decided by
                # `src.pm.taxonomy` on the ledger row, not here — the ledger is
                # the board's source, this is the investigator's.
                "outcome": _outcome_of(run),
            },
        )
        self._root.end()


def _propagating(identity: Mapping[str, Any]) -> Any:
    """The SDK's way of stamping trace-level identity on the spans opened inside it.

    Langfuse SDK 4 dropped `update_trace` for `propagate_attributes`, a context
    manager that writes `user_id`/`session_id`/`tags` onto every span created
    within it — the root span is enough for the trace to carry them. Wrapped
    here so an SDK without it (3.x, which still has `update_trace`) degrades
    to doing nothing rather than to an ImportError inside a chat request.
    """
    try:
        from langfuse import propagate_attributes
    except ImportError:
        return nullcontext()
    return propagate_attributes(**identity)


def _outcome_of(run: RecordedRun) -> str:
    """How the run ended, as one filterable word.

    Order matters: a run the budget stopped often also carries an error-shaped
    message, and "we stopped it on purpose" is the truer of the two facts —
    the same distinction `RecordedRun` keeps the fields separate for. A paused
    leg is neither: its trace stays open for the leg that resumes it.
    """
    if run.stopped_by_budget:
        return "budget_stopped"
    if run.error:
        return "error"
    if run.paused_at:
        return "awaiting_approval"
    return "success"


_client: Any = None


def _langfuse_client() -> Any:
    """The process-wide Langfuse client, or `None` when tracing is not configured.

    One client for the process, built on first use: it owns a background thread
    and an HTTP connection pool, and one per request would cost more than the
    tracing is worth.

    Construction is guarded because it reaches the network to validate its
    credentials. A misconfigured key must degrade to "no tracing", never to "no
    chat" — the whole point of §6 of the ADR.
    """
    global _client
    if _client is not None:
        return _client
    settings = get_settings()
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception:  # noqa: BLE001 — an unreachable trace backend is not an outage
        logger.warning("Không khởi tạo được Langfuse — lần chạy vẫn tiếp tục, chỉ là không có trace", exc_info=True)
        return None
    return _client


def untraced(identity: RunIdentity) -> RunTrace:
    """A `Tracer` that records nothing, for a deployment with no trace backend."""
    return NullTrace()


def trace_url_template() -> str | None:
    """Where a trace can be opened, with `{trace_id}` left to fill in — or `None`.

    The one piece of Langfuse the PM board needs (ADR-0053 §4): a run's row in
    `agent_runs` carries the same `trace_id` the exporter filed the trace
    under, so the link is string substitution, not a lookup. `None` when
    tracing is off or the project is not named — the board then shows no
    link rather than one that 404s.
    """
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key and settings.langfuse_project_id):
        return None
    return f"{settings.langfuse_host.rstrip('/')}/project/{settings.langfuse_project_id}/traces/{{trace_id}}"


def open_trace(tracer: Tracer, identity: RunIdentity) -> RunTrace:
    """Start a trace for `identity`, degrading to an untraced run if that fails.

    The guard the run's own event loop cannot provide. `src.agents.runtime._trace`
    protects every `record` and `finish`, but by then the trace object exists —
    and building one is itself a call into the backend (`LangfuseTrace` opens the
    root span in its constructor, which is where a dead host or a rejected key
    surfaces). Unguarded, that would be an HTTP 500 on a chat request because an
    observability service was down: precisely the dependency #11 rules out,
    reintroduced one frame above where it was ruled out.

    Callers get a working `RunTrace` unconditionally, so nothing downstream
    branches on whether tracing came up.
    """
    try:
        return tracer(identity)
    except Exception:  # noqa: BLE001 — a trace that will not start is not an outage
        logger.warning("Không mở được trace cho lần chạy — chạy tiếp không có trace", exc_info=True)
        return NullTrace()


def get_tracer() -> Tracer:
    """FastAPI dependency: how a run gets somewhere to report itself.

    Returns a factory rather than a trace, for the same reason
    `get_chat_model_factory` does: what the trace is *about* is a per-run fact
    the dependency system cannot know.
    """
    client = _langfuse_client()
    if client is None:
        return untraced
    return lambda identity: LangfuseTrace(identity, client)


async def shutdown_tracing() -> None:
    """Push whatever is still batched before the process goes away.

    The only place `flush` is called. During a request it would put an HTTP round
    trip to a third party in front of a user's answer; at shutdown there is no
    user left to wait.
    """
    global _client
    if _client is None:
        return
    try:
        _client.flush()
    except Exception:  # noqa: BLE001 — losing the last batch is not worth a failed shutdown
        logger.warning("Không gửi được batch trace cuối cùng", exc_info=True)
    finally:
        _client = None
