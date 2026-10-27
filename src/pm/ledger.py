"""Writing a run into the shared ledger — `agent_runs` — from any pipeline. ADR-0053 §1, §3.

One writer, three doors. A V2 production leg arrives through
`src.api.agent_routes.RunLedgerEntry.bank`, a V2 trial through
`src.agents.sandbox.store_sandbox_run`, a UC-1 turn through
`src.api.uc1_agent_chat_routes._chat_stream`; each builds an `AgentRunRecord`
with the helper that knows its vocabulary (`v2_record`, `uc1_record`) and
hands it to `record_agent_run`, which is the only function that knows what
the row looks like.

Three things happen at write time and nowhere else:

- the transcript is **summarised** into the group-by columns
  (`src.pm.taxonomy.summarize_transcript`);
- a run that died gets its `outcome` from the **taxonomy**
  (`classify_error`), so `spec_error` / `system_error` / `guardrail_block`
  are decided by the same rules for every run;
- the **layer-one code checks** run on 100 % of runs and leave
  `quality_scores(source=code)` rows: `tool_groundedness` (every link or id
  the answer names is in some tool result — ADR-0052), `citation_groundedness`
  (every `[label]` cited was really retrieved — #102), `tools_ok` (no tool
  came back failed). Free, deterministic, and the floor the deck says must
  never be negotiated (slide 42: *"nền tảng không thể thương lượng của eval
  suite"*).

Nothing here is on a run's critical path. Every caller already writes after
the response has streamed, and a writer that raised would lose the bill too —
so `record_agent_run` computes what it can and stores what it has; a check
that fails to run is a missing score, not a missing run.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from src.agents.costs import CostFigure, by_model_of
from src.agents.threads import RunIdentity, ThreadNamespace
from src.db.models import AgentRun, QualityScore, RunOutcome, RunPipeline, RunTrigger, ScoreSource
from src.evals.checks import grade_citation_groundedness, grade_tool_groundedness
from src.models.schemas import SandboxTranscriptEntry
from src.pm.taxonomy import classify_error, summarize_transcript

logger = logging.getLogger(__name__)

#: The layer-one score names, as every reader spells them.
TOOL_GROUNDEDNESS, CITATION_GROUNDEDNESS, TOOLS_OK = "tool_groundedness", "citation_groundedness", "tools_ok"


@dataclass(frozen=True, slots=True)
class AgentRunRecord:
    """Everything one leg of one run hands the ledger, in the ledger's own words."""

    pipeline: str
    trace_id: str
    agent_key: str
    version_key: str
    agent_name: str
    conversation_id: str
    trigger: str
    input_message: str | None
    transcript: Sequence[SandboxTranscriptEntry] = ()
    error: str | None = None
    #: How the run stopped when it stopped some way other than finishing or
    #: dying (`needs_approval`, `budget_stopped`) — the same word the bill gets.
    ending: RunOutcome | None = None
    cost_usd: float = 0.0
    latency_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_by_model: Mapping[str, Any] = field(default_factory=dict)
    caller: str | None = None
    agent_id: int | None = None
    version_id: int | None = None
    run_cost_id: int | None = None
    simulated: bool = False
    #: A final answer the pipeline already knows (UC-1 hands one back); for V2
    #: it is read off the transcript's last spoken line.
    answer: str | None = None


def record_agent_run(db: Session, record: AgentRunRecord) -> AgentRun:
    """Add the run to `db` (not committed — the caller owns the transaction) with its layer-one scores."""
    transcript = [entry.model_dump(mode="json") for entry in record.transcript]
    summary = summarize_transcript(transcript, error=record.error)
    # A specific ending (`needs_approval`, `budget_stopped`) is the caller's
    # word and stands. The coarse `error` is not a word, it is the absence of
    # one — the taxonomy decides what the death was.
    if record.ending is not None and record.ending is not RunOutcome.ERROR:
        outcome = record.ending.value
    elif record.error:
        outcome = classify_error(record.error, transcript=transcript)
    else:
        outcome = RunOutcome.SUCCESS.value
    answer = record.answer if record.answer is not None else summary.answer
    run = AgentRun(
        pipeline=record.pipeline,
        trace_id=record.trace_id,
        agent_key=record.agent_key,
        version_key=record.version_key,
        agent_name=record.agent_name[:200],
        agent_id=record.agent_id,
        version_id=record.version_id,
        run_cost_id=record.run_cost_id,
        conversation_id=record.conversation_id,
        caller=record.caller,
        trigger=record.trigger,
        outcome=outcome,
        error=record.error,
        cost_usd=float(record.cost_usd or 0.0),
        latency_seconds=float(record.latency_seconds or 0.0),
        input_tokens=int(record.input_tokens or 0),
        output_tokens=int(record.output_tokens or 0),
        usage_by_model=dict(record.usage_by_model or {}),
        input_message=record.input_message,
        answer=answer,
        transcript=transcript,
        node_count=summary.node_count,
        tool_calls=summary.tool_calls,
        tool_failures=summary.tool_failures,
        actions=list(summary.actions),
        first_failure=summary.first_failure,
        failure_node_type=summary.failure_node_type,
        simulated=record.simulated,
        live_sends=any(entry.live_in_sandbox for entry in record.transcript),
    )
    db.add(run)
    db.flush()  # the scores below need `run.id`
    for score in layer_one_scores(record.transcript, answer or "", input_message=record.input_message or ""):
        db.add(
            QualityScore(
                agent_id=record.agent_id,
                version_id=record.version_id,
                run_cost_id=record.run_cost_id,
                run_id=run.id,
                name=score.name,
                value=score.value,
                comment=score.comment,
                source=ScoreSource.CODE,
            )
        )
    return run


