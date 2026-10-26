"""The `run_approvals` ledger — a run stopped in front of a person, made queryable.

See issue #47 / ADR-0024. `HumanApproval` pauses a run with LangGraph's own
`interrupt()`, and the checkpointer persists exactly where it stopped. That is
the resume source of truth and this module never competes with it: what a
checkpoint cannot do is answer *"which runs are waiting on a human right now"*
without deserialising every in-flight one, and it makes no promise to still exist
once LangGraph prunes it. So every pause writes a row here **alongside** the
checkpoint — the same division ADR-0012 §1 drew between the dossier a reviewer
reads and the machinery that actually gates a run.

Four things happen to one of these rows, and the functions here do them (the
rest of the module is the reading half — turning a row back into the dossier a
reviewer sees and the leg a run resumes from):

- `open_approval` writes it, **idempotently**. `interrupt()` re-executes a node
  from the top when the run resumes, so everything before the interrupt runs
  twice; keying on `(run_id, node_id, occurrence_index)` — a key derived from the
  run's own state rather than minted fresh — is what makes the second execution
  find the first execution's row instead of opening a second pause.
- `park_ledger` and `park_transcript` write down what the pause has to remember.
  They are two functions because they have two owners: the runtime knows what the
  run had spent (ADR-0024 §7's paused clock), the sandbox knows what the
  transcript said so far, and neither should have to be handed the other's state
  to write its own.
- `decide_approval` records the decision, and is the *only* way a row leaves
  `pending`. It refuses a rejection with no reason, mirroring ADR-0012 §2 — a
  `rejected` port routing into a "revise and resubmit" branch has nothing to
  revise against if nobody said what was wrong.

**An approval is of a payload, not of a node** (DS-08 / ADR-0059 §7). V2 puts
no limit on who an email may go to, so the person deciding is the only thing
between a model and the outside world, and a "yes" is only worth something if
it is a yes to exactly what was shown. A row therefore carries `actions` — the
call each guarded action node will make, `{node_id, tool, args, hash}` — and
the card shows them verbatim. `action_hash` is the canonical digest of one
call; the action node recomputes it right before calling and refuses on any
difference (`approved_action_hash` is the database's side of that check).

**There is no auto-approval path here, and that is the point** (ADR-0024 §7). No
function in this module writes `approved` without a person's id and a real call,
and a sandbox run reaches exactly these functions rather than a permissive
variant of them: a trial is only evidence of what production would do if it stops
where production would stop.

Sessions are opened here rather than borrowed, for `store_sandbox_run`'s reason:
by the time a pause happens the response body is already streaming and the
request's session is long closed.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.budget import ModelUsage, usage_of_payload, usage_payload
from src.agents.threads import RunIdentity, ThreadNamespace, ThreadRef
from src.db.base import SessionLocal
from src.db.models import RunApproval, RunApprovalStatus
from src.models.schemas import SandboxTranscriptEntry

#: The event a run emits instead of finishing, when it stopped to ask somebody.
#:
#: Its own name rather than an `error`: a paused run has not failed, and calling
#: it one would make `record_trial` mark the trial `failed` and an admin reading
#: the queue believe something broke. Defined here, beside the rows it points at,
#: so the runtime that emits it and the sandbox that reacts to it read one
#: spelling. `Final` so it narrows to its own literal type and can satisfy
#: `WorkflowEventName` without that module and this one importing each other.
AWAITING_APPROVAL_EVENT: Final = "awaiting_approval"


def action_hash(tool: str, args: Mapping[str, Any]) -> str:
    """The canonical digest of one call — the thing an approval is *of* (DS-08).

    Canonical JSON: keys sorted, no whitespace, non-ASCII kept as itself, so
    the same call hashes the same however its dict was built, and changing so
    much as one character of a body changes it. Values are not trimmed or
    case-folded: what the person read is exactly what is hashed, and a
    "normalisation" that equated two bodies would approve one and send the
    other. `default=str` only so a value JSON cannot carry still hashes.
    """
    canonical = json.dumps(
        {"tool": tool, "args": dict(args)}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalAction(TypedDict):
    """One call a pause stands in front of, exactly as the action node will make it — DS-08.

    `node_id` is the action node that will make it; `tool` the wire name
    (`gmail.send_email`) and `action` the action's own name on its connection
    (`send_email`); `args` the arguments, verbatim; `hash` their
    `action_hash`, written once when the pause opens.
    """

    node_id: str
    tool: str
    action: str
    args: dict[str, Any]
    hash: str


class ApprovalRequest(TypedDict):
    """What a reviewer is shown, and what `interrupt()` carries as its payload.

    Also exactly the `awaiting_approval` event's data, deliberately: the client
    watching a run pause and the reviewer opening the queue later are looking at
    the same pause, and two shapes for it would be two chances to describe it
    differently.
    """

    approval_id: int
    run_id: str
    node_id: str
    occurrence_index: int
    approver_role: str
    message: str
    #: The calls this pause guards (DS-08) — empty for a gate with nothing but prose behind it.
    actions: list[ApprovalAction]


class ApprovalDecision(TypedDict):
    """What a person decided, as it travels back into the graph via `Command(resume=...)`.

    `reason` rides along rather than being looked up from the row on the other
    side, because ADR-0024 §6 has the node write it into `RuntimeState.messages`:
    a `rejected` port routing into an `AgentLlm` node must know *why* it was
    rejected, not merely which port it arrived through.
    """

    status: str
    reason: str | None
    decided_by: int | None


class ApprovalDecisionError(ValueError):
    """A decision that cannot be recorded as given. User-facing."""


@dataclass(frozen=True, slots=True)
class ParkedLeg:
    """Everything the resumed half of a run needs that the HTTP request cannot carry.

    A run pauses in the middle of a response body that is already closing, so the
    next leg arrives as a *different* request with nothing but an approval id.
    This is what that id buys back: which graph execution it was (`run_id`), which
    thread holds its checkpoint, what it was asked, what it had already spent, and
    — for a trial — the transcript lines written before the pause, so the finished
    run stores one piece of evidence rather than two halves of one.
    """

    approval_id: int
    run_id: str
    node_id: str
    identity: RunIdentity
    sandbox: bool
    input_message: str
    carried_seconds: float
    carried_usage: dict[str, ModelUsage]
    transcript: list[SandboxTranscriptEntry]
    #: Who the first leg read data as (DS-05) — the resumed leg asks as them too.
    run_as: str | None = None


def _identity_of(row: RunApproval) -> RunIdentity:
    """The run identity stored across `row`'s columns, reassembled.

    Stored as its parts rather than as one opaque thread id because ownership is
    checked against `agent_id` and the trial/production split is read off
    `namespace` — both questions a joined string would have to be parsed to
    answer.

    `caller` is the third part of the address, and it is read here for the exact
    reason this docstring used to give for leaving it out: *"whoever gives a
    workflow run a production namespace has to add that column in the same
    change, or a resumed run will address a different conversation from the one
    it paused in."* `deployed-workflow-chat` did give workflow runs a production
    namespace, and the prediction came true — a decided run replayed from its
    first node, on an empty thread, because the checkpoint it wanted lived under
    an address with a caller in it. `None` on a rehearsal, where the `trial`
    namespace is the creator's own space and carries no caller at all.
    """
    thread = ThreadRef(
        agent_id=row.agent_id,
        conversation_id=row.conversation_id,
        namespace=ThreadNamespace(row.namespace),
        caller=row.caller,
    )
    return RunIdentity(thread=thread, version_id=row.version_id, trace_id=row.trace_id)


def _request_of(row: RunApproval) -> ApprovalRequest:
    """`row` as the dossier a reviewer reads and `interrupt()` carries."""
    return ApprovalRequest(
        approval_id=row.id,
        run_id=row.run_id,
        node_id=row.node_id,
        occurrence_index=row.occurrence_index,
        approver_role=row.approver_role,
        message=row.message,
        actions=[ApprovalAction(**action) for action in row.actions or []],
    )


def find_approval(*, run_id: str, node_id: str, occurrence_index: int) -> ApprovalRequest | None:
    """The row for this pause if it was already opened — what a resumed node reads instead of preparing again.

    `open_approval` is idempotent about the *row*; this is what makes the
    payload idempotent too. The calls a pause shows are prepared by a model,
    and a second preparation on resume could word the body differently from
    the one the person just approved — so the resumed node reads the approved
    one back rather than asking again.
    """
    db: Session = SessionLocal()
    try:
        row = db.scalars(
            select(RunApproval).where(
                RunApproval.run_id == run_id,
                RunApproval.node_id == node_id,
                RunApproval.occurrence_index == occurrence_index,
            )
        ).one_or_none()
        return None if row is None else _request_of(row)
    finally:
        db.close()


def approved_action_hash(approval_id: int, node_id: str) -> str | None:
    """The hash `approval_id` approved for `node_id`'s call — `None` unless that row is approved and has one.

    The database's half of the check an action node makes before calling
    (DS-08): what the graph state says was approved is compared against what
    the row says, so a checkpoint cannot claim an approval nobody gave.
    """
    db: Session = SessionLocal()
    try:
        row = db.get(RunApproval, approval_id)
        if row is None or row.status is not RunApprovalStatus.APPROVED:
            return None
        return next((action["hash"] for action in row.actions or [] if action.get("node_id") == node_id), None)
    finally:
        db.close()


def open_approval(
    identity: RunIdentity,
    *,
    run_id: str,
    node_id: str,
    occurrence_index: int,
    approver_role: str,
    message: str,
    input_message: str,
    sandbox: bool,
    run_as: str | None = None,
    actions: Sequence[ApprovalAction] = (),
) -> ApprovalRequest:
    """The pending row for this pause — found if it is already there, written if not.

    `run_as` is the asker the run's `data_query` reads were scoped to (DS-05),
    kept so the resumed leg reads as the same person. `actions` are the calls
    the pause guards (DS-08), written once with the row and never rewritten:
    an existing row keeps the payload it was opened with.

    Idempotent because it has to be. `interrupt()` suspends a node and, on
    resume, runs that node's function again from the top: everything before the
    interrupt executes a second time. A row inserted unconditionally would
    therefore open a second pause every time somebody approved the first one.

    What makes finding it possible is that `occurrence_index` is *derived* — it
    counts the decisions this node has already completed in this run, which the
    graph state carries — rather than minted at insert time. A `While` loop
    carrying a `HumanApproval` node round twice gets two rows because the second
    pass genuinely has a different index (ADR-0024 §3), not because two inserts
    happened to race.
    """
    db: Session = SessionLocal()
    try:
        existing = db.scalars(
            select(RunApproval).where(
                RunApproval.run_id == run_id,
                RunApproval.node_id == node_id,
                RunApproval.occurrence_index == occurrence_index,
            )
        ).one_or_none()
        if existing is not None:
            return _request_of(existing)

        thread = identity.thread
        row = RunApproval(
            run_id=run_id,
            node_id=node_id,
            occurrence_index=occurrence_index,
            version_id=identity.version_id,
            agent_id=thread.agent_id,
            conversation_id=thread.conversation_id,
            namespace=thread.namespace.value,
            caller=thread.caller,
            sandbox=sandbox,
            trace_id=identity.trace_id,
            approver_role=approver_role,
            message=message,
            input_message=input_message,
            run_as=run_as,
            actions=[dict(action) for action in actions],
            status=RunApprovalStatus.PENDING,
            transcript=[],
            elapsed_seconds=0.0,
            usage_by_model={},
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return _request_of(row)
    finally:
        db.close()


def park_ledger(approval_id: int, *, elapsed_seconds: float, usage_by_model: dict[str, ModelUsage]) -> None:
    """Write down what the run had spent when it stopped — the runtime's half of the pause.

    Without this, ADR-0024 §7 is unimplementable: `max_wall_seconds` is measured
    against running time, and running time is only separable from
    wall-clock-since-start if the running time so far is recorded at the moment
    the clock is supposed to stop. A reviewer who takes a day to decide must not
    thereby fail the run.
    """
    db: Session = SessionLocal()
    try:
        row = db.get_one(RunApproval, approval_id)
        row.elapsed_seconds = elapsed_seconds
        row.usage_by_model = usage_payload(usage_by_model)
        db.commit()
    finally:
        db.close()


def park_transcript(approval_id: int, transcript: Sequence[SandboxTranscriptEntry]) -> None:
    """Keep a paused trial's transcript so far — the sandbox's half of the pause.

    A trial that stops for a person has not finished, so it must not be stored as
    a `sandbox_run`: a version would earn `sandbox_passed` on a run that never
    reached the side effect the approval exists to guard. The lines live here
    until the resumed leg finishes and stores **one** complete transcript, which
    is what makes "the demo runs end-to-end in sandbox" a single readable piece
    of evidence rather than two halves an admin has to staple together.
    """
    db: Session = SessionLocal()
    try:
        row = db.get_one(RunApproval, approval_id)
        row.transcript = [entry.model_dump(mode="json") for entry in transcript]
        db.commit()
    finally:
        db.close()


def pending_approvals(db: Session, *, version_id: int) -> list[RunApproval]:
    """Every pause of this version's runs still waiting on somebody, oldest first.

    Oldest first because a queue is worked from the front, and because a run that
    has been waiting longest is the one closest to being forgotten.
    """
    return list(
        db.scalars(
            select(RunApproval)
            .where(RunApproval.version_id == version_id, RunApproval.status == RunApprovalStatus.PENDING)
            .order_by(RunApproval.created_at, RunApproval.id)
        )
    )


def approval_for_decision(db: Session, approval_id: int, *, version_id: int) -> RunApproval:
    """The row `approval_id` names, if it belongs to `version_id` and is still open.

    Both refusals are `ApprovalDecisionError` rather than two types, because the
    caller does the same thing with them: a row that is not this version's is not
    this caller's business, and one already decided is a decision somebody else
    made — neither is a decision this request may now record.
    """
    row = db.get(RunApproval, approval_id)
    if row is None or row.version_id != version_id:
        raise ApprovalDecisionError(f"Không tìm thấy yêu cầu duyệt {approval_id}")
    if row.status is not RunApprovalStatus.PENDING:
        raise ApprovalDecisionError(
            f"Yêu cầu duyệt {approval_id} đã được quyết định rồi ({row.status.value}) — không quyết định lại"
        )
    return row


def decide_approval(
    db: Session, row: RunApproval, *, decided_by: int, approved: bool, reason: str | None
) -> ApprovalDecision:
    """Record the decision on `row` and hand back what the graph resumes with.

    A rejection without a reason is refused, mirroring ADR-0012 §2's rule for
    version rejection and for the same reason twice over: an auditor reading
    `run_approvals` later needs to know why, and — this is the part unique to a
    *running* graph — the `rejected` port may route into a node whose whole job
    is to act on that answer (ADR-0024 §6). "No" with nothing after it is not
    something a branch can be written against.

    The row is written before the graph is resumed, never after, so a resume that
    crashes mid-stream still leaves a permanent record that somebody decided.
    `decided_by` is whoever the route let through, not necessarily an admin —
    ADR-0024 §5 declines to enforce `approver_role`, and the door this is reached
    by is the creator's own (see `src.api.workflow_run_routes`). ADR-0024 §7's
    known gap stands unchanged: two people deciding at once are not arbitrated,
    and the later call is what the row and the graph end up reflecting.
    """
    cleaned = (reason or "").strip()
    if not approved and not cleaned:
        raise ApprovalDecisionError("Từ chối thì phải nêu lý do — nhánh 'rejected' còn phải dựa vào đó mà chạy tiếp")
    row.status = RunApprovalStatus.APPROVED if approved else RunApprovalStatus.REJECTED
    row.decided_by = decided_by
    row.decided_at = datetime.now(UTC).replace(tzinfo=None)
    row.reason = cleaned or None
    db.commit()
    return ApprovalDecision(status=row.status.value, reason=row.reason, decided_by=decided_by)


def parked_leg(db: Session, row: RunApproval) -> ParkedLeg:
    """`row` read back as everything the resumed leg needs to carry on.

    Read from the row rather than rebuilt from the request, because none of it is
    the deciding caller's to state: which thread the checkpoint is under, what the
    run was originally asked, and what it had already spent are all facts about a
    run that started before this request existed.
    """
    return ParkedLeg(
        approval_id=row.id,
        run_id=row.run_id,
        node_id=row.node_id,
        identity=_identity_of(row),
        sandbox=row.sandbox,
        input_message=row.input_message,
        carried_seconds=row.elapsed_seconds,
        carried_usage=usage_of_payload(row.usage_by_model),
        transcript=[SandboxTranscriptEntry.model_validate(entry) for entry in row.transcript],
        run_as=row.run_as,
    )


def approval_payload(row: RunApproval) -> dict[str, Any]:
    """One pending approval as a plain payload, for the list a reviewer reads."""
    return {
        **_request_of(row),
        "status": row.status.value,
        "version_id": row.version_id,
        "sandbox": row.sandbox,
        "created_at": row.created_at.isoformat() if row.created_at is not None else None,
    }
