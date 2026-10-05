"""Proposal, review, confirmation: the two stages a spec passes before anything reads it.

ADR-0020 §4 — finishing the interview does not produce an immutable spec, it
produces a **proposal**. `AgentVersion.status` gains two stages ahead of
everything V1 had:

    spec_pending_confirmation → spec_confirmed → (Workflow Generator runs) → ...

At the first the creator sees all seven fields and can correct them; only
confirming reaches the second, which is the point the spec becomes immutable and
the Workflow Generator (#43) is allowed to read it. Both halves of that sentence
are enforced here — `revise_spec` refuses a confirmed version, and there is
deliberately no other write path into `solution_spec`.

The shape is V1's version lifecycle (`src.agents.versions`) extended rather than
copied: same table, same "edits make a new version, never mutate what is
running" rule, two more statuses at the front. These functions live in their own
module because the gates they enforce are about a *different document* — a
version at `spec_pending_confirmation` has no `AgentSpec` at all, so none of
V1's transitions apply to it and none of them should learn to.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.versions import wrong_stage
from src.db.models import Agent, AgentVersion, SolutionInterviewSession, VersionStatus
from src.models.solution_spec import AgentSolutionSpecification
from src.solution.sessions import state_of

#: How long an agent's auto-generated name may be before it is cut short. Long
#: enough for a real goal sentence, short enough to read in a list of agents.
_NAME_LIMIT = 80


class SpecStageError(ValueError):
    """A spec was asked for something its stage does not allow. Message is user-facing."""


def propose_spec(db: Session, session: SolutionInterviewSession) -> AgentVersion:
    """Turn a finished interview into a version awaiting the creator's confirmation.

    Creates the agent on the first proposal and points the interview at it, the
    same way V1 does: an interview describes one agent, so proposing from it
    again must not leave a creator with two near-duplicates and no way to tell
    them apart.

    Proposing again hands back the version this interview already produced,
    whatever stage it has since reached. That is safe precisely because a
    finished interview cannot change — `answer_current_question` refuses once
    nothing is open — so a second proposal has nothing new to say. Matching on
    the spec itself rather than on the status is what makes it hold *after*
    confirmation too: a client that re-posts is otherwise handed a second,
    identical spec to review, and the creator has two things to confirm and no
    way to tell which one anything downstream will read.

    Refuses on a standing contradiction before it refuses on a missing field,
    because they are different next moves: a contradiction is a thing to settle
    in conversation, a missing field is a question waiting to be answered.
    """
    state = state_of(session)
    if state.conflicts:
        raise SpecStageError(
            "Phỏng vấn còn chỗ chưa khớp nhau, làm rõ xong mới dựng được đặc tả: " + "; ".join(state.conflicts)
        )
    spec = state.draft.to_specification()
    document = spec.model_dump(mode="json")

    agent = _ensure_agent(db, session, spec)
    existing = _version_holding(db, agent, document)
    if existing is not None:
        return existing

    version = AgentVersion(
        agent_id=agent.id,
        solution_spec=document,
        status=VersionStatus.SPEC_PENDING_CONFIRMATION,
    )
    db.add(version)
    return _persist(db, version)


def revise_spec(db: Session, version: AgentVersion, spec: AgentSolutionSpecification) -> AgentVersion:
    """Replace a *proposed* spec with the one the creator says it should be.

    The correction path, and the reason showing all seven fields is worth doing at
    all: a field filled by reading somebody's description is a machine's reading
    of their words, and being shown a misunderstanding with no way to fix it is
    worse than not being shown it (the argument ADR-0011 §1 made for generated
    test cases).

    It is also where a `ref` arrives. The interview never invents one — a blob key
    or a connection id is something the creator attaches — so the first time a
    data source can point at a real resource is here.

    Refused at every other status, which is the whole of "confirming makes the
    spec immutable": there is no second door, so a confirmed spec cannot be
    edited by any caller, present or future.
    """
    _require_status(version, VersionStatus.SPEC_PENDING_CONFIRMATION, action="sửa đặc tả")
    version.solution_spec = spec.model_dump(mode="json")
    return _persist(db, version)


def confirm_spec(db: Session, version: AgentVersion) -> AgentVersion:
    """`spec_pending_confirmation -> spec_confirmed`. The creator saying "yes, that is my problem".

    The one transition that opens the Workflow Generator's door, and the point the
    spec stops being editable. Confirming twice is refused rather than ignored:
    the second caller believes it is confirming what it just read, and a version
    that has moved on since is not that.
    """
    _require_status(version, VersionStatus.SPEC_PENDING_CONFIRMATION, action="xác nhận đặc tả")
    version.status = VersionStatus.SPEC_CONFIRMED
    return _persist(db, version)


def solution_spec_of(version: AgentVersion) -> AgentSolutionSpecification:
    """`version`'s Agent Solution Specification, re-validated on the way out.

    Re-validated rather than handed over as stored JSON, the defence in depth
    ADR-0003 §7 asks of every stored document: a row written before a validator
    existed must not reach a reader either.

    A version that has no solution spec is refused by name. `agent_versions` holds
    V1 rows too (ADR-0020 §5 widened the table rather than replacing it), and a
    reader that asked the wrong one of the two documents deserves a sentence
    saying so rather than a validation error about a document that was never there.
    """
    if version.solution_spec is None:
        raise SpecStageError("Version này chưa có Agent Solution Specification")
    return AgentSolutionSpecification.model_validate(version.solution_spec)


def _version_holding(db: Session, agent: Agent, document: dict[str, Any]) -> AgentVersion | None:
    """The newest version of `agent` already holding exactly `document`, if there is one.

    Matched on the spec rather than on the status, so a re-post keeps being
    answered with the same version after it has been confirmed. Compared in
    Python rather than in SQL because two JSON documents differing only in key
    order are the same spec, and a database-side equality on a `json` column
    would call them different — letting through the very duplicate this prevents.
    """
    versions = db.execute(
        select(AgentVersion).where(AgentVersion.agent_id == agent.id).order_by(AgentVersion.id.desc())
    ).scalars()
    return next((version for version in versions if version.solution_spec == document), None)


def _ensure_agent(db: Session, session: SolutionInterviewSession, spec: AgentSolutionSpecification) -> Agent:
    """The agent this interview is building, **creating and attaching it** if it has none.

    Named for the write it performs rather than for the read, as V1's namesake is.
    The name comes from the goal because the creator never typed one: they
    described a business problem, and the sentence they used for it is the closest
    thing to a name anybody has written. A later proposal leaves the name alone —
    renaming an agent under its owner is a change they did not ask for.
    """
    if session.agent is not None:
        return session.agent
    agent = Agent(owner_id=session.owner_id, name=_agent_name(spec))
    db.add(agent)
    db.flush()
    session.agent_id = agent.id
    return agent


def _agent_name(spec: AgentSolutionSpecification) -> str:
    """The goal, short enough to read in a list of agents.

    Cut at a word boundary rather than mid-syllable, and marked when it was cut,
    so a creator can tell a shortened name from a name that happened to be brief.
    """
    goal = " ".join(spec.goal.split())
    if len(goal) <= _NAME_LIMIT:
        return goal
    return goal[:_NAME_LIMIT].rsplit(" ", 1)[0] + "…"


def _require_status(version: AgentVersion, expected: VersionStatus, *, action: str) -> None:
    """Refuse unless `version` is at the one stage where `action` is allowed.

    The sentence comes from `wrong_stage`, shared with the workflow's own gate
    (#43); the exception type stays this module's, because a caller catching a
    refusal about the spec must not also catch one about the workflow.
    """
    refusal = wrong_stage(version, expected, action=action)
    if refusal is not None:
        raise SpecStageError(refusal)


def _persist(db: Session, version: AgentVersion) -> AgentVersion:
    """Commit and refresh — the tail every mutating function here ends with."""
    db.commit()
    db.refresh(version)
    return version
