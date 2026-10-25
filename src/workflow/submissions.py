"""Creator "save as template" submissions, and the admin queue that reviews them — #53, ADR-0025 §6-8.

`src.workflow.templates` is the *catalog*: a code-defined seed plus, from here
on, the DB-backed half `approved_templates`/`template_of` were always written to
join. This module is the other half — how a `TemplateSubmission` row is born,
how it is decided, and how a decided row turns back into the one `TemplateSpec`
shape both halves of the registry share.

**Only from `workflow_confirmed`, never a freehand or LLM-drawn graph** (ADR-0025
§7). The graph a submission copies has already passed the ADR-0021/ADR-0023
graph validator — cycle, fan-out, reachability, `HumanApproval` in front of every
high-risk `MCP` path — at `src.workflow.drafts.confirm_workflow`. A standalone
"describe your template" form or an LLM skeleton would reopen exactly the gap
that validator exists to close, which is why `submit_template` takes a version
and a stage check rather than a graph.

**The copy is literal, taken once.** `submit_template` calls `strip_for_submission`
against the version's `WorkflowJSON` at the moment of submission and never reads
that version again — editing or deleting the source workflow afterward does not
touch the row here, and approving, rejecting or disabling the row never touches
any `WorkflowJSON` a Generator run already produced from it while it was
`approved`. Same guarantee `TemplateSpec.skeleton` gives the built-in half, kept
by never storing a reference back to `AgentVersion.workflow` — only a copy of it.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import AgentVersion, TemplateSubmission, VersionStatus
from src.models.solution_spec import DataSourceKind
from src.models.workflow import Edge, NodeInstance, WorkflowJSON
from src.workflow.generator import workflow_of
from src.workflow.templates import TemplateSource, TemplateSpec, TemplateStatus, strip_for_submission


class TemplateSubmissionError(ValueError):
    """A "save as template" ask, or a review decision, that the stage rules refuse. Message is user-facing."""


def submit_template(
    db: Session,
    version: AgentVersion,
    *,
    submitted_by: int,
    name: str,
    description: str,
    required_data_kinds: Sequence[DataSourceKind] = (),
) -> TemplateSubmission:
    """Copy `version`'s confirmed graph into a new `pending` `TemplateSubmission`.

    Refuses anything other than `workflow_confirmed` (ADR-0025 §7, criterion 1)
    — a version still on the canvas has not passed the graph validator, and a
    draft with an unwired port or a blank prompt is not a shape to hand other
    creators as a starting point.

    `name`/`description`/`required_data_kinds` are the creator's own words, the
    same three fields a built-in `TemplateSpec` carries beside its graph — there
    is nothing in a stripped skeleton a rule could derive them from, and
    `description` in particular is what the Generator's LLM will read to decide
    whether this template fits a future spec (ADR-0025 §1).
    """
    if version.status is not VersionStatus.WORKFLOW_CONFIRMED:
        raise TemplateSubmissionError(
            "Chỉ lưu được thành template từ workflow đã xác nhận (workflow_confirmed) — "
            f"version này đang ở trạng thái {version.status.value!r}"
        )
    workflow = workflow_of(version)
    row = TemplateSubmission(
        submitted_by=submitted_by,
        source_version_id=version.id,
        name=name,
        description=description,
        nodes=[node.model_dump(mode="json") for node in strip_for_submission(workflow.nodes)],
        edges=[edge.model_dump(mode="json") for edge in workflow.edges],
        start_node_id=workflow.start_node_id,
        required_data_kinds=[kind.value for kind in required_data_kinds],
        status=TemplateStatus.PENDING,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def pending_submissions(db: Session) -> Sequence[TemplateSubmission]:
    """Every submission waiting on an admin, oldest first — the review queue criterion 4 asks for."""
    statement = (
        select(TemplateSubmission)
        .where(TemplateSubmission.status == TemplateStatus.PENDING)
        .order_by(TemplateSubmission.created_at)
    )
    return db.execute(statement).scalars().all()


def approve_submission(db: Session, row: TemplateSubmission, *, admin_id: int) -> TemplateSubmission:
    """`pending -> approved`. From this moment the Generator may propose it (criterion 5).

    Refuses anything other than `pending` — an admin re-deciding an already-
    decided row, or approving a row they meant to disable, is a mistake this
    function is the one place that can catch before it happens silently.
    """
    _require_pending(row, action="duyệt")
    row.status = TemplateStatus.APPROVED
    row.decided_by = admin_id
    row.decided_at = _now()
    return _persist(db, row)


def reject_submission(db: Session, row: TemplateSubmission, *, admin_id: int, reason: str) -> TemplateSubmission:
    """`pending -> rejected`, with a reason its creator will read. Never joins the candidate pool."""
    _require_pending(row, action="từ chối")
    if not reason.strip():
        raise TemplateSubmissionError("Từ chối template phải kèm lý do")
    row.status = TemplateStatus.REJECTED
    row.decided_by = admin_id
    row.decided_at = _now()
    row.rejection_reason = reason
    return _persist(db, row)


def disable_submission(db: Session, row: TemplateSubmission, *, admin_id: int) -> TemplateSubmission:
    """`approved -> disabled`. Removes it from future selection; touches no workflow already produced.

    The other half of criterion 5: disabling is not a stronger rejection, it is a
    separate transition that only ever starts from `approved` — a `pending` row
    is disabled by being rejected, and a row already `disabled` or `rejected` has
    nothing left for this to do. `approved_submission_specs` is what actually
    enforces "removed from the pool"; this function only flips the status it
    reads.
    """
    if row.status is not TemplateStatus.APPROVED:
        raise TemplateSubmissionError(
            f"Chỉ tắt được template đang ở trạng thái approved — template này đang {row.status.value!r}"
        )
    row.status = TemplateStatus.DISABLED
    row.decided_by = admin_id
    row.decided_at = _now()
    return _persist(db, row)


def approved_submission_specs(db: Session) -> Sequence[TemplateSpec]:
    """Every creator-submitted template the Generator may currently propose.

    Read fresh on every call rather than cached: `approved_templates` (the
    caller) is asked once per Generator run, and a row an admin just disabled
    must stop being offered on the very next run, not eventually.
    """
    statement = select(TemplateSubmission).where(TemplateSubmission.status == TemplateStatus.APPROVED)
    return [_spec_of(row) for row in db.execute(statement).scalars().all()]


def approved_submission_spec_of(db: Session, template_id: str) -> TemplateSpec | None:
    """The approved submission named `template_id`, or `None` — `template_of`'s DB-backed half."""
    submission_id = _submission_id_of(template_id)
    if submission_id is None:
        return None
    row = db.get(TemplateSubmission, submission_id)
    if row is None or row.status is not TemplateStatus.APPROVED:
        return None
    return _spec_of(row)


