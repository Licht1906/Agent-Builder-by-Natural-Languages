"""Version lifecycle: immutable `AgentVersion` rows, and the gate on publishing one.

See issue #7 / ADR-0005. `Agent` is a persistent identity; `AgentVersion` is an
**immutable** `AgentSpec` snapshot with a status; which version is live is a
**pointer**, never a flag on a version. This module is where the version state
machine and its gates live — it has no HTTP surface of its own. #14 (approval
queue) and #15 (deploy/rollback) both call these functions from their own
routes rather than re-deriving the rules; that is the "both pairs build on
top of it" the issue describes.

The pointer itself left this module in #50: it lives on `Deployment` now
(ADR-0029 §1) and `src.deployments.publication` does the write. What stayed is
`refuse_undeployable` — the rule about *what may be published*, which is a fact
about the lifecycle and not about the publication.

Two of those gates come from elsewhere and are enforced here because here is the
only way past: the model allowlist at `create_draft` (#12) and the eval score at
`request_approval` (#13). Both are decisions this module does not make and
cannot be bypassed by not asking.

There is deliberately no function that edits a saved version's `spec`. The
only way to change behaviour is `create_draft`, which always makes a new row
and never touches the deployed pointer — so refining an agent can never
alter what is already running.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.admin.config import ConfigValidationError, refuse_withdrawn_models
from src.db.models import Agent, AgentVersion, VersionStatus
from src.evals.gate import (
    NEVER_EVALUATED,
    EvalGateError,
    live_score,
    refuse_unless_eval_passed,
    unwaivable_blocker,
    version_blocker,
)
from src.models.agent_spec import AgentSpec
from src.models.workflow import NodeInstance, WorkflowJSON

_Row = TypeVar("_Row", Agent, AgentVersion)

#: Top-level `AgentSpec` fields compared field-by-field by `compare_versions`.
_COMPARED_SPEC_FIELDS = ("name", "description", "guardrails", "budget", "allowed_domains")

#: `Step` fields compared for any step id present in both versions.
_COMPARED_STEP_FIELDS = ("title", "prompt", "model", "tools", "max_iterations", "next")

#: The documents `compare_versions` covers. These are `AgentVersion` column names
#: *and* the keys a skipped half is reported under, which is why they are stated
#: once: a second list of them is how a document silently stops being reported as
#: missing on the day a third one is added.
_DOCUMENTS = ("spec", "workflow")


#: Said to a version that wants reviewing before anyone has watched it run. Named
#: rather than inlined because `submission_blocker` answers the same question
#: without acting on it, and a second copy of the sentence is how a screen and a
#: button start telling a creator two different next moves.
NOT_TRIALLED = "Version chưa chạy sandbox — không thể xin duyệt"

#: The stages a version can be sitting at when it has a runnable definition and
#: no trial behind it yet — the two doors into `sandbox_passed`.
#:
#: Two rather than one because V2 versions never pass through `draft` (#46). A
#: V1 version is `draft` from `create_draft` until a trial promotes it; a V2 one
#: reaches `workflow_confirmed` when its graph is frozen (ADR-0022 §5) and has
#: nothing to be `draft` about — its `spec` column is empty and always will be.
#:
#: A set on `mark_sandbox_passed` rather than a second function beside it: "may
#: this version be promoted on the strength of a run" is one question, and two
#: functions answering it would be two places to remember the next time a stage
#: is added. Both stages mean the same thing about the *evidence*, which is the
#: only thing the promotion is about.
AWAITING_SANDBOX: frozenset[VersionStatus] = frozenset({VersionStatus.DRAFT, VersionStatus.WORKFLOW_CONFIRMED})

#: The stages at which a V2 version's graph is immutable, and so the stages at
#: which running it means anything (#46).
#:
#: Confirmation is the event that freezes the document (ADR-0022 §5), and these
#: are the stages a version reaches by trialling and submitting it — the sequence
#: #46 creates. More than one of them, because a check for the single exact stage
#: would have let a creator run their workflow once and then be refused by the
#: very success of the run: a finished trial promotes it to `sandbox_passed`.
#:
#: `approved` joined on 2026-08-26 on a *need* — this set's own bar for
#: widening. The demo fleet's finished agents are `approved` with active
#: Deployments, and that made the evidence door 409 on exactly the versions
#: being served: the eval tab's (since-retired, ADR-0065) scenario grid read
#: its state through `runnable_workflow`, so the one stage where evidence
#: mattered most (people are talking to this graph right now) was the one stage
#: that could neither show nor grow any. The widening outlives the grid — the
#: workflow eval doors go through the same function. An approved graph is as immutable as `sandbox_passed`
#: (nothing writes `workflow` after confirmation, and the stage is reached only
#: through #14's decision), and admitting it promotes nothing by accident:
#: `mark_sandbox_passed` consults `AWAITING_SANDBOX`, which stops at
#: `workflow_confirmed`.
#:
#: `rejected` stays deliberately absent: nothing needs it — a rejected
#: version's next step is a fork, and evidence for a graph nobody will serve is
#: spend without a reader. Widening a gate on the strength of an argument
#: rather than a need is how a door ends up open for reasons nobody remembers.
#:
#: Here rather than on the route that asks it, beside `AWAITING_SANDBOX`, because
#: both are statements about what a status *means* — and the next stage anyone
#: adds should be one edit to this file rather than one here and one in the HTTP
#: layer.
WORKFLOW_RUNNABLE: frozenset[VersionStatus] = frozenset(
    {
        VersionStatus.WORKFLOW_CONFIRMED,
        VersionStatus.SANDBOX_PASSED,
        VersionStatus.PENDING_APPROVAL,
        VersionStatus.APPROVED,
    }
)

#: The stages whose graph is frozen but whose *agent* may still be taken further
#: — so an edit arriving at one of them forks (`src.workflow.drafts`).
#:
#: Every stage after confirmation, and the width is the point. It used to be the
#: single stage `workflow_confirmed`, which made three promises the platform
#: could not keep. Running the sandbox once promoted a version to
#: `sandbox_passed` and locked its graph forever. An admin rejecting a version
#: with "change this" left its creator nothing to change it with. And a deployed
#: agent could never be improved at all. None of those were decisions; they were
#: the consequence of a gate written before there was anywhere for an edit to go.
#:
#: Forking from any of them is safe for the reason the lifecycle was built the
#: way it was: a version is immutable, and which one serves is a **pointer** on
#: `Deployment` (ADR-0029 §1). A fork writes a new row and moves no pointer, so
#: the graph people are talking to right now is untouched by anybody editing it —
#: which is the guarantee ADR-0005 §2 exists to make, finally usable.
#:
#: `draft` and `spec_confirmed` are absent because neither holds a graph, and
#: `workflow_pending_confirmation` is absent because it is edited *in place* —
#: forking a draft would mint a version per save.
WORKFLOW_FORKABLE: frozenset[VersionStatus] = frozenset(
    {
        VersionStatus.WORKFLOW_CONFIRMED,
        VersionStatus.SANDBOX_PASSED,
        VersionStatus.PENDING_APPROVAL,
        VersionStatus.APPROVED,
        VersionStatus.REJECTED,
    }
)

#: Every stage the canvas accepts an edit at, whichever way the edit is made.
#:
#: One set for the mouse and the chat both, which is what keeps
#: `PUT .../workflow` and `POST .../workflow/refine` answering the same question.
#: The refine gate used to be narrower on the grounds that "letting chat through
#: while the canvas stays read-only would make a sentence do something the same
#: screen's mouse cannot" — the right rule, stated as a constant now that the
#: mouse reaches further, so the two cannot drift apart again.
WORKFLOW_EDITABLE: frozenset[VersionStatus] = WORKFLOW_FORKABLE | {VersionStatus.WORKFLOW_PENDING_CONFIRMATION}


def version_number(version: AgentVersion) -> int:
    """Which version of *its own agent* this is, counting from 1.

    `AgentVersion.id` is a primary key over the whole table, so the first version
    of a new agent is numbered by how many versions everyone else has already
    made — a creator's brand-new agent opening at "v#12" and reaching "v#13" on
    its first edit. The id is the right thing to address a version by and the
    wrong thing to show somebody; this is the number to show.

    Derived rather than stored, so there is no column that can disagree with the
    rows. Ordered by `(created_at, id)` — the order `list_versions` returns and
    the order a fork appends in — so the number a version has today is the number
    it had yesterday.
    """
    ordered = sorted(version.agent.versions, key=lambda row: (row.created_at, row.id))
    return next((position for position, row in enumerate(ordered, start=1) if row.id == version.id), len(ordered) or 1)


def wrong_stage(
    version: AgentVersion, expected: VersionStatus | Collection[VersionStatus], *, action: str
) -> str | None:
    """Why `version` may not be `action`-ed yet, or `None` when it may.

    One sentence for the whole platform's stage gates. V2 added two more gated
    documents on top of V1's pipeline — the solution spec (#42) and the workflow
    (#43) — and each gate refuses in its own module with its own exception type,
    because a caller catching "wrong stage on the spec" must not also catch
    "wrong stage on the workflow".

    What they must *not* each own is the wording. A creator meeting the same
    refusal at three stages should read the same sentence each time, and three
    copies of it drift the moment one of them is improved. Returned rather than
    raised for exactly that reason: the sentence is shared, the exception type
    stays the caller's.

    `expected` takes several stages as readily as one, because #46 produced the
    first gate that admits more than one (`AWAITING_SANDBOX`). Widening the
    shared sentence rather than letting that one caller write its own is the
    whole rule above applied to itself: a fourth hand-rolled copy would be the
    copy that drifts.
    """
    allowed = frozenset({expected} if isinstance(expected, VersionStatus) else expected)
    if version.status in allowed:
        return None
    # One name reads as a requirement, several read as a choice, and a reader
    # met with "cần {'draft'}" would be reading Python rather than a sentence.
    named = (
        repr(next(iter(allowed)).value)
        if len(allowed) == 1
        else "một trong: " + ", ".join(sorted(status.value for status in allowed))
    )
    return f"Không thể {action}: version đang ở trạng thái {version.status.value!r}, cần {named}"


class VersionLifecycleError(ValueError):
    """An invalid state transition or gate violation. Message is user-facing."""


@dataclass(frozen=True, slots=True)
class EvalOverride:
    """An admin's decision to let one version past the eval bar, and why — see #14 / ADR-0012.

    The safety valve #14 asks for. The eval judge is an LLM and it wobbles, so a
    single unlucky grading would otherwise dead-end a version with no way
    forward; this is that way forward, and it is a decision a person signs for.

    A type rather than a `bypass_eval: bool`, and that is the whole design. A
    flag can be passed by a route that forgot to collect a reason, and the reason
    is not paperwork — it is the audit artifact the ticket asks to keep. Here the
    reason cannot be omitted, because there is no way to build the permission
    without one.
    """

    admin_id: int
    reason: str

    def __post_init__(self) -> None:
        reason = self.reason.strip()
        if not reason:
            raise VersionLifecycleError("Vượt rào ngưỡng eval phải kèm lý do — lý do này là bản ghi kiểm toán")
        object.__setattr__(self, "reason", reason)


def _require_status(version: AgentVersion, expected: VersionStatus | Collection[VersionStatus], *, action: str) -> None:
    """Refuse unless `version` is at a stage where `action` is allowed.

    The sentence comes from `wrong_stage`, shared with the spec's gate (#42) and
    the workflow's (#43), rather than being formatted here — this module's own
    rule about wording, obeyed by the module that wrote it.
    """
    refusal = wrong_stage(version, expected, action=action)
    if refusal is not None:
        raise VersionLifecycleError(refusal)


def _persist(db: Session, row: _Row) -> _Row:
    """Commit and refresh — the tail every mutating function in this module ends with."""
    db.commit()
    db.refresh(row)
    return row


def create_draft(db: Session, agent: Agent, spec: AgentSpec) -> AgentVersion:
    """A new `draft` version holding `spec`. Never changes which version is published.

    This is the only path that lets a spec's content change — editing "in
    place" does not exist. Calling this on an agent that already has a
    deployed version leaves that version, and the pointer to it, untouched.

    Being the only such path is also what makes the admin allowlist bite: the
    spec is re-checked here against the *live* configuration, so a model an admin
    has since removed cannot be written into a new version by any caller,
    present or future (#12, ADR-0010 §4).
    """
    try:
        refuse_withdrawn_models(db, spec)
    except ConfigValidationError as exc:
        raise VersionLifecycleError(f"Không thể lưu version: {exc}") from None
    version = AgentVersion(agent_id=agent.id, spec=spec.model_dump(mode="json"), status=VersionStatus.DRAFT)
    db.add(version)
    return _persist(db, version)


def mark_sandbox_passed(db: Session, version: AgentVersion) -> AgentVersion:
    """`draft` or `workflow_confirmed` -> `sandbox_passed`, once a run has finished.

    Called by both sandboxes — V1's (#9, `src.agents.sandbox`) and V2's (#46,
    `src.workflow.sandbox`) — because both produce the same thing: a finished
    transcript of a definition nobody has approved. Which of the two stages the
    version came from is `AWAITING_SANDBOX`'s business, not this function's.

    Refusing anything else is what keeps a re-run from dragging a version
    backwards out of the approval queue; the callers ask `awaiting_sandbox`
    first so an ordinary retry is not an error at all.
    """
    _require_status(version, AWAITING_SANDBOX, action="đánh dấu đã chạy sandbox")
    version.status = VersionStatus.SANDBOX_PASSED
    return _persist(db, version)


def awaiting_sandbox(version: AgentVersion) -> bool:
    """Whether a finished run would move `version` on, or find it already past that point.

    The question form of the gate above, for the same reason `submission_blocker`
    is the question form of `request_approval`: a sandbox module asks it before
    promoting, so a creator re-running a version that is already waiting for
    approval gets an ordinary successful run rather than an exception raised
    behind a stream that has already closed.
    """
    return version.status in AWAITING_SANDBOX


def request_approval(db: Session, version: AgentVersion, *, eval_override: EvalOverride | None = None) -> AgentVersion:
    """`sandbox_passed -> pending_approval`, and only if eval says so.

    A version that never ran sandbox is rejected with an explanation, so the
    admin always has a transcript to read (issue #2) — checked before the
    generic status check so a `draft` gets the specific reason, not a bare
    state-name mismatch.

    The eval gate (#13) comes last, and it is the reason this function takes a
    `Session`. Being *here* rather than on the endpoint is what makes it
    unavoidable: `request_approval` is the only door into the approval queue, so
    #14's routes inherit the bar instead of re-deriving it — the same argument
    ADR-0010 §4 makes for the allowlist at `create_draft`.

    `eval_override` is the one sanctioned way past that bar, and it is deliberately
    a parameter of *this* function rather than a second door beside it: a bypass
    that lived on its own would be a second answer to "may this version be
    queued", and the sandbox check above would then have two places to be
    remembered. It waives the eval score and nothing else — see `_refuse_pointless_override`
    for the two cases where there is no score to waive.

    Since #48 there is one thing it does not waive either, and it is checked
    **before** the branch so no path can miss it: a case that failed on a rule
    read straight off the run's transcript — a forbidden action called, a guarded
    action fired after a refusal — blocks whoever is asking (ADR-0028 §6).
    ADR-0011 §7's valve was always scoped to a judge that wobbles, and a
    deterministic fact does not wobble. This is a widening of the gate rather
    than a contradiction of it: rule violations did not exist as a case type
    until ADR-0028.
    """
    if awaiting_sandbox(version):
        raise VersionLifecycleError(NOT_TRIALLED)
    _require_status(version, VersionStatus.SANDBOX_PASSED, action="xin duyệt")
    unwaivable = unwaivable_blocker(db, version.id)
    if unwaivable is not None:
        raise VersionLifecycleError(f"Không thể xin duyệt: {unwaivable}")
    if eval_override is None:
        try:
            refuse_unless_eval_passed(db, version.id)
        except EvalGateError as exc:
            raise VersionLifecycleError(f"Không thể xin duyệt: {exc}") from None
    else:
        _refuse_pointless_override(db, version)
    version.status = VersionStatus.PENDING_APPROVAL
    return _persist(db, version)


def submission_blocker(db: Session, version: AgentVersion) -> str | None:
    """Why `request_approval` would refuse `version` right now, or `None` if it would not.

    The question form of the transition, so a screen can show the next move
    before a creator presses a button and is refused — the argument
    `src.evals.gate.blocked_reason` makes, extended to the gate that comes before
    it. Both answers come from the same two checks `request_approval` runs, in
    the same order, so the sentence on the screen is the sentence the button
    would have given.

    Which of the two gates is quoted matters. A version that has not been
    trialled is told *that*, **not** that its eval is missing: both are true and
    only the first is the thing to go and do. That was the whole point of
    `request_approval` special-casing the pre-trial stages (ADR-0005 §1), and a
    status field that answered with the eval sentence would undo it. Both
    pre-trial stages, V1's and V2's, for the reason `AWAITING_SANDBOX` exists.

    `None` for any other status. "Why can you not submit this" is not a question
    about a version that has already been submitted, approved or rejected, and
    inventing an answer for it would put a blocker on a screen beside a version
    that is not blocked.
    """
    if awaiting_sandbox(version):
        return NOT_TRIALLED
    if version.status is not VersionStatus.SANDBOX_PASSED:
        return None
    return version_blocker(db, version.id)


def _refuse_pointless_override(db: Session, version: AgentVersion) -> None:
    """Refuse an override where there was no bar in the way.

    Two such cases, and both are refused so that every recorded override is a bar
    that genuinely blocked something. An audit trail whose entries sometimes
    describe decisions nobody had to make is one nobody can read as evidence.

    A version that never ran eval is the more important of the two. The valve
    answers a *wobbling judge*, and a judge that never ran did not wobble —
    letting it through here would quietly turn "the grader was unlucky" into "we
    skipped grading", which is a much larger decision and one #14 never asks for.
    """
    score = live_score(db, version.id)
    if score is None:
        raise VersionLifecycleError(f"Không thể vượt rào: {NEVER_EVALUATED}")
    if score.meets_threshold:
        raise VersionLifecycleError(
            f"Không thể vượt rào: eval gần nhất đạt {score.passed_count}/{score.total_count} case "
            f"và đã đạt ngưỡng {score.threshold:.2f} — version này xin duyệt được mà không cần vượt rào"
        )


def approve(db: Session, version: AgentVersion) -> AgentVersion:
    """`pending_approval -> approved`."""
    _require_status(version, VersionStatus.PENDING_APPROVAL, action="duyệt")
    version.status = VersionStatus.APPROVED
    return _persist(db, version)


def reject(db: Session, version: AgentVersion, reason: str) -> AgentVersion:
    """`pending_approval -> rejected`. `reason` is mandatory and stored verbatim."""
    if not reason.strip():
        raise VersionLifecycleError("Từ chối phải kèm lý do")
    _require_status(version, VersionStatus.PENDING_APPROVAL, action="từ chối")
    version.status = VersionStatus.REJECTED
    version.rejection_reason = reason.strip()
    return _persist(db, version)


def refuse_undeployable(agent: Agent, version: AgentVersion) -> None:
    """Refuse unless `version` is something `agent` may actually publish.

    The gate that used to sit inside `set_deployed_version`, kept here after #50
    moved the pointer itself onto `Deployment` (ADR-0029 §1). What may be
    published is a fact about the version lifecycle — never a draft, never
    somebody else's version — so it stays in the module that owns the lifecycle
    and `src.deployments.publication` inherits it, the same way #14's routes
    inherit the approval gates instead of restating them.

    Raising rather than returning a sentence, unlike `wrong_stage`: there is one
    caller and it is a write, not a screen asking what would happen.
    """
    if version.agent_id != agent.id:
        raise VersionLifecycleError("Version không thuộc về agent này")
    if version.status is not VersionStatus.APPROVED:
        raise VersionLifecycleError(
            f"Chỉ được trỏ tới version đã duyệt, version này đang ở trạng thái {version.status.value!r}"
        )


def list_versions(db: Session, agent: Agent) -> list[AgentVersion]:
    """Every version of `agent`, oldest first — which one is published is the Deployment's pointer."""
    return list(
        db.execute(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent.id)
            .order_by(AgentVersion.created_at, AgentVersion.id)
        ).scalars()
    )


@dataclass(frozen=True, slots=True)
class SpecComparison:
    """How two versions' executable pipelines differ — V1's document (ADR-0001).

    `changed_fields` and the per-step diffs hold `(old, new)` pairs, keyed by
    field name — only fields that actually differ are present.
    """

    changed_fields: dict[str, tuple[object, object]]
    added_step_ids: tuple[str, ...]
    removed_step_ids: tuple[str, ...]
    changed_steps: dict[str, dict[str, tuple[object, object]]]


@dataclass(frozen=True, slots=True)
class EdgeRef:
    """One edge by what it *means*, which is not what it is keyed by.

    An `Edge.id` is minted by the canvas and carries nothing: re-drawing a
    connection between the same two ports produces a new id for a graph that
    behaves identically. Diffing on the id would report that as an edge removed
    and an edge added, and a creator reading it would go looking for a rewiring
    that never happened. So the identity used here is the triple the runtime
    actually follows.
    """

    source_node_id: str
    source_port: str
    target_node_id: str


@dataclass(frozen=True, slots=True)
class NodeDiff:
    """How one node differs between two versions of a workflow.

    `config` is diffed as a whole value *and* summarised by
    `changed_config_keys`, and the pair is deliberate. The mapping's shape is
    per node type and is not checked until the confirm gate
    (`src.workflow.validation`), so this module has no business taking a
    half-filled draft's config apart into fields it cannot name. What it can say
    without inventing anything is which keys moved — which is the sentence a
    creator reads — while both whole values travel alongside for the screen that
    wants to show them.
    """

    changed_fields: dict[str, tuple[object, object]]
    changed_config_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkflowComparison:
    """How two versions' graphs differ — V2's document (ADR-0022)."""

    changed_fields: dict[str, tuple[object, object]]
    added_node_ids: tuple[str, ...]
    removed_node_ids: tuple[str, ...]
    changed_nodes: dict[str, NodeDiff]
    added_edges: tuple[EdgeRef, ...]
    removed_edges: tuple[EdgeRef, ...]


@dataclass(frozen=True, slots=True)
class VersionComparison:
    """The difference between two versions of the same agent, document by document.

    Two halves rather than one flat diff, because a version carries up to three
    documents and each has its own stage gate (ADR-0020 §5): a V1 version holds a
    `spec` and never a `workflow`, a V2 one holds a `workflow` and never a
    `spec`. A single merged shape would have to pretend a workflow's nodes were
    a pipeline's steps, which is the one thing they are not.

    `None` on a half means neither version had that document — nothing happened
    there, and there is nothing to report. `skipped` is the other case: exactly
    one of them had it, so the half could not be diffed and the reason is
    carried by name rather than being dropped on the floor.
    """

    spec: SpecComparison | None
    workflow: WorkflowComparison | None
    skipped: dict[str, str]


def _held_by_both(version_a: AgentVersion, version_b: AgentVersion, document: str) -> bool:
    """Whether both versions carry `document`, and so whether it can be diffed at all.

    `is not None` and not truthiness, which is the same test `_absent_from` makes
    — deliberately, because the two answers have to agree. An empty document is
    still a document the version reached the stage of having, and a pair where
    one of them read as "absent" here and "present" there would fall through to
    the refusal below carrying no reason for it.
    """
    return getattr(version_a, document) is not None and getattr(version_b, document) is not None


def _absent_from(version_a: AgentVersion, version_b: AgentVersion, document: str) -> str | None:
    """Why `document` cannot be diffed across this pair, or `None` when the question does not arise.

    Only the *lopsided* case is a reason. Two versions that both lack a document
    have nothing to say about it — that is the ordinary state of every V1 pair
    asked about workflows — and reporting it would put a refusal on a screen
    beside a comparison that worked perfectly.
    """
    lacking = [version.id for version in (version_a, version_b) if getattr(version, document) is None]
    if len(lacking) != 1:
        return None
    return f"Version {lacking[0]} chưa có {document}"


def _spec_comparison(version_a: AgentVersion, version_b: AgentVersion) -> SpecComparison:
    """Diff two versions' `AgentSpec`s. Both are known to have one."""
    spec_a = AgentSpec.model_validate(version_a.spec)
    spec_b = AgentSpec.model_validate(version_b.spec)

    changed_fields = {
        field: (getattr(spec_a, field), getattr(spec_b, field))
        for field in _COMPARED_SPEC_FIELDS
        if getattr(spec_a, field) != getattr(spec_b, field)
    }

    steps_a = {step.id: step for step in spec_a.steps}
    steps_b = {step.id: step for step in spec_b.steps}

    changed_steps: dict[str, dict[str, tuple[object, object]]] = {}
    for step_id in sorted(set(steps_a) & set(steps_b)):
        step_a, step_b = steps_a[step_id], steps_b[step_id]
        field_diffs = {
            field: (getattr(step_a, field), getattr(step_b, field))
            for field in _COMPARED_STEP_FIELDS
            if getattr(step_a, field) != getattr(step_b, field)
        }
        if field_diffs:
            changed_steps[step_id] = field_diffs

    return SpecComparison(
        changed_fields=changed_fields,
        added_step_ids=tuple(sorted(set(steps_b) - set(steps_a))),
        removed_step_ids=tuple(sorted(set(steps_a) - set(steps_b))),
        changed_steps=changed_steps,
    )


def _workflow_fields(workflow: WorkflowJSON) -> dict[str, object]:
    """The graph's whole-value settings, flattened out of `metadata`.

    Flattened because `metadata` is a container and not a thing a creator
    changed: a diff reporting that `metadata` differs would name the box rather
    than what moved inside it. The budget is dumped to plain JSON on the way out
    so the pair a screen renders holds numbers rather than a model it would have
    to know how to unwrap.
    """
    return {
        "start_node_id": workflow.start_node_id,
        "budget": workflow.metadata.budget.model_dump(mode="json"),
        "template_id": workflow.metadata.template_id,
    }


def _node_diff(node_a: NodeInstance, node_b: NodeInstance) -> NodeDiff | None:
    """How one node changed, or `None` if nothing about it did.

    `position` is not compared, and its absence is the decision. Dragging a node
    across the canvas changes where it is drawn and nothing about what it does;
    a diff that reported it would fill a creator's screen with the one edit that
    never needs reviewing, and bury the ones that do.
    """
    changed_fields: dict[str, tuple[object, object]] = {}
    if node_a.type is not node_b.type:
        changed_fields["type"] = (node_a.type.value, node_b.type.value)

    moved_keys = tuple(
        sorted(
            key for key in set(node_a.config) | set(node_b.config) if node_a.config.get(key) != node_b.config.get(key)
        )
    )
    if moved_keys:
        changed_fields["config"] = (dict(node_a.config), dict(node_b.config))

    if not changed_fields:
        return None
    return NodeDiff(changed_fields=changed_fields, changed_config_keys=moved_keys)


def _edge_refs(workflow: WorkflowJSON) -> set[EdgeRef]:
    """Every connection in `workflow`, keyed by where it runs from and to."""
    return {EdgeRef(edge.source_node_id, edge.source_port, edge.target_node_id) for edge in workflow.edges}


def _sorted_edges(edges: set[EdgeRef]) -> tuple[EdgeRef, ...]:
    """`edges` in an order that does not change between two identical comparisons."""
    return tuple(sorted(edges, key=lambda edge: (edge.source_node_id, edge.source_port, edge.target_node_id)))


def _workflow_comparison(version_a: AgentVersion, version_b: AgentVersion) -> WorkflowComparison:
    """Diff two versions' `WorkflowJSON`s. Both are known to have one."""
    workflow_a = WorkflowJSON.model_validate(version_a.workflow)
    workflow_b = WorkflowJSON.model_validate(version_b.workflow)

    fields_a, fields_b = _workflow_fields(workflow_a), _workflow_fields(workflow_b)
    changed_fields = {
        field: (fields_a[field], fields_b[field]) for field in fields_a if fields_a[field] != fields_b[field]
    }

    nodes_a = {node.id: node for node in workflow_a.nodes}
    nodes_b = {node.id: node for node in workflow_b.nodes}
    changed_nodes = {
        node_id: diff
        for node_id in sorted(set(nodes_a) & set(nodes_b))
        if (diff := _node_diff(nodes_a[node_id], nodes_b[node_id])) is not None
    }

    edges_a, edges_b = _edge_refs(workflow_a), _edge_refs(workflow_b)

    return WorkflowComparison(
        changed_fields=changed_fields,
        added_node_ids=tuple(sorted(set(nodes_b) - set(nodes_a))),
        removed_node_ids=tuple(sorted(set(nodes_a) - set(nodes_b))),
        changed_nodes=changed_nodes,
        added_edges=_sorted_edges(edges_b - edges_a),
        removed_edges=_sorted_edges(edges_a - edges_b),
    )


def compare_versions(version_a: AgentVersion, version_b: AgentVersion) -> VersionComparison:
    """Diff two versions of the same agent, across every document they both hold.

    Both documents are re-validated on the way in rather than diffed as stored
    JSON — the defence in depth ADR-0003 §7 asks of every reader, and the reason
    a graph re-keyed by the canvas can be recognised as unchanged at all: the
    comparison is between two *documents*, not between two blobs.

    Refuses only when there is nothing to compare. A pair with one document
    between them, or none, is a question with no answer rather than an internal
    error, and saying so by name is what keeps it out of a 500.
    """
    if version_a.agent_id != version_b.agent_id:
        raise VersionLifecycleError("Chỉ so sánh được hai version của cùng một agent")

    skipped = {
        document: reason
        for document in _DOCUMENTS
        if (reason := _absent_from(version_a, version_b, document)) is not None
    }
    spec = _spec_comparison(version_a, version_b) if _held_by_both(version_a, version_b, "spec") else None
    workflow = _workflow_comparison(version_a, version_b) if _held_by_both(version_a, version_b, "workflow") else None

    if spec is None and workflow is None:
        raise VersionLifecycleError(
            "; ".join(skipped.values())
            if skipped
            else f"Version {version_a.id} và {version_b.id} chưa có tài liệu nào để so sánh"
        )
    return VersionComparison(spec=spec, workflow=workflow, skipped=skipped)
