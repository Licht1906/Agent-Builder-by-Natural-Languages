"""UC1-38 (#126) — sandbox chat: FR-36/FR-37/FR-39.

Runs one turn through a `CompiledAgent`'s graph on a `sandbox:`-namespaced
thread and turns the graph's own `transcript` state (UC1-10 / #124) straight
into what FR-37 asks a sandbox run to record — no second bookkeeping layer
re-deriving what already happened inside the graph.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from src.compiler.graph import BUDGET_DENIAL_MARKER, CompiledAgent
from src.compiler.state import PendingActionState, TranscriptStep, initial_state
from src.policy.pep import Principal


class ThreadNamespace(StrEnum):
    """FR-36's own two worlds — "thread tách biệt production" — kept apart the
    same way `src.agents.threads.ThreadNamespace` already keeps V2's trial
    runs apart from V2's production ones, just re-declared here rather than
    imported: that enum's `TRIAL`/`PRODUCTION` values ("trial"/"prod") do not
    match the exact `sandbox:`/`prod:` prefixes spec.md names, and its
    `ThreadRef.agent_id` is `int` (a V2 `agents.id` FK) where UC-1's is the
    spec's own string id.
    """

    SANDBOX = "sandbox"
    PRODUCTION = "prod"


def sandbox_thread_id(agent_id: str, version: int, conversation_id: str) -> str:
    """FR-36: a checkpointer thread id namespaced apart from any production thread for the same agent."""
    return f"{ThreadNamespace.SANDBOX.value}:{agent_id}:{version}:{conversation_id}"


def production_thread_id(agent_id: str, version: int, conversation_id: str) -> str:
    """FR-36's other half: the deployed-agent counterpart to `sandbox_thread_id`, same shape,
    `prod:` namespace — so a sandbox rehearsal and a real caller talking to the same
    deployed version never land on the same checkpointer thread.
    """
    return f"{ThreadNamespace.PRODUCTION.value}:{agent_id}:{version}:{conversation_id}"


class SandboxRunStatus(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXCEEDED = "budget_exceeded"


@dataclass(frozen=True, slots=True)
class SandboxTurnResult:
    """What one `run_sandbox_turn` call produced — everything
    `src.api.sandbox_routes` needs to stream to the client and persist as a
    `UC1SandboxRun` row.
    """

    thread_id: str
    status: SandboxRunStatus
    transcript: list[TranscriptStep]
    total_cost_usd: float
    final_answer: str | None
    budget_error: dict[str, Any] | None
    #: UC1-21 (#137): every `require_approval` decision this turn hit, for
    #: `src.api.sandbox_routes` to persist as `uc1_pending_actions` rows —
    #: `run_sandbox_turn` itself never touches a DB session.
    pending_actions: list[PendingActionState]
    #: UC1-31 (#143): the same id every PEP call this turn made carried into
    #: `audit_log`/the structured log (`src.policy.audit`/
    #: `src.services.uc1_tracing`) — handed back so a caller can export this
    #: turn's own Langfuse trace under the id its rows already join by,
    #: rather than mint a second, unrelated one.
    request_id: str


def run_sandbox_turn(
    compiled: CompiledAgent, *, message: str, principal: Principal, thread_id: str
) -> SandboxTurnResult:
    """FR-36/37/39: one chat turn, on `thread_id`, budget-enforced exactly as production would be.

    `turn_input` is a *full* `initial_state`, not a bare `{"messages": ...}` —
    on a brand-new thread there is no checkpoint yet, so `provenance`/
    `session`/`transcript` would otherwise start from each channel's own bare
    bootstrap value rather than this module's `EMPTY_*` shapes. That is safe
    to do on every turn, not only the first: merging `EMPTY_PROVENANCE`/
    `EMPTY_SESSION` into whatever the checkpointer already restored is a
    no-op (OR with `False`, `+0`), so a *continuing* thread accumulates
    exactly as before — this is what makes `authority.budget` a *session*
    ceiling across turns on one thread rather than a per-message one that
    resets every call.
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    turn_input = initial_state([HumanMessage(content=message)], principal=principal)
    final_state = compiled.graph.invoke(turn_input, config=config)

    transcript: list[TranscriptStep] = final_state["transcript"]
    total_cost = sum(step["cost_usd"] for step in transcript)
    final_answer = _final_answer_of(final_state["messages"])
    pending_actions: list[PendingActionState] = final_state["pending_actions"]

    if final_state["budget_exceeded"]:
        budget_step = next(
            step
            for step in reversed(transcript)
            if step["node"] == "tools" and step["result"] and BUDGET_DENIAL_MARKER in step["result"]
        )
        return SandboxTurnResult(
            thread_id=thread_id,
            status=SandboxRunStatus.BUDGET_EXCEEDED,
            transcript=transcript,
            total_cost_usd=total_cost,
            final_answer=None,
            budget_error={"code": "budget_exceeded", "tool": budget_step["tool"], "detail": budget_step["result"]},
            pending_actions=pending_actions,
            request_id=final_state["request_id"],
        )
    return SandboxTurnResult(
        thread_id=thread_id,
        status=SandboxRunStatus.COMPLETED,
        transcript=transcript,
        total_cost_usd=total_cost,
        final_answer=final_answer,
        pending_actions=pending_actions,
        budget_error=None,
        request_id=final_state["request_id"],
    )


def _final_answer_of(messages: list[Any]) -> str | None:
    last = messages[-1]
    if isinstance(last, AIMessage) and not last.tool_calls:
        return str(last.content) if last.content else None
    return None
