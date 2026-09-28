"""What runs actually spent, written down and added up.

See issue #11 / ADR-0009 §4. `src.agents.costs` decides what a run cost; this
module decides where that fact lives and how it is totalled. They are split
because they change for different reasons — a price change or a new figure edits
`costs`, a new question about spending edits this.

One row per run, **trial and production alike**, and that is the design decision
worth knowing. The PRD's storage sketch puts token/cost/latency on `sandbox_runs`
and [ADR-0007](../../docs/adr/0007-sandbox-runs.md) left room for exactly that.
We did not take it: the question the ticket actually asks — *"chi phí thực tế
tích luỹ theo từng agent … để biết agent nào đang tốn tiền"* — is mostly about
the runs nobody is watching, and `sandbox_runs` only knows about rehearsals.
"""

import asyncio
from dataclasses import dataclass

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.orm import Session

from src.agents.budget import ModelUsage
from src.agents.costs import CostBasis, CostFigure, by_model_of, model_costs_of
from src.agents.threads import RunIdentity, ThreadNamespace
from src.db.base import SessionLocal
from src.db.models import RunCost, RunOutcome, RunTrigger


def run_cost_row(
    identity: RunIdentity,
    cost: CostFigure,
    *,
    sandbox: bool,
    error: str | None = None,
    ending: RunOutcome | None = None,
) -> RunCost:
    """What one run spent and how it ended, as an unsaved row.

    Unsaved, so a caller already writing something else about the run — the
    sandbox writes its transcript — can commit both together and never end up
    with evidence that has no price on it.

    `sandbox` is passed in rather than read off `identity.thread.namespace`, and
    the difference is not pedantry: `POST /chat` accepts `namespace=trial`, and
    such a run uses the *real* tools. Filing it as a rehearsal would let a figure
    measured against the real world be served as the sandbox measurement — a
    measurement of the wrong thing, which is the one mistake a figure labelled
    "đo thật" cannot make. No default, for the same reason `stream_agent_reply`
    gives `sandbox` none: each caller states which world its run belonged to.

    The namespace *is* read off the identity, and is stored beside it since #55.
    That the two questions come apart is exactly why both are kept: "were the
    tools real" prices a figure, "was the conversation real" is what the
    operations dashboard counts, and a ledger holding only the first has to guess
    at the second.

    `error` defaults to `None` because most callers are recording a run that
    finished — and a default of "broken" is not a default anybody would want to
    inherit by forgetting the argument.
    """
    thread = identity.thread
    return RunCost(
        agent_id=thread.agent_id,
        version_id=identity.version_id,
        conversation_id=thread.conversation_id,
        sandbox=sandbox,
        namespace=thread.namespace,
        input_tokens=cost.usage.input_tokens,
        output_tokens=cost.usage.output_tokens,
        cost_usd=cost.usd,
        latency_seconds=cost.seconds or 0.0,
        usage_by_model=by_model_of(cost.by_model),
        trace_id=identity.trace_id,
        error=error,
        # PM playbook §1. Derived, not passed — and derived from the *thread*,
        # not from `sandbox`: what makes a run the creator's own rehearsal is
        # the world it happened in, and the two come apart exactly as this
        # module's own docstring says (a `trial` chat calls real tools, so
        # `sandbox` is False while the run is still somebody testing their own
        # agent). Same two-condition cut `src.deployments.operations.served`
        # makes. There is no third trigger: nothing here runs on a timer.
        trigger=(
            RunTrigger.CHAT_TEST if sandbox or thread.namespace is ThreadNamespace.TRIAL else RunTrigger.MANUAL
        ).value,
        # `ending` when the caller saw how the run stopped (a pause for a
        # person, a budget stop — neither is an exception, and neither is a
        # success); otherwise the coarse cut from `error`. Without `ending`, a
        # run that paused for approval banked as `success` *twice* — once per
        # leg — and a budget stop banked as success too, so widget 1's success
        # rate was counting the runs that most needed a second look.
        outcome=(ending or (RunOutcome.ERROR if error else RunOutcome.SUCCESS)).value,
    )


