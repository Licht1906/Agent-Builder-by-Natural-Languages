"""Deleting an agent — the one order the foreign keys allow, and the one refusal.

A small module for one operation, because that operation has a trap in it. Most
of what hangs off an agent cascades: its costs, its run approvals, its
publication. Two things deliberately do not, and they are the two that decide the
order everything else has to happen in.

- `agent_versions.agent_id` has **no** `ON DELETE`, so an agent cannot be deleted
  while any of its versions exist. Versions are immutable evidence (ADR-0005),
  and a schema that let them be swept away as a side effect of a click would be a
  schema that disagreed with that.
- `deployments.agent_version_id` likewise has none, so a version cannot be
  deleted while a Deployment points at it.

Which leaves exactly one order: publication, then versions, then the agent. Doing
it in any other order is not a bug that produces wrong data — it is an
`IntegrityError` from Postgres, which is the schema doing its job. This module
exists so that order is written down once, next to the reason for it, rather than
being rediscovered by whoever writes the second caller.

**What survives on purpose.** An interview keeps its own row and merely forgets
which agent it produced (`ON DELETE SET NULL`) — the sheet is the creator's, and
deleting the agent it generated must not take their half-finished thinking with
it. So does a template somebody submitted from one of these versions: it was
copied, never referenced (ADR-0025 §8), and remains a perfectly good template.
"""

from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.db.models import Agent, AgentVersion, Deployment


class AgentInUseError(Exception):
    """This agent is currently serving somebody, so deleting it is not the next move.

    Its own exception rather than a bare refusal, so the route can answer 409 —
    nothing is wrong with the request, the world is in a state where what it asks
    for should not happen yet.
    """


def delete_agent(db: Session, agent: Agent) -> None:
    """Remove `agent` and everything that is only about `agent`.

    Refused while its Deployment is **active**, and only then. Deleting something
    other people are talking to right now should be a decision taken twice, and
    the platform already has the first half of it: pausing (ADR-0029 §2) is one
    call, it is reversible, and it is how an owner finds out whether anybody was
    relying on this before the deletion makes that unanswerable.

    A *paused* Deployment is no obstacle at all and is deleted along with the
    rest. That is the whole point of drawing the line at "serving" rather than at
    "published": an agent nobody can reach is an agent nobody loses.

    Committed as one transaction. A half-deleted agent — publication gone,
    versions still there — is a state no screen in this platform knows how to
    render, and the FK ordering above means a failure part-way through is
    precisely the case worth being atomic about.
    """
    deployment = agent.deployment
    if deployment is not None and deployment.is_serving:
        raise AgentInUseError(
            "Agent này đang phục vụ người dùng — hãy tạm dừng deployment trước rồi mới xoá"
        )

    agent_id = agent.id

    # Three statements, in the order the foreign keys allow, and **not**
    # `session.delete(agent)`. The ORM would cascade to the Deployment through
    # `Agent.deployment`'s `delete-orphan` — at flush time, which is after the
    # versions have gone, which is the one order Postgres refuses. Issuing the
    # deletes directly is what puts the order back in this function's hands
    # rather than in the unit-of-work's.
    #
    # The database-level cascades still fire: transcripts, eval runs, eval cases,
    # approval decisions, run costs and run approvals are all `ON DELETE CASCADE`
    # on their own tables, and the interviews' `ON DELETE SET NULL` fires on the
    # last statement. None of that is ORM behaviour, so none of it is lost by
    # going around the ORM here.
    db.execute(delete(Deployment).where(Deployment.agent_id == agent_id))
    db.execute(delete(AgentVersion).where(AgentVersion.agent_id == agent_id))
    db.execute(delete(Agent).where(Agent.id == agent_id))
    db.commit()

    # The rows are gone but this session still holds objects describing them.
    # Expiring says so, rather than letting a caller read an `Agent` that no
    # longer exists out of the identity map.
    db.expire_all()
