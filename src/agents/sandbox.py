"""Trial runs: executing a version without letting it touch the outside world.

See issue #9 / ADR-0007. A sandbox run is an ordinary run of the ordinary
runtime with one flag set — read-only tools really run, so what the creator
sees is trustworthy, while every side-effecting tool runs its simulation and
says what it *would* have done. There is no second execution path here, and
that is the point: evidence produced by a different engine than production
would not be evidence.

This module owns the two things a run leaves behind:

- the **transcript**, assembled from the very events the chat window already
  streams (ADR-0006), and stored as the mandatory attachment of a later
  approval request;
- the **version's status**, which moves `draft -> sandbox_passed` only when a
  run actually finished.

Thread separation is not enforced here but by the caller passing a `TRIAL`
conversation (see `src.agents.threads`) — one rule, one place.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

from sqlalchemy.orm import Session

from src.agents.costs import CostFigure
from src.agents.runtime import (
    BUDGET_EXCEEDED_EVENT,
    COST_EVENT,
    AgentEvent,
    Conversation,
    RunServices,
    stream_agent_reply,
)
from src.agents.spend import run_cost_row
from src.agents.threads import RunIdentity
from src.agents.versions import awaiting_sandbox, mark_sandbox_passed
from src.db.base import SessionLocal
from src.db.models import AgentVersion, SandboxRun, SandboxRunStatus
from src.models.agent_spec import AgentSpec
from src.models.schemas import SandboxTranscriptEntry
from src.pm.ledger import record_agent_run, v2_record

#: The SSE event a sandbox stream closes with, carrying the stored run's id.
#: Named here rather than in the HTTP layer because it is this module that
#: decides the run is over — the route only encodes what it is handed.
SANDBOX_RUN_EVENT = "sandbox_run"

#: Fallback reason for a budget stop that arrived without a message. Same
#: reasoning as the `or` on a dead run's `error` below: an empty reason is
#: indistinguishable from no failure, and that would promote the version.
BUDGET_STOP_WITHOUT_REASON = "Lần chạy thử dừng vì vượt trần ngân sách"


class SandboxStreamEvent(TypedDict):
    """One line of a sandbox stream, ready for the HTTP layer to encode as-is.

    `event` is a plain `str` rather than the runtime's `AgentEventName`: this
    stream carries the runtime's own names (ADR-0006) *plus* `error` for a run
    that died and `sandbox_run` for its record, and neither of those is
    something the runtime knows how to emit.
    """

    event: str
    data: dict[str, Any]


def transcript_entry(event: AgentEvent) -> SandboxTranscriptEntry | None:
    """`event` as a transcript line, or `None` for events the transcript does not keep.

    `token` events are dropped: they are the same answer arriving in pieces, and
    the assembled answer is already the `step` entry's content. Keeping them
    would multiply the size of the evidence an admin has to read without adding
    a fact to it.
    """
    data = event["data"]
    if event["event"] == "step":
        return SandboxTranscriptEntry(
            kind="step", step_id=data["step_id"], title=data["title"], content=data["content"]
        )
    if event["event"] == "tool":
        return SandboxTranscriptEntry(
            kind="tool",
            step_id=data["step_id"],
            tool=data["tool"],
            args=data["args"],
            simulated=data["simulated"],
        )
    if event["event"] == "tool_result":
        return SandboxTranscriptEntry(
            kind="tool_result",
            step_id=data["step_id"],
            tool=data["tool"],
            ok=data["ok"],
            simulated=data["simulated"],
            content=data["result"],
        )
    if event["event"] == BUDGET_EXCEEDED_EVENT:
        # In the transcript and not only in the run's `error` column: the
        # transcript is what an admin reads down looking for where a run went
        # wrong, and "it stopped here, on this ceiling" is that answer. The
        # column answers a different question — why the run produced no verdict.
        return SandboxTranscriptEntry(
            kind="budget_exceeded",
            step_id=data["step_id"],
            limit=data["limit"],
            limit_value=data["limit_value"],
            used=data["used"],
            content=data["message"],
        )
    return None


class SandboxRunRecord(TypedDict):
    """The `sandbox_run` event's payload: where the evidence landed, and what it changed.

    `version_status` is read back from the row rather than assumed, so a client
    is told what the version *is* — not what a successful run would have made it.
    """

    run_id: int
    status: str
    version_status: str
    error: str | None


@dataclass(frozen=True, slots=True)
class SandboxOutcome:
    """Everything a finished trial leaves behind, as one thing to write down.

    The three arrive together and are stored together, and passing them
    separately was how `store_sandbox_run` grew past what a reader can hold at
    once.
    """

    transcript: list[SandboxTranscriptEntry]
    error: str | None
    cost: CostFigure | None


def _agent_name_of(db: Session, version_id: int) -> str:
    """The agent's name for the ledger row, through the version — the trial only knows the version."""
    version = db.get(AgentVersion, version_id)
    agent = getattr(version, "agent", None)
    return str(getattr(agent, "name", "") or f"agent {getattr(version, 'agent_id', '?')}")


