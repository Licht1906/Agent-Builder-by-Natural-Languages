"""What is waiting on an admin — and what is stuck waiting on an admin.

See issue #14 / ADR-0012 §4. The obvious reading of "hàng chờ các version đang
xin duyệt" is the `pending_approval` versions, and that is most of this list.

The other half is the reason the list is not only that. A version whose eval fell
short cannot submit itself — `request_approval` refuses it (ADR-0011 §7) — so it
never becomes `pending_approval` and would never appear here. But the override
that #14 gives an admin exists precisely for that version, and an admin who
cannot see it has no way to reach the button. Listing it, marked as blocked
rather than as waiting, is what makes the safety valve reachable in the product
rather than only in the API.

A version that has *never* run eval is deliberately not here. Nothing is blocking
it that an admin can unblock — the work is the creator's — and `request_approval`
refuses an override on it anyway. Showing it would offer a door that does not
open.

**And what an admin has already settled** (ADR-0064). `decided_versions` is the
other list this screen needs, and it is a different list rather than a wider one:
a waiting row is picked by risk and by how long it has waited, a settled row is
read for *what was decided and by whom*, and neither set of fields means anything
on the other's rows. They live in one module because they are two readings of the
same table by the same person on the same screen — a second module would be the
place the next status-shaped question gets asked twice.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.approvals.decisions import score_of
from src.approvals.dossier import requested_actions, requested_tools
from src.approvals.states import QueueState
from src.db.models import Agent, AgentVersion, ApprovalDecision, ApprovalDecisionKind, User, VersionStatus
from src.evals.gate import blocked_reason, live_score, version_blocker
from src.evals.results import Score
from src.models.agent_spec import AgentSpec
from src.models.workflow import WorkflowJSON

#: The two statuses a review ends at. `approve` and `reject` both refuse anything
#: but `pending_approval` and a rejected version forks rather than resubmits
#: (`src.agents.versions.WORKFLOW_FORKABLE`), so reaching either of these is the
#: end of *this* version's review — which is what makes the dossier behind one of
#: them safe to open read-only.
DECIDED_STATUSES: tuple[VersionStatus, ...] = (VersionStatus.APPROVED, VersionStatus.REJECTED)


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """One row of the queue — enough to choose what to open, and nothing more.

    `high_risk_tools` is on the row rather than only in the dossier because it is
    what an admin picks by: given six versions and an afternoon, the one asking
    to send email is the one to read first. Everything else about the tools
    (which step, what the tool does) waits for the dossier.
    """

    version_id: int
    agent_id: int
    agent_name: str
    owner_email: str
    state: QueueState
    score: Score | None
    #: Why this version cannot ask for approval, in the gate's own words. `None`
    #: for anything already in the queue — those are past that bar.
    blocked_reason: str | None
    high_risk_tools: tuple[str, ...]
    created_at: datetime


def approval_queue(db: Session) -> list[QueueEntry]:
    """Everything an admin has to look at, oldest first.

    Oldest first because a queue that put the newest arrival on top would starve
    whoever has waited longest — which is the failure mode of every review queue
    that sorts by recency.

    One query for the rows, then per-version work in Python: the score and the
    tool risk both need the *live* configuration and the tool registry, neither
    of which the database knows about. The set being iterated is what is
    currently waiting on a person, so it is bounded by how fast an admin works
    rather than by how much the platform has ever built.
    """
    rows = db.execute(
        select(AgentVersion, Agent, User)
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .join(User, User.id == Agent.owner_id)
        .where(AgentVersion.status.in_([VersionStatus.PENDING_APPROVAL, VersionStatus.SANDBOX_PASSED]))
        .order_by(AgentVersion.created_at, AgentVersion.id)
    ).all()
    return [entry for version, agent, owner in rows if (entry := _entry_for(db, version, agent, owner)) is not None]


def _entry_for(db: Session, version: AgentVersion, agent: Agent, owner: User) -> QueueEntry | None:
    """One row, or `None` if this version is not actually waiting on an admin.

    A `sandbox_passed` version earns a place only when eval has run and fallen
    short: that is the one situation an admin can resolve, and the only one the
    override applies to. Whether it earns a place is therefore still asked of the
    *score* — a version failing only ADR-0028 §6's unwaivable rule is not
    something an admin can do anything about, and listing it would offer them a
    decision they do not have.

    What is *shown* on the row is `version_blocker`, the sentence the gate itself
    would give, which since #48 may be that rule rather than the score. An admin
    who is going to be refused an override should read why here rather than
    discover it after clicking (ADR-0034 §6).
    """
    score = live_score(db, version.id)
    reason = version_blocker(db, version.id)
    if version.status is VersionStatus.PENDING_APPROVAL:
        state, reason = QueueState.AWAITING_DECISION, None
    elif score is not None and blocked_reason(score) is not None:
        state = QueueState.BLOCKED_BY_EVAL
    else:
        return None

    return QueueEntry(
        version_id=version.id,
        agent_id=agent.id,
        agent_name=agent.name,
        owner_email=owner.email,
        state=state,
        score=score,
        blocked_reason=reason,
        high_risk_tools=_high_risk_tools(version),
        created_at=version.created_at,
    )


@dataclass(frozen=True, slots=True)
class DecidedEntry:
    """One review that is over — see ADR-0064.

    Every field after `status` is nullable, and the reason is a fact about this
    platform's data rather than a defensive habit: a version can reach `approved`
    without any `ApprovalDecision` behind it. Two of the three approved versions
    in the shipped fleet are in exactly that state — 131 has a decision row, 132
    and 133 have none — having been put there by something other than
    `POST /approve`. So "who" and "when" are genuinely unknown for some rows, and
    the row says `None` rather than borrowing `AgentVersion.created_at`: a
    made-up decided-at is worse than a blank one, because a blank one can be
    questioned.

    `eval_override_reason` is separate from `note` because the two answer
    different questions. `note` is why this version was sent back;
    `eval_override_reason` is why the machine's verdict was waived on the way in,
    and it is the one an audit asks about first (ADR-0012, ADR-0028 §6).
    """

    version_id: int
    agent_id: int
    agent_name: str
    owner_email: str
    #: `approved` or `rejected` — the decision itself, read off the version
    #: rather than off the decision row, so a row with no audit record still says
    #: what happened.
    status: VersionStatus
    #: Who settled it, from the `ApprovalDecision`. `None` when there is no
    #: record — see the class docstring.
    decided_by: str | None
    decided_at: datetime | None
    #: The reason they left. `None` for an approval, which needs none, and `None`
    #: for a rejection nobody recorded.
    note: str | None
    #: Why the eval bar was waived for this version, if it ever was. `None` means
    #: no override was used, which is the common case and the reassuring one.
    eval_override_reason: str | None
    #: The eval score **as the settling decision recorded it**, never a fresh
    #: reading. Re-reading would answer a different question: the gate consults
    #: the latest run, so a creator who evaluated again afterwards would silently
    #: rewrite what this decision appears to have been made against
    #: (`src.approvals.decisions.score_of`).
    score: Score | None
    created_at: datetime


def decided_versions(db: Session) -> list[DecidedEntry]:
    """Every review that has ended, most recently settled first — see ADR-0064.

    Newest first, and deliberately the opposite of `approval_queue`'s order. A
    queue is worked oldest-first so nobody starves; a log is *read*, and the
    thing a reader wants on top is the last decision made — "what did we just
    approve" is the question this list gets asked.

    Three queries and no more, however long the history gets: the versions, then
    their decisions in one `IN`, then the admins named by those decisions in
    one more. `approval_queue` can afford per-row work because it is bounded by
    how fast an admin works; this list only ever grows, so the per-row lookups
    that suit the queue would turn into an N+1 on the platform's whole history.

    Not paged, which is the one thing here that will need revisiting. Today's
    fleet is three rows and a cap would be invented ceremony; the moment this is
    a screen anybody scrolls, the limit belongs on the endpoint rather than
    hidden in here silently truncating.
    """
    rows = db.execute(
        select(AgentVersion, Agent, User)
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .join(User, User.id == Agent.owner_id)
        .where(AgentVersion.status.in_(DECIDED_STATUSES))
    ).all()
    if not rows:
        return []

    decisions = _decisions_by_version(db, [version.id for version, _agent, _owner in rows])
    entries = [_decided_entry(version, agent, owner, decisions[version.id]) for version, agent, owner in rows]
    # Sorted here rather than in SQL because the key is `decided_at` *falling
    # back to* `created_at`, and the fallback exists for rows whose decision was
    # never recorded. Expressing that as a COALESCE over a correlated subquery
    # would put the same judgement in a place nobody reading this list would
    # think to look.
    return sorted(entries, key=lambda entry: (entry.decided_at or entry.created_at, entry.version_id), reverse=True)


def _decisions_by_version(db: Session, version_ids: list[int]) -> dict[int, list[tuple[ApprovalDecision, str]]]:
    """Every decision about these versions, oldest first, each paired with its admin's email.

    The admins are fetched in one query and joined in Python for
    `src.api.approval_routes._decision`'s reason inverted: that function reads
    *one* version's handful of rows and a join would be overkill, while this one
    reads the whole history and a per-row `db.get` would be the N+1 the docstring
    above promises not to have.
    """
    rows = list(
        db.execute(
            select(ApprovalDecision)
            .where(ApprovalDecision.version_id.in_(version_ids))
            .order_by(ApprovalDecision.created_at, ApprovalDecision.id)
        ).scalars()
    )
    emails = dict(
        db.execute(select(User.id, User.email).where(User.id.in_({row.admin_id for row in rows}))).all()
    )
    grouped: dict[int, list[tuple[ApprovalDecision, str]]] = defaultdict(list)
    for row in rows:
        # An admin account that no longer exists is left as an empty string, the
        # same way `_decision` does it on the wire: the audit row survives its
        # actor by design (`ApprovalDecision.admin_id` has no cascade), and
        # dropping the row because the name is gone would lose the decision too.
        grouped[row.version_id].append((row, emails.get(row.admin_id, "")))
    return grouped


def _decided_entry(
    version: AgentVersion, agent: Agent, owner: User, history: list[tuple[ApprovalDecision, str]]
) -> DecidedEntry:
    """One settled review, assembled from the version and whatever history it has.

    The *last* settling decision is taken rather than the only one. There can only
    be one today — `approve` and `reject` both refuse anything but
    `pending_approval`, and neither status transitions back — but "the latest
    verdict" stays true if that ever changes, where "the single verdict" would
    quietly start raising.
    """
    settled = _latest(history, ApprovalDecisionKind.APPROVED, ApprovalDecisionKind.REJECTED)
    override = _latest(history, ApprovalDecisionKind.EVAL_OVERRIDE)
    return DecidedEntry(
        version_id=version.id,
        agent_id=agent.id,
        agent_name=agent.name,
        owner_email=owner.email,
        status=version.status,
        decided_by=settled[1] if settled is not None else None,
        decided_at=settled[0].created_at if settled is not None else None,
        # From the decision row, not from `AgentVersion.rejection_reason`: the
        # column holds only the *latest* rejection, and this field is about the
        # decision named beside it.
        note=settled[0].reason if settled is not None else None,
        eval_override_reason=override[0].reason if override is not None else None,
        score=score_of(settled[0]) if settled is not None else None,
        created_at=version.created_at,
    )


def _latest(
    history: list[tuple[ApprovalDecision, str]], *kinds: ApprovalDecisionKind
) -> tuple[ApprovalDecision, str] | None:
    """The most recent decision of any of `kinds`, or `None` — `history` is oldest first."""
    matching = [entry for entry in history if entry[0].kind in kinds]
    return matching[-1] if matching else None


def _high_risk_tools(version: AgentVersion) -> tuple[str, ...]:
    """The high-risk permissions `version` asks for, whichever document it asks through.

    A V1 version asks through its pipeline's tool names and a V2 one through its
    graph's `MCP` nodes (#49), so the column that holds the answer is read off
    the version rather than assumed. Reading only the pipeline would leave every
    V2 row claiming no high-risk permission at all — the one field on this row
    that exists to pull attention, saying "nothing here" about the agent that
    wants to send email.

    A document that fails validation is a real possibility for a row written
    before a rule existed (ADR-0003 §7), and it must not take the whole queue
    down with it: the dossier is where that version gets its loud failure, with
    the validation error attached. Here, silence is the safe answer — an empty
    list understates the risk on one row and the admin still has to open it.
    """
    try:
        if version.spec is not None:
            tools = requested_tools(AgentSpec.model_validate(version.spec))
            return tuple(tool.name for tool in tools if tool.is_high_risk)
        if version.workflow is not None:
            actions = requested_actions(WorkflowJSON.model_validate(version.workflow))
            return tuple(action.name for action in actions if action.is_high_risk)
    except ValueError:
        return ()
    return ()