def template_id_of(row: TemplateSubmission) -> str:
    """The `TemplateSpec.id`/`WorkflowJSON.metadata.template_id` a submission is addressed by.

    Namespaced against the seven built-in ids (`document_qa`, ...), which are
    bare words with no separator of their own — a creator naming a submission
    `"document_qa"` must not collide with the seed entry of that name.
    """
    return f"submission-{row.id}"


def _submission_id_of(template_id: str) -> int | None:
    """The row id `template_id_of` encoded, or `None` if `template_id` is not one of ours."""
    prefix = "submission-"
    if not template_id.startswith(prefix):
        return None
    try:
        return int(template_id[len(prefix) :])
    except ValueError:
        return None


def _spec_of(row: TemplateSubmission) -> TemplateSpec:
    """`row` read back as the one `TemplateSpec` shape the built-in seed also uses.

    `nodes`/`edges` are re-validated through `NodeInstance`/`Edge` on the way
    out, the same defence in depth `workflow_of` gives a stored `WorkflowJSON` —
    a row written before a validator existed must not reach the Generator
    unchecked either.
    """
    return TemplateSpec(
        id=template_id_of(row),
        name=row.name,
        description=row.description,
        nodes=[NodeInstance.model_validate(node) for node in row.nodes],
        edges=[Edge.model_validate(edge) for edge in row.edges],
        start_node_id=row.start_node_id,
        required_data_kinds=[DataSourceKind(kind) for kind in row.required_data_kinds],
        source=TemplateSource.CREATOR_SUBMITTED,
        status=row.status,
    )


def _require_pending(row: TemplateSubmission, *, action: str) -> None:
    if row.status is not TemplateStatus.PENDING:
        raise TemplateSubmissionError(
            f"Chỉ {action} được template đang chờ duyệt (pending) — template này đang {row.status.value!r}"
        )


def _now() -> datetime:
    """A naive UTC timestamp, matching every other `decided_at` column in this codebase."""
    return datetime.now(UTC).replace(tzinfo=None)


def _persist(db: Session, row: TemplateSubmission) -> TemplateSubmission:
    db.commit()
    db.refresh(row)
    return row


def skeleton_of(row: TemplateSubmission) -> WorkflowJSON:
    """`row`'s stripped graph as a `WorkflowJSON` — what the admin review queue shows for criterion 4."""
    return _spec_of(row).skeleton()