def store_sandbox_run(identity: RunIdentity, message: str, outcome: SandboxOutcome) -> SandboxRunRecord:
    """Write the run down and, if it finished, let the version move on.

    Shared by both sandboxes — V1's stream below and V2's in
    `src.workflow.sandbox` (#46) — rather than copied, because everything it
    decides is a rule about *evidence* and not about either runtime: what makes
    a run failed, that the transcript and its price are one commit, and when a
    version has earned `sandbox_passed`. A second copy would be a second answer
    to the last of those, and the two would drift the first time one of them was
    improved.

    Opens its own `Session` rather than borrowing the request's: by the time
    this runs the response body is already streaming, and FastAPI has long
    since closed the session its dependency handed the endpoint.

    Advancing the version is conditional on the run having finished *and* on the
    version still awaiting a trial — `draft` for V1, `workflow_confirmed` for V2
    (`AWAITING_SANDBOX`). A version already past that point is left exactly
    where it is: a creator re-running the sandbox on a version that is waiting
    for approval must not drag it backwards out of the queue.

    The transcript and what the run cost (#11) are committed together. They are
    two halves of one piece of evidence — an approval dossier showing what an
    agent did without what it charged is half an answer — and a transcript that
    could exist without its price would eventually be one.
    """
    status = SandboxRunStatus.FAILED if outcome.error is not None else SandboxRunStatus.SUCCEEDED
    db: Session = SessionLocal()
    try:
        run = SandboxRun(
            version_id=identity.version_id,
            conversation_id=identity.thread.conversation_id,
            input_message=message,
            transcript=[entry.model_dump(mode="json") for entry in outcome.transcript],
            status=status,
            error=outcome.error,
        )
        db.add(run)
        if outcome.cost is not None:
            bill = run_cost_row(identity, outcome.cost, sandbox=True, error=outcome.error)
            db.add(bill)
            db.flush()
            # The PM's ledger (ADR-0053): the same transcript, the same price,
            # one more row — committed with the other two, for the same reason
            # those two are committed together.
            record_agent_run(
                db,
                v2_record(
                    identity,
                    outcome.cost,
                    sandbox=True,
                    agent_name=_agent_name_of(db, identity.version_id),
                    input_message=message,
                    transcript=outcome.transcript,
                    error=outcome.error,
                    run_cost_id=bill.id,
                ),
            )
        db.commit()
        db.refresh(run)

        # `get_one`: the insert above carries a foreign key to this row, so a
        # missing version here is a broken database, not a case to branch on.
        version = db.get_one(AgentVersion, identity.version_id)
        if status is SandboxRunStatus.SUCCEEDED and awaiting_sandbox(version):
            mark_sandbox_passed(db, version)
        return SandboxRunRecord(
            run_id=run.id,
            status=status.value,
            version_status=version.status.value,
            error=outcome.error,
        )
    finally:
        db.close()


#: How a run's events become transcript lines. One per runtime, because that is
#: the one thing the two genuinely disagree about — V1's payloads carry
#: `step_id`, V2's carry `node_id`, and ADR-0031 §6 is emphatic that the two must
#: not be collapsed. Everything *around* the mapping is `record_trial`.
TranscriptMapper = Callable[[Any], SandboxTranscriptEntry | None]

#: What a finished trial does with everything it collected. `store_sandbox_run`
#: is the answer for a run that reached its end, and the parameter exists because
#: since #47 there is a second answer: a V2 run can stop in front of a person
#: (ADR-0024), and a run that has not finished must not be stored as one — a
#: version would earn `sandbox_passed` on a transcript that never reached the
#: side effect the approval exists to guard.
#:
#: `None` means "nothing was stored", and is why the closing `sandbox_run` event
#: is conditional: a paused trial has already said what happened, on the
#: `awaiting_approval` event the runtime emitted, and a `sandbox_run` event after
#: it would name a record that does not exist.
#:
#: Which of the two applies is the *caller's* knowledge, not this module's:
#: nothing about pausing is a rule about evidence, so nothing about it belongs in
#: the shared half. `src.workflow.sandbox` owns that decision.
TrialStore = Callable[[RunIdentity, str, "SandboxOutcome"], SandboxRunRecord | None]


