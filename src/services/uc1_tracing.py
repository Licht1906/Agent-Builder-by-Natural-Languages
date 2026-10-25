"""UC1-31 (#143), spec.md §14 — Langfuse trace + structured JSON log, the two
"operational observability" wires tickets.md groups into one ticket (as
opposed to UC1-32's `audit_log`, kept separate: "bằng chứng bắt buộc... nặng
ký hơn — an toàn/kiểm toán, không chỉ observability").

Two seams:

`log_decision` — one JSON line per PEP decision, meant to be called from the
exact place `src.policy.audit.record_decision` already is
(`execute_tool_call`, `src.compiler.tools`): tickets.md gives UC1-31 and
UC1-32 the identical "sau mỗi lời gọi PEP" wording for a reason — an audited
decision and a logged decision come from the same call, so wiring the second
one anywhere else risks the two drifting out of step over time. `request_id`
is what a line and the `audit_log` row it corresponds to share — see
`src.policy.audit`'s own docstring for why that id lives inside `payload`
rather than as a schema.sql column of its own.

`export_uc1_trace` — one Langfuse trace per turn, built from the turn's own
`transcript` (`src.compiler.state.TranscriptStep`) once `run_sandbox_turn`
(`src.compiler.sandbox`) has already produced it whole — not from a second
layer of instrumentation threaded through the graph's nodes, the same "a
trace is built from the run's own record, not a parallel one" argument
`src.services.tracing`'s own module docstring makes for V2, adapted to UC1's
synchronous `.invoke()`: a whole transcript arrives at once here, never as an
event stream, so there is only one step-by-step record of a turn, not two
that could disagree. Like V2's tracer, never load-bearing: every exception
this module can raise is caught before it reaches a caller, because a chat
turn's answer must never depend on Langfuse being reachable.

`pii_scrub_traces` (`spec.observability`, `Observability`'s own default
`True`) redacts every step's `args`/`result` with the same
`src.compiler.guardrails.redact_pii` the agent node already applies to a
user's own message, before any of it leaves this process for Langfuse — T11's
own mitigation ("`pii_scrub_traces: true`; Langfuse self-hosted").
"""

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from src.agentspec.spec import AgentSpec, Trace
from src.compiler import guardrails
from src.compiler.state import TranscriptStep
from src.config import get_settings
from src.policy.pep import AuthzRequest, Decision, Principal

logger = logging.getLogger("uc1.observability")

#: A separate, filterable logger name for the structured decision lines
#: themselves — `docker logs`/any log shipper can select on "uc1.audit" the
#: same way an operator would grep for a fixed prefix, without this module's
#: own warnings (Langfuse export failures) mixed in.
_decision_logger = logging.getLogger("uc1.audit")


def log_decision(request_id: str, request: AuthzRequest, decision: Decision) -> None:
    """§14's own minimal structured-log record: `request_id`/`agent_id`/
    `principal_id`/`tool`/`effect` — one JSON line, one call, no batching:
    a call site wiring this in has already committed to "every decision",
    so there is nothing to buffer that would not just delay the same line.
    """
    _decision_logger.info(
        json.dumps(
            {
                "request_id": request_id,
                "agent_id": request.spec.metadata.id,
                "principal_id": request.principal.employee_id,
                "tool": request.action.tool,
                "effect": decision.effect.value,
            },
            ensure_ascii=False,
        )
    )


_client: Any = None


def _langfuse_client() -> Any:
    """The process-wide Langfuse client, or `None` when tracing is not configured.

    Same shape as `src.services.tracing._langfuse_client` — re-implemented
    rather than imported, the same "re-declare, do not reach across the
    boundary" choice `src.compiler.sandbox.ThreadNamespace` already makes for
    its own, unrelated reason: UC1 and V2 are different pipelines (ADR-0046),
    and importing a private, underscore-named function across that boundary
    would read as a dependency this module does not actually want to carry if
    V2's tracer ever changes shape for reasons of its own.
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
        logger.warning("Không khởi tạo được Langfuse cho UC-1 — lượt chạy vẫn tiếp tục, chỉ là không có trace", exc_info=True)
        return None
    return _client


def _scrub(value: Any) -> Any:
    """`redact_pii` applied recursively — a tool call's `args`/`result` are
    JSON-shaped (dict/list/str/number/None), never a bare string the way a
    chat message is, so this walks the structure instead of assuming one.
    """
    if isinstance(value, str):
        return guardrails.redact_pii(value).text
    if isinstance(value, Mapping):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def export_uc1_trace(
    transcript: Sequence[TranscriptStep],
    *,
    spec: AgentSpec,
    principal: Principal,
    thread_id: str,
    request_id: str,
) -> None:
    """One Langfuse trace for `transcript`, or nothing at all when tracing is
    off — by `spec.observability` (a creator's own per-agent choice, `None`
    or `trace: none` both mean "not this agent") or by `settings` (an
    operator's platform-wide one, empty keys meaning "no backend
    configured", the same reading `src.services.tracing.get_tracer` gives
    them). Both have to say yes for anything to be exported.

    Never raises: every exception below is caught, exactly the "trace is
    never on the critical path" rule `src.services.tracing`'s module
    docstring states outright for V2, restated here because a chat turn
    that already completed successfully must not turn into a 500 on its way
    out because an observability backend hiccupped.
    """
    observability = spec.observability
    if observability is None or observability.trace is not Trace.LANGFUSE:
        return
    client = _langfuse_client()
    if client is None:
        return

    scrub_traces = observability.pii_scrub_traces
    try:
        from langfuse.types import TraceContext

        # Filed under the turn's own `request_id` — a 32-hex uuid, which is
        # what a trace id has to be — so the Langfuse trace and the turn's row
        # in `agent_runs` (ADR-0053, `trace_id=request_id`) are one key: the
        # PM board links straight from the row to this trace.
        root = client.start_observation(
            trace_context=TraceContext(trace_id=request_id),
            name=f"agent-{spec.metadata.id}-turn",
            as_type="agent",
            metadata={
                "agent_id": spec.metadata.id,
                "version": spec.metadata.version,
                "thread_id": thread_id,
                "request_id": request_id,
                "principal_id": principal.employee_id,
            },
        )
        for step in transcript:
            _export_step(root, step, scrub_traces=scrub_traces)
        root.end()
    except Exception:  # noqa: BLE001 — see module/function docstring
        logger.warning("Không xuất được trace UC-1 sang Langfuse — lượt chạy vẫn tiếp tục", exc_info=True)


def _export_step(root: Any, step: TranscriptStep, *, scrub_traces: bool) -> None:
    args = step["args"]
    result = step["result"]
    if scrub_traces:
        args = _scrub(args) if args is not None else args
        result = _scrub(result) if result is not None else result
    observation = root.start_observation(
        name=step["tool"] or step["node"],
        as_type="tool" if step["tool"] else "chain",
        input=args,
        metadata={"node": step["node"], "row_filter": step["row_filter"]},
    )
    observation.update(
        output=result,
        metadata={"tokens": step["tokens"], "latency_ms": step["latency_ms"], "cost_usd": step["cost_usd"]},
    )
    observation.end()
