"""UC1-11 (#125), §10.3 — the graph state `provenance` lives in.

`from_untrusted_data` is the whole mechanism T2 (prompt injection defence)
depends on: once a read tool call sets it, the merge reducer below is what
keeps it set for the rest of the turn, "lan sang mọi tool call phát sinh sau
đó cùng lượt" (§10.3) — an OR across every node's state update, never an
overwrite. If a later node's update carried `False` and this merged it by
replacement instead of OR, one more read call after the first would erase the
flag a write call three steps later depends on — exactly the silent failure
mode §10.3's closing line warns about ("nếu provenance tracker không được cài
trong LangGraph state, rule chặn injection sẽ không bao giờ bắn").

`session` exists for the same reason `authz.rego`'s own `input.session` does:
`max_tool_calls_per_session`/`per_tool_calls` are budget counters the PEP
reads on every call (UC1-10 §7, FR-39), so the graph has to accumulate them
across nodes exactly as faithfully as it accumulates provenance.
"""

import uuid
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from src.policy.pep import Principal


class ProvenanceState(TypedDict):
    from_untrusted_data: bool
    sources: list[str]


class SessionState(TypedDict):
    tool_calls_used: int
    per_tool_calls: dict[str, int]


class TranscriptStep(TypedDict):
    """One row of UC1-38's (#126) sandbox transcript — FR-37's own list:
    "node/tool nào chạy, input/output, token, latency, chi phí ước tính".

    Emitted directly by the node that ran, at the moment it ran — never
    reconstructed after the fact by re-parsing `messages` — so `args`/
    `latency_ms` are the exact values that node saw and measured, not a
    guess recovered from a `ToolMessage`'s string content.
    """

    node: str
    tool: str | None
    args: dict[str, Any] | None
    result: str | None
    tokens: int | None
    latency_ms: float
    cost_usd: float
    #: UC1-30 (#131) §11.2's "phân quyền chéo": the PEP's own `row_filter` for a
    #: successful `scope: read` call, read straight off this transcript rather than
    #: re-derived — `None` for an agent-node step, `{}` for a call that never got an
    #: `allow` decision (denied, needs approval, or hallucinated a tool name).
    row_filter: dict[str, Any] | None


class PendingActionState(TypedDict):
    """UC1-21 (#137), FR-42/FR-43: one `require_approval` decision the tools node
    hit this turn, in exactly the shape `src.workflow.uc1_pending_actions.
    record_pending_actions` needs to insert a `UC1PendingAction` row —
    captured *in the graph*, at the moment `ApprovalRequiredError` is caught,
    rather than reconstructed later by parsing a `ToolMessage`'s string
    content, the same reasoning `TranscriptStep`'s own docstring gives for
    reading fields off the node that ran instead of off `messages`.
    """

    tool: str
    args: dict[str, Any]
    action_hash: str
    connection_id: str
    approver_roles: list[str]


def append_pending_actions(left: list[PendingActionState], right: list[PendingActionState]) -> list[PendingActionState]:
    """Concatenation, like `append_transcript` — a turn's queue writes are a log, not a slot."""
    return [*left, *right]


def merge_provenance(left: ProvenanceState, right: ProvenanceState) -> ProvenanceState:
    """OR the flag, concatenate the sources. Never lets a later `False` clear an earlier `True`.

    Reads with `.get(..., default)` rather than indexing: LangGraph seeds a
    `BinaryOperatorAggregate` channel by calling this reducer once against the
    channel's own bare `{}` bootstrap value *before* `initial_state`'s value
    is folded in — `left` on that first call is not one of this module's own
    dicts, so it cannot be trusted to hold every key.
    """
    return {
        "from_untrusted_data": left.get("from_untrusted_data", False) or right.get("from_untrusted_data", False),
        "sources": [*left.get("sources", []), *right.get("sources", [])],
    }


def merge_session(left: SessionState, right: SessionState) -> SessionState:
    per_tool_calls = dict(left.get("per_tool_calls", {}))
    for tool, count in right.get("per_tool_calls", {}).items():
        per_tool_calls[tool] = per_tool_calls.get(tool, 0) + count
    return {
        "tool_calls_used": left.get("tool_calls_used", 0) + right.get("tool_calls_used", 0),
        "per_tool_calls": per_tool_calls,
    }


def append_transcript(left: list[TranscriptStep], right: list[TranscriptStep]) -> list[TranscriptStep]:
    """Concatenation, never replacement — a transcript is a log, and a log that overwrote
    itself each super-step would show only the last node that ran.
    """
    return [*left, *right]


EMPTY_PROVENANCE: ProvenanceState = {"from_untrusted_data": False, "sources": []}
EMPTY_SESSION: SessionState = {"tool_calls_used": 0, "per_tool_calls": {}}


class AgentState(TypedDict):
    """One turn's working memory. `principal` is set once, at the start of a run,
    and never updated mid-graph — delegated authority means every tool call
    within *this* run asks the PEP on behalf of the same caller who opened it
    (§10.1); it is not annotated with a reducer because nothing after the
    first node ever writes to it again.

    `budget_exceeded` is a plain overwrite too, but for the opposite reason:
    every tools-node pass sets it fresh (`True`/`False`) from *this* pass's
    own denials only — it answers "should this turn stop right now", not
    "has this thread ever hit the ceiling", so it must never accumulate.
    """

    messages: Annotated[list[BaseMessage], add_messages]
    principal: Principal
    #: UC1-31 (#143): one id per turn, never per tool call — set once here,
    #: read by every `_run_one_call` this turn makes (`src.compiler.graph`),
    #: which is what lets UC1-31's structured log lines and UC1-32's
    #: `audit_log` rows for the *same* turn join on the same value even
    #: though a turn can make several PEP calls. Not annotated with a
    #: reducer, for the same reason `principal` is not: nothing after the
    #: first node ever writes to it again.
    request_id: str
    provenance: Annotated[ProvenanceState, merge_provenance]
    session: Annotated[SessionState, merge_session]
    transcript: Annotated[list[TranscriptStep], append_transcript]
    budget_exceeded: bool
    pending_actions: Annotated[list[PendingActionState], append_pending_actions]


def initial_state(messages: list[BaseMessage], *, principal: Principal, request_id: str | None = None) -> AgentState:
    return {
        "messages": messages,
        "principal": principal,
        "request_id": request_id or uuid.uuid4().hex,
        "provenance": EMPTY_PROVENANCE.copy(),
        "session": EMPTY_SESSION.copy(),
        "transcript": [],
        "budget_exceeded": False,
        "pending_actions": [],
    }
