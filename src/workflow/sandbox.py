"""Trial runs of a confirmed workflow: what a creator watches before anyone approves it.

See issue #46 / ADR-0026 / ADR-0032. This is `src.agents.sandbox` for V2's
document, and deliberately the same shape, because the two answer the same
question about two different definitions: *what did this thing do, and may it now
be sent for review?*

What is shared with V1 is everything that is about **evidence** rather than about
either runtime, and that turned out to be nearly all of it: `record_trial`
watches the run, decides what makes it failed, captures its price and closes with
the stored record, and `store_sandbox_run` writes the row and decides when a
version has earned `sandbox_passed`.

What is left here is the one thing that genuinely differs — how a run of a
*graph* reads as transcript lines. A `step_id` indexes `AgentSpec.steps` and a
`node_id` indexes `WorkflowJSON.nodes`, so the mapping cannot be shared without
collapsing the two documents into one, which is the same reason ADR-0031 §6 kept
the two event vocabularies apart. That mapping, and the call that starts a V2 run
rather than a V1 one, is the whole of this module.

**Both halves of "mock/read-only" bite since #47.** `KnowledgeSearch` reads the
creator's own files, `WebSearch` reads the public web and `AgentLlm` calls a
provider that is charged for in both modes alike — all live, all real. `MCP` is
the node that finally has something to mock, and whether it does is read off the
action's own `sandbox_behavior` (ADR-0023 §4) rather than off this module's
mode: a `low` risk action runs for real in a rehearsal too. So `simulated` still
describes the implementation that ran, never the world the run happened in.

**A trial that stops in front of a person is not a finished trial.** ADR-0024 §7
makes `HumanApproval` pause for real in sandbox mode, which means a trial run can
end without reaching its `End` — and storing that as a `sandbox_run` would hand a
version `sandbox_passed` on a transcript that stopped before the side effect the
approval exists to guard. So a paused trial parks its lines on the
`run_approvals` row instead, and the leg that resumes it prepends them and stores
**one** record. That is what makes "the demo ran end-to-end in sandbox" a single
readable piece of evidence rather than two halves an admin has to staple
together.

Thread separation is not enforced here but by the caller passing a `TRIAL`
conversation — one rule, one place, exactly as in V1.
"""

from collections.abc import AsyncIterator
from typing import cast

from src.agents.runtime import Conversation, RunServices
from src.agents.sandbox import (
    SandboxOutcome,
    SandboxRunRecord,
    SandboxStreamEvent,
    _agent_name_of,
    record_trial,
    store_sandbox_run,
)
from src.agents.spend import run_cost_row
from src.agents.threads import RunIdentity
from src.datasources.reader import DatabaseReader
from src.db.base import SessionLocal
from src.db.models import RunOutcome
from src.models.schemas import SandboxTranscriptEntry
from src.models.workflow import WorkflowJSON
from src.pm.ledger import record_agent_run, v2_record
from src.workflow.approvals import AWAITING_APPROVAL_EVENT, ApprovalRequest, ParkedLeg, park_transcript
from src.workflow.knowledge import KnowledgeStore
from src.workflow.runtime import (
    BUDGET_EXCEEDED_EVENT,
    Resumption,
    WorkflowEvent,
    stream_workflow_reply,
)


def transcript_entry(event: WorkflowEvent) -> SandboxTranscriptEntry | None:
    """`event` as a transcript line, or `None` for events the transcript does not keep.

    Public since #48: an eval run reads the same lines to grade a rule-based case
    (`src.evals.checks`). One mapping rather than two is the point — a verdict
    explained by "the workflow called `send_email`" and a transcript an admin
    reads that does not mention it would be one system disagreeing with itself
    about what happened.

    Two kinds are dropped and for different reasons. `token` events are the same
    answer arriving in pieces, and the assembled answer is already the `node`
    entry's content — keeping them would multiply the size of the evidence an
    admin has to read without adding a fact to it (ADR-0007 §5). `node_started`,
    `usage` and `cost` are dropped because each is already answered elsewhere in
    the record: what ran is the `node` line, and what the run spent is the
    `run_costs` row committed beside the transcript.

    `awaiting_approval` is dropped too, and for a third reason again: it is not a
    line of the run, it is the run *stopping*. What it says — which node, which
    approval — is already the `node` line above it, and the record it produces is
    the `run_approvals` row itself, which outlives the transcript by design
    (ADR-0024 §2).

    ADR-0026's `port_taken`, `iteration` and forced-exit flag arrive on the `node`
    line with #47, filled in by the decision nodes that can fill them.
    """
    data = event["data"]
    if event["event"] == "node":
        return SandboxTranscriptEntry(
            kind="node",
            node_id=data["node_id"],
            node_type=data["node_type"],
            content=data["content"],
            port_taken=data.get("port_taken"),
            iteration=data.get("iteration"),
            forced_exit=data.get("forced_exit"),
        )
    if event["event"] == "tool":
        return SandboxTranscriptEntry(
            kind="tool",
            node_id=data["node_id"],
            tool=data["tool"],
            args=data["args"],
            simulated=data["simulated"],
        )
    if event["event"] == "tool_result":
        return SandboxTranscriptEntry(
            kind="tool_result",
            node_id=data["node_id"],
            tool=data["tool"],
            ok=data["ok"],
            simulated=data["simulated"],
            content=data["result"],
            # `.get` rather than `[...]`: an event replayed from a stored run
            # predates ADR-0061's field, and the whole point of the field is
            # that its absence means "nobody said", not "there was no fault".
            fault=data.get("fault"),
        )
    if event["event"] == BUDGET_EXCEEDED_EVENT:
        # In the transcript and not only in the run's `error` column, for V1's
        # reason: the transcript is what an admin reads down looking for where a
        # run went wrong, and "it stopped here, on this ceiling" is that answer.
        return SandboxTranscriptEntry(
            kind="budget_exceeded",
            node_id=data["node_id"],
            limit=data["limit"],
            limit_value=data["limit_value"],
            used=data["used"],
            content=data["message"],
        )
    return None


