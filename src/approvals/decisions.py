"""The three things an admin can decide, and the record each one leaves.

See issue #14 / ADR-0012. Each function here does exactly two things: it makes
the lifecycle transition (by calling `src.agents.versions`, never by writing a
status itself) and it writes the `ApprovalDecision` row that says who decided
what and why.

The two happen in one transaction, deliberately. A row without its transition
would be a history of things that were attempted; a transition without its row
would be a decision nobody can be held to. So the transition is attempted
*first* and the row is only added once it has been accepted — a refused approval
leaves nothing behind.

Reasons are mandatory where #14 says they are, and they are mandatory in one
place: `src.agents.versions` refuses a rejection or an override without one, and
these functions inherit that rather than re-checking. A second check would be a
second wording of the same refusal, and eventually a second opinion about what
counts as empty.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents import versions
from src.agents.versions import EvalOverride
from src.db.models import AgentVersion, ApprovalDecision, ApprovalDecisionKind, User
from src.evals.gate import live_score
from src.evals.results import Score


def approve_version(db: Session, version: AgentVersion, *, admin: User) -> ApprovalDecision:
    """Let `version` run for real. `pending_approval -> approved`.

    No reason is required, and that asymmetry with `reject_version` is on
    purpose: a rejection has to tell the creator what to change, while an
    approval has nothing for them to act on. What an approval does have to leave
    behind is *who* and *when*, and the row carries both.
    """
    versions.approve(db, version)
    return _record(db, version, admin=admin, kind=ApprovalDecisionKind.APPROVED, reason=None)


def reject_version(db: Session, version: AgentVersion, *, admin: User, reason: str) -> ApprovalDecision:
    """Send `version` back with a reason. `pending_approval -> rejected`.

    The reason lands in two places on purpose. `AgentVersion.rejection_reason` is
    where the creator's own version list already reads it from (#7), and the
    decision row is where the history keeps it — including the earlier rejections
    that a later one would otherwise overwrite.
    """
    version = versions.reject(db, version, reason)
    return _record(db, version, admin=admin, kind=ApprovalDecisionKind.REJECTED, reason=version.rejection_reason)


def override_eval_bar(db: Session, version: AgentVersion, *, admin: User, reason: str) -> ApprovalDecision:
    """Waive the eval threshold for `version`. `sandbox_passed -> pending_approval`.

    The safety valve, and the only one. It opens the eval gate and nothing else:
    a version that has never been trialled is still refused, and so is one that
    has nothing to waive (`src.agents.versions._refuse_pointless_override`).

    Note what it does *not* do: it does not approve anything. The version lands
    in the queue, and an admin — the same one or another — still has to read the
    dossier and decide. Waiving the machine's verdict is not the same act as
    accepting the agent, and collapsing the two would make the valve a way to
    skip the review rather than a way to reach it.
    """
    score = live_score(db, version.id)
    # Built first, so an override with no reason is refused before anything is
    # read or written — and the reason recorded below is the one the permission
    # was actually granted with, not a second normalisation of the same string.
    override = EvalOverride(admin_id=admin.id, reason=reason)
    versions.request_approval(db, version, eval_override=override)
    return _record(
        db,
        version,
        admin=admin,
        kind=ApprovalDecisionKind.EVAL_OVERRIDE,
        reason=override.reason,
        score=score,
    )


def history_for(db: Session, version_id: int) -> list[ApprovalDecision]:
    """Every decision ever made about `version_id`, oldest first.

    Oldest first because the history is a story — overridden, then rejected, then
    resubmitted and approved — and a story read backwards is a list of unrelated
    events. Nothing ever deletes from it, so a version that went round the loop
    three times shows all three laps.
    """
    return list(
        db.execute(
            select(ApprovalDecision)
            .where(ApprovalDecision.version_id == version_id)
            .order_by(ApprovalDecision.created_at, ApprovalDecision.id)
        ).scalars()
    )


def score_of(decision: ApprovalDecision) -> Score | None:
    """The eval score as it stood when `decision` was made, or `None` if there was none.

    Read back out of the snapshot rather than looked up again. Looking it up
    would answer a different question — the gate reads the *latest* run, so a
    creator who evaluated again afterwards would silently rewrite what the
    decision appears to have been made against.
    """
    payload = decision.eval_score
    if payload is None:
        return None
    return Score(
        passed_count=int(payload["passed_count"]),
        total_count=int(payload["total_count"]),
        threshold=float(payload["threshold"]),
    )


def _record(
    db: Session,
    version: AgentVersion,
    *,
    admin: User,
    kind: ApprovalDecisionKind,
    reason: str | None,
    score: Score | None = None,
) -> ApprovalDecision:
    """Write one decision down. Only ever called after its transition was accepted.

    `score` is passed in by the one caller that has to read it *before* it acts —
    an override changes nothing about the score, but it does change the version's
    status, and reading afterwards would be reading around a moved part. The
    other two read it here, where the number is still whatever the decision was
    made against.
    """
    decision = ApprovalDecision(
        version_id=version.id,
        admin_id=admin.id,
        kind=kind,
        reason=reason,
        eval_score=_score_payload(score if score is not None else live_score(db, version.id)),
    )
    db.add(decision)
    db.commit()
    db.refresh(decision)
    return decision


def _score_payload(score: Score | None) -> dict[str, Any] | None:
    """A score flattened for the JSON column. `None` stays `None` — never a zero."""
    if score is None:
        return None
    return {
        "passed_count": score.passed_count,
        "total_count": score.total_count,
        "threshold": score.threshold,
    }