async def record_trial(
    events: AsyncIterator[Any],
    message: str,
    *,
    identity: RunIdentity,
    to_entry: TranscriptMapper,
    prior: Sequence[SandboxTranscriptEntry] = (),
    store: TrialStore = store_sandbox_run,
) -> AsyncIterator[SandboxStreamEvent]:
    """Watch a run happen, write down what it did, and close with the stored record.

    The whole of "what a trial run leaves behind", shared by both sandboxes (#46)
    — V1's `stream_sandbox_run` below and V2's `stream_workflow_sandbox_run` —
    because every decision in here is about *evidence* rather than about either
    runtime. `events` is whichever run is being watched, already started by the
    caller who knows how to start it; `to_entry` is the one thing that differs.

    Yields exactly what the underlying run yields, so a client renders a trial
    with the code it already has, then one `sandbox_run` event naming the row the
    transcript was saved to and the version's status after it.

    A run that dies mid-flight becomes an `error` event and a `failed` record:
    once the response body is open there is no status code left to fail with, and
    a crashed run must still be visible rather than vanishing into a truncated
    stream.

    A run stopped by its own budget (#10) is `failed` too, and this is the one
    place that decides so. The runtime's job is to stop the run and say which
    ceiling; what a stopped run *means* is a question about evidence, and the
    answer is that a run which never reached its end proves nothing about the
    steps it never got to — so the version stays where it is rather than earning
    `sandbox_passed` on a truncated transcript.

    A run the *client* abandons is not recorded at all — the generator is closed
    before it gets here, and an abandoned trial has nothing to be evidence of.

    What the run cost (#11) is captured off the `cost` event both runtimes end
    every run with, rather than measured here. The measured figure a creator is
    shown is then, by construction, the ledger the run was actually enforced
    against — not a second count that could disagree with it.

    `prior` is the transcript of an earlier **leg** of this same run — lines a V2
    run wrote before it stopped for a human decision (#47) and parked. They are
    prepended rather than stored separately so that one run produces one
    transcript: an admin reading down the evidence for *"where did this go
    wrong"* should not have to notice that the interesting part is in a different
    row.
    """
    transcript: list[SandboxTranscriptEntry] = [*prior]
    error: str | None = None
    cost: CostFigure | None = None
    try:
        async for event in events:
            if event["event"] == COST_EVENT:
                cost = CostFigure.from_payload(event["data"])
            entry = to_entry(event)
            if entry is not None:
                transcript.append(entry)
                # Read off the entry rather than testing the event a second time:
                # one question ("is this line a stop?"), asked once, so the
                # transcript and the record can never disagree about whether the
                # run finished.
                if entry.kind == BUDGET_EXCEEDED_EVENT:
                    error = entry.content or BUDGET_STOP_WITHOUT_REASON
            yield SandboxStreamEvent(event=event["event"], data=dict(event["data"]))
    except Exception as exc:  # noqa: BLE001 — reported in band; the stream is already open
        # `or` and not a bare `str(exc)`: an exception raised with no message
        # would leave `error` empty, and an empty reason is indistinguishable
        # from no failure at all — which would promote the version on a run
        # that crashed.
        error = str(exc) or f"Lần chạy thử dừng vì {type(exc).__name__}"
        yield SandboxStreamEvent(event="error", data={"message": error})

    record = await asyncio.to_thread(
        store, identity, message, SandboxOutcome(transcript=transcript, error=error, cost=cost)
    )
    if record is not None:
        # `dict(...)`: a `TypedDict` is not assignable to `dict[str, Any]`, the
        # same reason `src.agents.runtime._event` copies its payload.
        yield SandboxStreamEvent(event=SANDBOX_RUN_EVENT, data=dict(record))


async def stream_sandbox_run(
    spec: AgentSpec,
    message: str,
    *,
    identity: RunIdentity,
    services: RunServices,
    conversation: Conversation,
) -> AsyncIterator[SandboxStreamEvent]:
    """Run `spec` in sandbox mode, recording everything — V1's half of `record_trial`.

    The two lines here are the whole of what a V1 trial is: start the V1 runtime
    with `sandbox=True`, and read its events with V1's transcript mapper.
    """
    async for event in record_trial(
        stream_agent_reply(spec, message, services=services, conversation=conversation, sandbox=True),
        message,
        identity=identity,
        to_entry=transcript_entry,
    ):
        yield event