async def record_run_cost(
    identity: RunIdentity, cost: CostFigure, *, error: str | None = None, ending: RunOutcome | None = None
) -> None:
    """Write a run's spend from inside a response body that is already streaming.

    Its own session on its own thread, for the reason `src.agents.sandbox` opens
    one too: by the time a run finishes, the response is streaming and FastAPI
    has long since closed the session its dependency handed the endpoint.

    `sandbox=False` is not a parameter because there is no caller for whom it
    would be true: a trial run's spend is committed alongside its transcript, in
    one transaction, by `src.agents.sandbox`.

    `error` is, because a streaming caller learns how its run ended *after* the
    run has said what it cost — the `cost` event is emitted on the failing path
    too (`stream_agent_reply`). Which is why the callers hold the figure until
    the stream is over and write once, rather than writing on the cost event and
    correcting the row afterwards: a ledger that is briefly wrong about whether a
    run broke is a ledger that stays wrong whenever the correction is the thing
    that fails.
    """

    def write() -> None:
        db = SessionLocal()
        try:
            db.add(run_cost_row(identity, cost, sandbox=False, error=error, ending=ending))
            db.commit()
        finally:
            db.close()

    await asyncio.to_thread(write)


def _figure_of(row: RunCost) -> CostFigure:
    """A stored row read back as the measurement it was.

    Priced when it was recorded, not re-priced now: a run's cost is what it cost
    on the day, and re-deriving it from today's catalog would silently rewrite
    history every time a provider changes its rates.
    """
    return CostFigure(
        basis=CostBasis.MEASURED,
        usage=ModelUsage(row.input_tokens, row.output_tokens),
        usd=row.cost_usd,
        seconds=row.latency_seconds,
        by_model=model_costs_of(row.usage_by_model),
    )


def latest_trial_cost(db: Session, version_id: int) -> CostFigure | None:
    """What the most recent trial run of `version_id` cost, or `None` if it has never been trialled.

    `None` and not a zero: an untrialled version has no measurement, and a zero
    would read as "we ran it and it was free" — the one thing a measured figure
    must never say when nothing was measured.

    The *latest* trial rather than the cheapest or the average, because the
    creator's question is "what does it cost now", and the run they just watched
    is the one they are asking about.
    """
    row = db.execute(
        select(RunCost)
        .where(RunCost.version_id == version_id, RunCost.sandbox.is_(True))
        .order_by(RunCost.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _figure_of(row) if row is not None else None


@dataclass(frozen=True, slots=True)
class AgentSpend:
    """Everything one agent has cost so far, and over how many runs.

    Rehearsals and live runs are broken out rather than only totalled: an agent
    whose bill is all trial runs is being built, and one whose bill is all live
    traffic is being used. Those are different conversations to have with its
    owner. Both are real money — a sandbox run simulates tools, not the model.
    """

    agent_id: int
    runs: int
    usd: float
    tokens: int
    trial_usd: float
    production_usd: float


def _usd_of(*, sandbox: bool) -> ColumnElement[float]:
    """A row's cost if it is that kind of run, otherwise nothing.

    Two conditional sums in one pass rather than two extra queries: the totals
    have to add up to the overall one, and separate trips to the database are
    separate chances for a run recorded in between to land in one answer and not
    the next.
    """
    return case((RunCost.sandbox.is_(sandbox), RunCost.cost_usd), else_=0.0)


def agent_spend(db: Session, *, agent_ids: list[int] | None = None) -> list[AgentSpend]:
    """Accumulated actual spend per agent, dearest first.

    Aggregated in the database rather than by summing rows in Python: the answer
    is one number per agent however many thousands of runs are behind it, and the
    admin view (#11's "agent nào đang tốn tiền") reads every agent at once.

    `agent_ids=None` means every agent — the admin's question. A list scopes it to
    one owner's agents, which is the same query behind a creator's own view.
    """
    statement = (
        select(
            RunCost.agent_id,
            func.count(RunCost.id),
            func.sum(RunCost.cost_usd),
            func.sum(RunCost.input_tokens + RunCost.output_tokens),
            func.sum(_usd_of(sandbox=True)),
            func.sum(_usd_of(sandbox=False)),
        )
        .group_by(RunCost.agent_id)
        .order_by(func.sum(RunCost.cost_usd).desc())
    )
    if agent_ids is not None:
        statement = statement.where(RunCost.agent_id.in_(agent_ids))
    return [
        AgentSpend(
            agent_id=agent_id,
            runs=runs,
            usd=float(usd or 0.0),
            tokens=int(tokens or 0),
            trial_usd=float(trial or 0.0),
            production_usd=float(production or 0.0),
        )
        for agent_id, runs, usd, tokens, trial, production in db.execute(statement).all()
    ]
