"""Cumulative budget enforcement — the ledger a run is stopped by.

See issue #10 / ADR-0008. `AgentSpec.budget` has existed since ADR-0001 and has
been checked against the platform's own hard ceilings ever since (see
`SYSTEM_LIMITS`), but nothing measured a running agent against it. This module is
the measuring: one `BudgetLedger` per run, carried across every step and every
model call, and consulted before each of them.

Cumulative is the whole point. A per-step or per-call ceiling cannot stop the
failure that matters — a step looping on a tool, or a spec whose twelve steps are
each individually modest — because no single unit of work ever looks expensive.
Only a total does.

The ledger measures, decides, and says why; it never talks to a client. Turning a
`BudgetExceededError` into something a reader sees is `src.agents.runtime`'s job,
which is what lets a trial run and a production run be stopped by the same code
and reported by the same event.

It is also where a run's *measured* cost comes from (#11). The ledger already
counts every token a run spends, so `src.agents.costs` prices what is recorded
here rather than measuring the same calls a second time — a second count is a
second answer to "what did this run cost", and the one that gets shown would be
the one that happens to be wired to the screen.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from langchain_core.messages import BaseMessage

#: Which ceiling a run hit. The two names are what reaches the client, so they
#: are the wire contract — `Budget`'s own field names minus the `max_` prefix.
BudgetLimit = Literal["tokens", "wall_seconds"]


class BudgetCeiling(Protocol):
    """The two numbers this ledger enforces, wherever they were written down.

    A structural type rather than `src.models.agent_spec.Budget` itself, because
    V2 moved the ceiling to a different document: ADR-0022 §3 puts it on
    `WorkflowJSON.metadata.budget` since the Agent Solution Specification carries
    nothing technical. That ADR calls the move *"a call-site change, not a design
    change"* — this protocol is what makes that literally true. The ledger reads
    two integers and never asks which document they came from, so a V1 `Budget`
    and a V2 `WorkflowBudget` drive exactly the same enforcement.
    """

    max_total_tokens: int
    max_wall_seconds: int


#: Rough characters per token, for models that report no usage of their own.
#: A crude average across languages rather than a real tokenizer: the ledger's
#: job is to stop a runaway run, and being 30% out on the estimate changes when
#: that happens, not whether. See `BudgetLedger.record_model_call`.
_CHARS_PER_TOKEN = 4


class BudgetExceededError(Exception):
    """A run that has spent past one of its ceilings and must not continue.

    Carries the numbers rather than only a message: the client is told which
    ceiling, what it was, and what the run had spent, so a creator can tell "my
    agent loops forever" from "my ceiling is too tight for the work I asked for".
    Those need different fixes, and a bare "budget exceeded" tells them apart for
    nobody.

    An exception rather than a return value because it has to unwind a step from
    wherever it happens to be — mid-ReAct-loop, between tool calls — and every
    frame in between has nothing useful to add.
    """

    def __init__(self, *, limit: BudgetLimit, limit_value: float, used: float, step_id: str) -> None:
        self.limit = limit
        self.limit_value = limit_value
        self.used = used
        self.step_id = step_id
        super().__init__(_message_for(limit=limit, limit_value=limit_value, used=used))

    @property
    def message(self) -> str:
        """The human-readable reason, for a client and for a stored sandbox record."""
        return str(self)


def _message_for(*, limit: BudgetLimit, limit_value: float, used: float) -> str:
    """Why the run stopped, in the language the rest of the product speaks."""
    if limit == "tokens":
        return (
            f"Lần chạy đã dùng {int(used)} token, vượt trần {int(limit_value)} token của agent — lần chạy đã dừng lại."
        )
    return f"Lần chạy đã chạy {used:.1f} giây, vượt trần {int(limit_value)} giây của agent — lần chạy đã dừng lại."


def _estimated_tokens(messages: list[BaseMessage]) -> int:
    """What `messages` probably cost, for a provider that did not say.

    Text length only: a tool call's arguments are already in the message content
    of the turn that carries it for every provider we support, and an estimate
    that walked the tool-call structures too would be no more accurate for the
    trouble.
    """
    characters = sum(len(str(message.content)) for message in messages)
    return characters // _CHARS_PER_TOKEN


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """What one model spent, split the way it is billed.

    Input and output are kept apart rather than totalled because that is how
    every provider prices them — output costs several times more (see
    `src.models.model_catalog.ModelInfo`). A ledger that only knew the total
    could enforce a ceiling but could never say what a run cost in money, which
    is the question #11 exists to answer.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """What the ceiling is measured against."""
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "ModelUsage") -> "ModelUsage":
        return ModelUsage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