@dataclass(frozen=True, slots=True)
class CodeScore:
    name: str
    value: float
    comment: str


def layer_one_scores(
    transcript: Sequence[SandboxTranscriptEntry], answer: str, *, input_message: str = ""
) -> list[CodeScore]:
    """The deterministic checks every run gets. Each is skipped — not failed — when it has nothing to check."""
    scores: list[CodeScore] = []
    if not transcript:
        return scores
    try:
        grounded = grade_tool_groundedness(transcript, answer, input_message=input_message)
        if grounded is not None:
            scores.append(CodeScore(TOOL_GROUNDEDNESS, 1.0 if grounded.passed else 0.0, grounded.reason))
        cited = grade_citation_groundedness(transcript, answer)
        if cited is not None:
            scores.append(CodeScore(CITATION_GROUNDEDNESS, 1.0 if cited.passed else 0.0, cited.reason))
    except Exception:  # noqa: BLE001 — a check that cannot run is a missing score, never a missing run
        logger.warning("Không chấm được lớp code cho run — bỏ qua điểm đó", exc_info=True)
    failed = [f"{e.node_id or e.step_id}:{e.tool}" for e in transcript if e.kind == "tool_result" and e.ok is False]
    called = any(e.kind == "tool" for e in transcript)
    if called:
        scores.append(
            CodeScore(
                TOOLS_OK,
                0.0 if failed else 1.0,
                ("Tool hỏng: " + ", ".join(failed[:5])) if failed else "Mọi tool được gọi đều trả về ok",
            )
        )
    return scores


# -- Builders, one per vocabulary ------------------------------------------- #


def v2_record(
    identity: RunIdentity,
    cost: CostFigure | None,
    *,
    sandbox: bool,
    agent_name: str,
    input_message: str | None,
    transcript: Sequence[SandboxTranscriptEntry],
    error: str | None = None,
    ending: RunOutcome | None = None,
    run_cost_id: int | None = None,
) -> AgentRunRecord:
    """A V2 leg (V1 spec or V2 workflow — one transcript shape) as a ledger record.

    `trigger` is derived exactly as `src.agents.spend.run_cost_row` derives it
    for the bill — from the thread's world, not from `sandbox` — so the two
    ledgers never disagree about which runs were the creator's own rehearsals.
    """
    thread = identity.thread
    trigger = RunTrigger.CHAT_TEST if sandbox or thread.namespace is ThreadNamespace.TRIAL else RunTrigger.MANUAL
    usage = cost.usage if cost is not None else None
    return AgentRunRecord(
        pipeline=RunPipeline.V2.value,
        trace_id=identity.trace_id,
        agent_key=str(thread.agent_id),
        version_key=str(identity.version_id),
        agent_name=agent_name,
        conversation_id=thread.conversation_id,
        caller=thread.caller,
        trigger=trigger.value,
        input_message=input_message,
        transcript=transcript,
        error=error,
        ending=ending,
        cost_usd=cost.usd if cost is not None else 0.0,
        latency_seconds=(cost.seconds or 0.0) if cost is not None else 0.0,
        input_tokens=usage.input_tokens if usage is not None else 0,
        output_tokens=usage.output_tokens if usage is not None else 0,
        usage_by_model=by_model_of(cost.by_model) if cost is not None else {},
        agent_id=thread.agent_id,
        version_id=identity.version_id,
        run_cost_id=run_cost_id,
    )


