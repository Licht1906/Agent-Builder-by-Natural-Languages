"""How a Deployment has actually been serving — the operations dashboard, #55 / ADR-0039.

The creator's counterpart to the admin's cost board (#11). That one asks "which
agent is costing us money"; this one asks "is *my* agent all right", and the two
questions want different cuts of the same ledger — which is why this module reads
`run_costs` rather than owning a table of its own. ADR-0006 already refused a
second execution-tracking mechanism beside the run's event stream, and ADR-0009 §4
made `run_costs` the durable record that stream leaves behind. A dashboard that
started writing its own rows would be a third account of one run, and the day two
of them disagree there is no way to say which is lying.

**What counts as the Deployment serving somebody** is the one decision here, and
it is two conditions rather than one:

- `sandbox = false` — a rehearsal with simulated tools (#9) is not traffic.
- `namespace = prod` — nor is the creator's own `trial` chat on the deployed
  version, which calls *real* tools and is therefore not caught by the first
  condition at all (ADR-0009 §4 is explicit that the two come apart).

Both, in SQL, in one place, so the totals and the recent list cannot end up
describing different populations of run. A count that included the owner's own
testing would be the specific way this screen misleads: it would say an agent
nobody has touched is busy, on the days its creator was working on it hardest.

Scoped by **agent**, not by the version currently deployed. A Deployment's
pointer moves (ADR-0013), and a dashboard that reset itself on every publish
would hide the thing an owner opens it to see — what happened around the release.
Each execution carries its own `version_id` instead, so which release a failure
belongs to is on the row rather than implied by the whole screen.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.orm import Session

from src.agents.threads import ThreadNamespace
from src.db.models import Deployment, RunCost

#: How many recent executions the dashboard carries. Enough to see a pattern in
#: the last hour of a small agent, short enough that the screen stays a summary —
#: an owner chasing one specific conversation has the trace (ADR-0009 §6), which
#: is the tool built for that and the reason this list does not grow a paginator
#: nobody asked for.
RECENT_EXECUTIONS = 20


@dataclass(frozen=True, slots=True)
class Execution:
    """One run this Deployment served, as the dashboard lists it.

    `error` is the sentence the run died with, or `None` if it finished. A run
    stopped by its own budget is **not** an error and has none: it produced real
    output and said why it stopped (ADR-0008 §3), and colouring it red would send
    an owner hunting for a fault in an agent that was behaving exactly as its
    ceiling told it to.
    """

    id: int
    version_id: int
    conversation_id: str
    tokens: int
    cost_usd: float
    latency_seconds: float
    error: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class DeploymentOperations:
    """Everything the dashboard shows: four totals, and the last few runs behind them.

    The totals are over *every* run the Deployment has served, not over the
    listed ones. A screen whose headline number counted only the rows underneath
    it would quietly become a different statistic the moment the list was capped.
    """

    runs: int
    errors: int
    tokens: int
    cost_usd: float
    recent: tuple[Execution, ...]


def served(agent_id: int) -> ColumnElement[bool]:
    """The condition for "this row is `agent_id`'s Deployment serving somebody".

    A SQLAlchemy condition rather than a predicate over loaded rows, for the
    reason `src.deployments.publication.visible_to` is one: it answers two shapes
    of question — "how many" and "which ones" — and two hand-written copies is
    how a total stops matching the list printed under it.
    """
    return and_(
        RunCost.agent_id == agent_id,
        RunCost.sandbox.is_(False),
        RunCost.namespace == ThreadNamespace.PRODUCTION,
    )


def operations_of(db: Session, deployment: Deployment) -> DeploymentOperations:
    """What `deployment` has served so far, and the last `RECENT_EXECUTIONS` runs of it.

    Two queries and not one: the totals aggregate every row, the list reads a
    handful, and a single statement doing both would either carry the totals down
    every listed row or read the whole table to build a list of twenty.

    Zeroes for a Deployment nobody has used, never a refusal. "Nothing yet" is a
    true and useful answer, and it is the answer for exactly the agents whose
    owner has just published them and is watching hardest.
    """
    runs, errors, tokens, usd = db.execute(
        select(
            func.count(RunCost.id),
            func.count(RunCost.error),
            func.sum(RunCost.input_tokens + RunCost.output_tokens),
            func.sum(RunCost.cost_usd),
        ).where(served(deployment.agent_id))
    ).one()

    rows = db.execute(
        select(RunCost).where(served(deployment.agent_id)).order_by(RunCost.id.desc()).limit(RECENT_EXECUTIONS)
    ).scalars()

    return DeploymentOperations(
        runs=runs,
        # `count` over a nullable column counts the rows that *have* a value,
        # which is the definition of a failed run since #55 — one pass, and no
        # chance of the two numbers being computed off different populations.
        errors=errors,
        tokens=int(tokens or 0),
        cost_usd=float(usd or 0.0),
        recent=tuple(
            Execution(
                id=row.id,
                version_id=row.version_id,
                conversation_id=row.conversation_id,
                tokens=row.input_tokens + row.output_tokens,
                cost_usd=row.cost_usd,
                latency_seconds=row.latency_seconds,
                error=row.error,
                at=row.created_at,
            )
            for row in rows
        ),
    )