def usage_payload(usage_by_model: dict[str, ModelUsage]) -> dict[str, dict[str, int]]:
    """A per-model tally as JSON, for a run that has to survive a pause (#47).

    Deliberately *not* `src.agents.costs.by_model_of`, whose shape carries a
    `usd` figure beside the tokens. What is being written down here is the input
    to enforcement, not a price: a run parked at a `HumanApproval` node resumes
    against the ceiling it already spent towards, and reviving a stored price
    would be reviving yesterday's price table with it.
    """
    return {
        model: {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        for model, usage in usage_by_model.items()
    }


def usage_of_payload(payload: dict[str, Any] | None) -> dict[str, ModelUsage]:
    """`usage_payload` read back. `None` — nothing carried — is empty."""
    return {
        model: ModelUsage(int(entry["input_tokens"]), int(entry["output_tokens"]))
        for model, entry in (payload or {}).items()
    }


def _reported_usage(response: BaseMessage) -> ModelUsage | None:
    """What the provider says one call cost, or `None` if it did not say usefully.

    `.get` and not `usage["input_tokens"]`, even though `UsageMetadata` marks the
    keys required: the dict arrives from a provider adapter, and a provider that
    fills in only part of it would otherwise stop the run with a raw `KeyError` —
    the "exception thô" #10 exists to replace. A partial report falls back to the
    estimate, same as no report at all.

    Tokens a provider counts in `total_tokens` but attributes to neither side —
    reasoning tokens, most often — are charged as output. That is the
    conservative direction on price, and it keeps the ledger's total equal to the
    number on the bill rather than quietly under it.
    """
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    unattributed = max(0, int(usage.get("total_tokens") or 0) - int(input_tokens) - int(output_tokens))
    return ModelUsage(int(input_tokens), int(output_tokens) + unattributed)


def _total_tokens(usage_by_model: dict[str, ModelUsage]) -> int:
    """Every token in a per-model tally, added up."""
    return sum(usage.total_tokens for usage in usage_by_model.values())


@dataclass(slots=True)
class BudgetLedger:
    """One run's spend, and the ceilings it is measured against.

    Mutable and shared by every step of a run — the opposite of the frozen
    dataclasses elsewhere in this package, and deliberately: a ledger that each
    step got its own copy of would be a per-step ceiling wearing a cumulative
    ceiling's name.

    Lifetime is exactly one run. A `Budget` is per-run (see
    `src.models.agent_spec.Budget`), so the second turn of a conversation starts
    from zero even though it continues the same thread.

    `budget` is a `BudgetCeiling` rather than one concrete document's field type,
    so a V1 `AgentSpec.budget` and a V2 `WorkflowJSON.metadata.budget` are
    enforced by this one ledger instead of two that could drift apart.

    `now` is the clock `seconds_this_leg`/`seconds_elapsed` read, defaulting to
    `time.monotonic` so production is unchanged — mirroring #51's
    `SlidingWindowLimiter(now=...)` (`src.deployments.rate_limit`). A run's own
    speed is real and worth measuring; what a *test* of the ceiling cannot afford
    is for that measurement to double as the test's stopwatch. A test that ties
    `max_wall_seconds` to real time either sleeps past it for real or proves
    nothing about the boundary, and under a loaded test suite the first option is
    not even reliable — the run's own steps can eat the ceiling before the thing
    being tested ever happens. A fake, test-driven clock is what makes the
    boundary — not the machine's mood — the thing being asserted on.
    """

    budget: BudgetCeiling
    now: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    #: Set from `now()` in `__post_init__` rather than a `default_factory`, which
    #: would call `time.monotonic` itself before `now` is known — a slots
    #: dataclass has no `self` to read a sibling field from inside another
    #: field's default. `init=False` because it is derived, not supplied: a
    #: caller states the clock, never the reading it produced.
    _started_at: float = field(init=False, repr=False, compare=False)
    _usage_by_model: dict[str, ModelUsage] = field(default_factory=dict)
    #: What earlier **legs** of this same run already spent, when a
    #: `HumanApproval` node paused it and a person resumed it later (#47,
    #: ADR-0024 §7). Both default to nothing, so an ordinary run — one that never
    #: stopped in front of anybody — is measured exactly as it always was.
    #:
    #: They are what makes "the ledger's lifetime is one run" survive a pause: a
    #: resumed leg that started counting from zero would hand a workflow a fresh
    #: ceiling every time it asked a human something.
    carried_seconds: float = 0.0
    carried_usage: dict[str, ModelUsage] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._started_at = self.now()

    @property
    def tokens_spent(self) -> int:
        """Tokens charged to this run so far, across every leg, step and call.

        The whole run and not this leg, because the ceiling is a per-run ceiling:
        a workflow that paused for approval three times and started counting
        again each time would be a workflow with no total at all — the exact
        failure a cumulative ledger exists to prevent.
        """
        return _total_tokens(self.carried_usage) + _total_tokens(self._usage_by_model)

    @property
    def usage_by_model(self) -> dict[str, ModelUsage]:
        """What each model spent **on this leg**, keyed by model id — the bill, before pricing.

        Per model and not one total, because a spec's whole cost argument is that
        its cheap steps are cheap: the extraction step runs on a model costing a
        fortieth of the reasoning step's, and a single total cannot show that
        (see the spec's "Model theo từng bước"). A copy, so a reader cannot
        change what the run has spent by holding the answer to that question.

        This leg and not the whole run, which is the one place `carried_usage`
        deliberately does *not* show through. Enforcement is about a run's total;
        billing is about money that has not been banked yet, and each leg of a
        paused run is banked when that leg ends. Including what an earlier leg
        already paid for would charge the same tokens twice — and the figure a
        creator is shown would climb every time somebody approved something.
        """
        return dict(self._usage_by_model)

    @property
    def usage_to_carry(self) -> dict[str, ModelUsage]:
        """Everything this run has spent across every leg — what a pause writes down.

        The counterpart of `usage_by_model`: that one answers "what is there left
        to bill", this one answers "what has this run spent against its ceiling",
        and a leg resumed from this figure enforces the same total the run would
        have reached had nobody stopped it.
        """
        carried = dict(self.carried_usage)
        for model, usage in self._usage_by_model.items():
            carried[model] = carried.get(model, ModelUsage()) + usage
        return carried

    @property
    def seconds_this_leg(self) -> float:
        """How long this leg has been running — what the leg is billed for.

        `seconds_elapsed` is the run's total and is what the ceiling is applied
        to; this is the measurement of the stretch that is about to be written
        into `run_costs`. A resumed leg reporting the whole run's running time as
        its own latency would count the first leg's seconds twice in any total
        over the table.
        """
        return self.now() - self._started_at

    @property
    def seconds_elapsed(self) -> float:
        """How long the run has been **running** — waiting on a person does not count.

        `now` is `time.monotonic` in production, so a clock adjustment mid-run
        cannot hand an agent unlimited time or cut a healthy run short.

        `carried_seconds` is the running time of earlier legs, so the sum is
        elapsed *running* time rather than wall-clock-since-the-run-began.
        That is ADR-0024 §7 stated as arithmetic: `max_wall_seconds` exists to
        stop a model that loops or hangs, and a run must not be killed because
        the manager who had to approve it went to lunch.
        """
        return self.carried_seconds + (self.now() - self._started_at)

    @property
    def seconds_remaining(self) -> float:
        """How long the run may still take — the deadline for the next single call.

        `check` can only fire between calls, so on its own it cannot stop a
        provider that has stopped answering: the ticket's "không phải treo" needs
        a bound on one call, not only on the gaps between them. Never negative,
        so a deadline built from it is always a deadline rather than an error.
        """
        return max(0.0, self.budget.max_wall_seconds - self.seconds_elapsed)

    def wall_clock_exceeded(self, step_id: str) -> BudgetExceededError:
        """The error for a call that ran past `seconds_remaining` instead of returning.

        Built here rather than at the call site so that the numbers on it come
        from the same place `check`'s numbers do — a timeout reported with a
        different elapsed time than the ledger holds would be two answers to one
        question.
        """
        return BudgetExceededError(
            limit="wall_seconds",
            limit_value=self.budget.max_wall_seconds,
            used=self.seconds_elapsed,
            step_id=step_id,
        )

    def record_model_call(self, exchange: tuple[list[BaseMessage], BaseMessage], *, model: str) -> None:
        """Charge one call to `model` — to the run's ceiling, and to that model's tally.

        The provider's own `usage_metadata` when there is one — it is the only
        number that matches the bill. When there is none the call is *estimated*
        rather than skipped: a ceiling that silently stops applying to providers
        that volunteer nothing would read as enforced on the approval screen
        while enforcing nothing, which is the same silent-failure shape the tool
        registry rejects for missing simulations (#9).

        The request half of `exchange` is needed for the estimate because the
        prompt is most of the cost — and it grows every iteration of a ReAct
        loop, which is exactly the runaway this ledger exists to catch. The two
        travel as one argument because they are one call; splitting them into two
        parameters invites a caller to pass the prompt of one call with the
        answer of another.

        `model` is the step's declared model rather than anything read back off
        the response: it is what the price table is keyed by (#11), and a
        provider that renamed the model in its reply would otherwise silently
        produce a run whose spend belongs to no priced model at all.
        """
        request, response = exchange
        usage = _reported_usage(response) or ModelUsage(_estimated_tokens(request), _estimated_tokens([response]))
        self._usage_by_model[model] = self._usage_by_model.get(model, ModelUsage()) + usage

    def check(self, step_id: str) -> None:
        """Raise `BudgetExceededError` if the run has already spent past a ceiling.

        Asked *before* each unit of work — each model call, each tool call, and
        once more when the last step is done. Never immediately after charging
        one: a call that has already been paid for should have its answer kept
        (see ADR-0008 §2), and the next thing the run tries to do is soon enough
        to stop it. "Before the next expensive thing" is also the cheapest place
        to stop, since nothing is in flight to abandon.
        """
        spent = self.tokens_spent
        if spent > self.budget.max_total_tokens:
            raise BudgetExceededError(
                limit="tokens",
                limit_value=self.budget.max_total_tokens,
                used=spent,
                step_id=step_id,
            )
        elapsed = self.seconds_elapsed
        if elapsed > self.budget.max_wall_seconds:
            raise BudgetExceededError(
                limit="wall_seconds",
                limit_value=self.budget.max_wall_seconds,
                used=elapsed,
                step_id=step_id,
            )