def uc1_transcript(steps: Iterable[Mapping[str, Any]]) -> list[SandboxTranscriptEntry]:
    """UC-1's `TranscriptStep` rows as the flat transcript shape the ledger keeps.

    UC-1 emits one row per node run, with the tool call folded in (`tool`,
    `args`, `result`). Unfolded here into the V1-style `step` / `tool` /
    `tool_result` lines — keyed by `step_id` because a UC-1 spec has no graph
    of node ids — so the same summariser, the same checks and the same screen
    read both pipelines. An agent-node row with no tool is a spoken step whose
    `content` is the node's result text.
    """
    lines: list[SandboxTranscriptEntry] = []
    for index, step in enumerate(steps, start=1):
        step_id = f"{step.get('node') or 'step'}-{index}"
        tool = step.get("tool")
        if tool:
            lines.append(
                SandboxTranscriptEntry(
                    kind="tool", step_id=step_id, tool=str(tool), args=dict(step.get("args") or {}), simulated=False
                )
            )
            result = step.get("result")
            ok = _uc1_tool_ok(result)
            lines.append(
                SandboxTranscriptEntry(
                    kind="tool_result",
                    step_id=step_id,
                    tool=str(tool),
                    ok=ok,
                    simulated=False,
                    content=str(result or ""),
                )
            )
        else:
            lines.append(
                SandboxTranscriptEntry(
                    kind="step",
                    step_id=step_id,
                    title=str(step.get("node") or ""),
                    content=str(step.get("result") or ""),
                )
            )
    return lines


#: What a UC-1 tool result starts with when the PEP refused or the call broke —
#: `src.compiler.tools` renders denials and errors as text, not exceptions.
_UC1_FAILURE_MARKS = ("error", "denied", "lỗi", "từ chối", "budget", "không được phép", "pep")


def _uc1_tool_ok(result: object) -> bool | None:
    if result is None:
        return None
    head = str(result).strip().lower()[:80]
    return not any(head.startswith(mark) or f'"{mark}"' in head for mark in _UC1_FAILURE_MARKS)


def uc1_record(
    *,
    request_id: str,
    agent_key: str,
    version: int,
    agent_name: str,
    thread_id: str,
    caller: str | None,
    input_message: str,
    steps: Sequence[Mapping[str, Any]],
    final_answer: str | None,
    total_cost_usd: float,
    budget_exceeded: bool,
    budget_detail: str | None = None,
) -> AgentRunRecord:
    """A UC-1 production turn as a ledger record. Always `manual`: UC-1's sandbox has its own route and table."""
    transcript = uc1_transcript(steps)
    tokens = sum(int(step.get("tokens") or 0) for step in steps)
    latency = sum(float(step.get("latency_ms") or 0.0) for step in steps) / 1000.0
    return AgentRunRecord(
        pipeline=RunPipeline.UC1.value,
        trace_id=request_id,
        agent_key=agent_key,
        version_key=str(version),
        agent_name=agent_name,
        conversation_id=thread_id,
        caller=caller,
        trigger=RunTrigger.MANUAL.value,
        input_message=input_message,
        transcript=transcript,
        error=budget_detail if budget_exceeded else None,
        ending=RunOutcome.BUDGET_STOPPED if budget_exceeded else None,
        cost_usd=total_cost_usd,
        latency_seconds=latency,
        # UC-1 reports one token figure per step, not an in/out split.
        input_tokens=tokens,
        output_tokens=0,
        usage_by_model={},
        answer=final_answer,
    )


# -- The V2 production door ------------------------------------------------- #


async def record_v2_run(
    identity: RunIdentity,
    cost: CostFigure,
    *,
    error: str | None,
    ending: RunOutcome | None,
    input_message: str,
    transcript: Sequence[SandboxTranscriptEntry],
    agent_name: str = "",
) -> None:
    """Bill and record of one V2 production leg, in one transaction, from inside a streaming response.

    Replaces `src.agents.spend.record_run_cost` as the thing `RunLedgerEntry`
    calls, and keeps every one of its rules: its own session on its own
    thread (the request's session is long closed by the time a run ends),
    `sandbox=False` because a trial's bill is committed beside its transcript
    by the sandbox itself, written once after the run has finished having
    opinions. What it adds is the second row — the `agent_runs` record with
    the transcript — committed with the first, so a run can never have a
    price and no record or a record and no price.
    """
    from src.agents.spend import run_cost_row  # local: `spend` is imported by both sandboxes
    from src.db.base import SessionLocal
    from src.db.models import Agent

    def write() -> None:
        db = SessionLocal()
        try:
            bill = run_cost_row(identity, cost, sandbox=False, error=error, ending=ending)
            db.add(bill)
            db.flush()
            name = agent_name or _agent_name(db.get(Agent, identity.thread.agent_id), identity.thread.agent_id)
            record_agent_run(
                db,
                v2_record(
                    identity,
                    cost,
                    sandbox=False,
                    agent_name=name,
                    input_message=input_message,
                    transcript=transcript,
                    error=error,
                    ending=ending,
                    run_cost_id=bill.id,
                ),
            )
            db.commit()
        finally:
            db.close()

    import asyncio

    await asyncio.to_thread(write)


def _agent_name(agent: Any, agent_id: int) -> str:
    return str(getattr(agent, "name", "") or f"agent {agent_id}")