def _park_paused_trial(identity: RunIdentity, message: str, request: ApprovalRequest, outcome: SandboxOutcome) -> None:
    """Write down what a trial had done when it stopped for a person, and bank what it cost.

    Two writes, one commit each, and neither is a `sandbox_run`: the run has not
    finished, so there is no finished trial to record. The transcript waits on
    the `run_approvals` row for the leg that resumes it; the price does **not**
    wait, because money spent before a pause is money spent whether or not
    anybody ever gets round to deciding — and a leg that was never banked would
    be missing from the answer to "which agent is costing us money" (ADR-0009
    §4).

    That per-leg banking is why `measured_cost` prices a leg rather than a run:
    each leg's tokens are written down exactly once, and a run that went round
    three approvals is three honest rows rather than one row counted three times.
    """
    park_transcript(request["approval_id"], outcome.transcript)
    if outcome.cost is None:
        return
    db = SessionLocal()
    try:
        bill = run_cost_row(identity, outcome.cost, sandbox=True, ending=RunOutcome.NEEDS_APPROVAL)
        db.add(bill)
        db.flush()
        record_agent_run(
            db,
            v2_record(
                identity,
                outcome.cost,
                sandbox=True,
                agent_name=_agent_name_of(db, identity.version_id),
                input_message=message,
                transcript=outcome.transcript,
                ending=RunOutcome.NEEDS_APPROVAL,
                run_cost_id=bill.id,
            ),
        )
        db.commit()
    finally:
        db.close()


async def _trial(
    events: AsyncIterator[WorkflowEvent],
    message: str,
    *,
    identity: RunIdentity,
    prior: list[SandboxTranscriptEntry],
) -> AsyncIterator[SandboxStreamEvent]:
    """Record `events` as a trial, parking it instead of storing it if it stopped for a person.

    The pause is noticed here and nowhere else. `record_trial` is shared with V1
    and everything in it is a rule about *evidence* — what makes a run failed,
    that a transcript and its price commit together, when a version has earned
    `sandbox_passed`. Pausing is none of those; it is a fact about V2's runtime.
    So this module watches for it and hands `record_trial` a different answer to
    "what do you do with what you collected", which is the one thing the outcome
    changes.
    """
    paused: list[ApprovalRequest] = []

    async def watched() -> AsyncIterator[WorkflowEvent]:
        async for event in events:
            if event["event"] == AWAITING_APPROVAL_EVENT:
                paused.append(cast(ApprovalRequest, event["data"]))
            yield event

    def store(run_identity: RunIdentity, asked: str, outcome: SandboxOutcome) -> SandboxRunRecord | None:
        if paused:
            _park_paused_trial(run_identity, asked, paused[-1], outcome)
            return None
        return store_sandbox_run(run_identity, asked, outcome)

    async for event in record_trial(
        watched(), message, identity=identity, to_entry=transcript_entry, prior=prior, store=store
    ):
        yield event


async def stream_workflow_sandbox_run(
    workflow: WorkflowJSON,
    message: str,
    *,
    identity: RunIdentity,
    services: RunServices,
    conversation: Conversation,
    knowledge: KnowledgeStore,
    resume: tuple[ParkedLeg, Resumption] | None = None,
    databases: DatabaseReader | None = None,
) -> AsyncIterator[SandboxStreamEvent]:
    """Trial `workflow` against `message`, recording everything, and close with the stored run.

    The two lines here are the whole of what a V2 trial is: start the V2 runtime
    with `sandbox=True`, and read its events with V2's transcript mapper.
    Everything that happens *around* that — the budget stop becoming a failure,
    the crash becoming an `error` event and a `failed` row, the price captured
    off the run's own `cost` event, the closing `sandbox_run` — is
    `record_trial`, shared with V1 because none of it is about either runtime.

    So the guarantees are V1's, and hold for the same reasons: a run stopped by
    its ceiling proves nothing about the nodes it never reached and leaves the
    version at `workflow_confirmed`; a run the client abandons is not recorded at
    all; and the figure a creator is shown (AC 3) is by construction the ledger
    the run was actually enforced against.

    A run that stops at a `HumanApproval` node is a fourth ending and gets none
    of those: it closes with `awaiting_approval` instead of `sandbox_run`, and a
    later call with `resume` set is what finishes it.

    **A resumed leg is the same trial, not a new one.** `leg.identity` is the
    identity the run started with and `leg.transcript` is what it had already
    said, so what is finally stored is one record of one run. Rebuilding either
    from the deciding request would file evidence about a run that never
    happened. A run that stops *again* — a `While` body carrying the approval
    node round twice — parks again, on its own row (ADR-0024 §3), and the
    transcript keeps growing across legs until a leg reaches `End`.
    """
    leg, resumption = resume if resume is not None else (None, None)
    async for event in _trial(
        stream_workflow_reply(
            workflow,
            message,
            services=services,
            conversation=conversation,
            knowledge=knowledge,
            databases=databases,
            sandbox=True,
            identity=leg.identity if leg is not None else identity,
            resume=resumption,
        ),
        message,
        identity=leg.identity if leg is not None else identity,
        prior=leg.transcript if leg is not None else [],
    ):
        yield event
