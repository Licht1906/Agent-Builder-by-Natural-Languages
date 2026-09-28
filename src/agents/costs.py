"""The three cost figures, and the record of what runs actually spent.

See issue #11 / ADR-0009. Three numbers, and the whole point of this module is
that they are **three**:

| Figure | Computed from | Nature |
|---|---|---|
| `ESTIMATE` | steps × an assumed per-call size × price | a guess |
| `WORST_CASE` | the loop ceilings and the enforced budget | a guarantee |
| `MEASURED` | a run that actually happened | a measurement |

Collapsing them into one is the single easiest way for this product to lose the
reader's trust. A "projected" number is a guess and it will be wrong; a "ceiling"
is a mathematical statement about what the runtime permits — *this agent never
costs more than X per run*. Presented as one number, a reader cannot tell which
promise they are being given, and correctly ends up believing neither.

So a number never travels without its basis: `CostFigure` carries `basis`, and
`label`/`nature` are read off it. There is deliberately no way to produce a bare
float from this module — a client that renders a figure renders its nature too,
because it has nothing else to render.

The measured figure is priced from `BudgetLedger`, which already counts every
token a run spends (ADR-0008's own consequence list says so). Counting a second
time would give two answers to one question, and the one on the screen would be
whichever happened to be wired to it.

Nothing here touches the database. Where a measurement is *stored* and how spend
is totalled is `src.agents.spend`, split off so that a price change and a new
question about spending do not edit the same file.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from src.agents.budget import BudgetLedger, ModelUsage
from src.models.agent_spec import AgentSpec
from src.models.model_catalog import ModelInfo, info_of
from src.models.model_catalog import usd_cost as _catalog_usd_cost

#: What the estimate assumes one model call looks like, in tokens. A prompt of a
#: few pages and a couple of paragraphs back — the shape of an ordinary step.
#: Deliberately round numbers: a reader who sees 2000 knows it was assumed, where
#: 2143 would look measured.
_ASSUMED_INPUT_TOKENS_PER_CALL = 2_000
_ASSUMED_OUTPUT_TOKENS_PER_CALL = 500

#: How many model calls the estimate assumes a step makes. One — the ticket's own
#: arithmetic ("số bước × token giả định × giá"), and a step that loops on a tool
#: will cost more than this says.
#:
#: Resisting the temptation to be cleverer here is the point. A guess with a
#: ReAct multiplier bolted on is still a guess, and dressing it up makes it read
#: as though someone worked it out. What protects the reader is the label, not
#: the arithmetic — and the honest bound on a looping step is the worst case,
#: which is computed from the ceiling the runtime actually enforces.
_ASSUMED_CALLS_PER_STEP = 1

#: Which bound produced the worst case. `BUDGET` means the run is stopped by the
#: token ledger before its steps could structurally spend that much; `STRUCTURE`
#: means the spec cannot reach its own budget however hard it tries.
WorstCaseBound = Literal["budget", "structure"]


class UnpricedModelError(LookupError):
    """A model that spent tokens but is not in the price table.

    Cannot happen through the front door — `AgentSpec` refuses any step whose
    model is not in `MODEL_CATALOG`, and the catalog is where prices live. It is
    raised rather than defaulted to zero because zero is a number a reader would
    believe: a silently free agent is worse than a loud failure.
    """

    def __init__(self, model: str) -> None:
        super().__init__(f"model {model!r} không có trong bảng giá — không thể quy ra tiền")


class CostBasis(StrEnum):
    """What kind of claim a figure is making. The most important field here."""

    ESTIMATE = "estimate"
    WORST_CASE = "worst_case"
    MEASURED = "measured"


#: The two sentences shown beside each figure. Written here and not in the
#: frontend so that every client — the creator's screen, the approval dossier
#: (#14), an API consumer — describes a figure the same way. A label that lived
#: in one client would be missing or reworded in the next.
_LABELS: dict[CostBasis, tuple[str, str]] = {
    CostBasis.ESTIMATE: (
        "Ước lượng",
        "Phỏng đoán — tính từ số bước và mức token giả định. Con số này sẽ sai; đừng cam kết bằng nó.",
    ),
    CostBasis.WORST_CASE: (
        "Trần xấu nhất",
        "Đảm bảo — runtime cưỡng chế trần này, một lần chạy không bao giờ tốn quá con số này.",
    ),
    CostBasis.MEASURED: (
        "Đo thật",
        "Số liệu — đọc từ một lần chạy đã thực sự diễn ra, không phải phỏng đoán.",
    ),
}


@dataclass(frozen=True, slots=True)
class ModelCost:
    """What one model spent, and what that came to."""

    usage: ModelUsage
    usd: float


def by_model_of(by_model: Mapping[str, ModelCost]) -> dict[str, dict[str, float]]:
    """A per-model breakdown flattened for JSON — one shape, one place.

    The same conversion is wanted on the wire (`CostFigure.as_payload`) and in
    the `run_costs` column, and hand-rolling it twice is how the stored figure
    and the served figure start disagreeing about a key name.
    """
    return {
        model: {
            "input_tokens": entry.usage.input_tokens,
            "output_tokens": entry.usage.output_tokens,
            "usd": entry.usd,
        }
        for model, entry in by_model.items()
    }


def model_costs_of(payload: Mapping[str, Mapping[str, Any]] | None) -> dict[str, ModelCost]:
    """`by_model_of` read back. `None` — a row written before the column existed — is empty."""
    return {
        model: ModelCost(
            usage=ModelUsage(int(entry["input_tokens"]), int(entry["output_tokens"])),
            usd=float(entry["usd"]),
        )
        for model, entry in (payload or {}).items()
    }


@dataclass(frozen=True, slots=True)
class CostFigure:
    """One of the three numbers, inseparable from what kind of number it is.

    One flat shape rather than three types, for the same reason
    `SandboxTranscriptEntry` is flat: the consumer that matters puts all three
    side by side, and comparing them is the whole exercise. The fields only some
    bases carry are documented as such and left `None` on the others.
    """

    basis: CostBasis
    usage: ModelUsage
    usd: float
    #: How long the run took. `MEASURED` only — nothing else has a duration to
    #: report, and a guessed latency would be a fourth unlabelled number.
    seconds: float | None = None
    #: Which of the two ceilings binds. `WORST_CASE` only — see `worst_case_cost`.
    bound: WorstCaseBound | None = None
    #: The spend broken down by model. `MEASURED` only: this is where "the cheap
    #: step really was cheap" becomes visible, which is the argument per-step
    #: model selection exists to make.
    by_model: dict[str, ModelCost] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """The figure's name, as a reader sees it."""
        return _LABELS[self.basis][0]

    @property
    def nature(self) -> str:
        """What kind of claim this figure makes — a guess, a guarantee, or a measurement."""
        return _LABELS[self.basis][1]

    def as_payload(self) -> dict[str, Any]:
        """The figure as it goes on the wire — labels included, never a bare number.

        `label` and `nature` are computed, not stored, so they cannot be omitted
        by a caller building the payload by hand. That is the whole mechanism
        behind "a number never travels without its basis": there is no other
        constructor for this shape.
        """
        return {
            "basis": self.basis.value,
            "label": self.label,
            "nature": self.nature,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "usd": self.usd,
            "seconds": self.seconds,
            "bound": self.bound,
            "by_model": by_model_of(self.by_model),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CostFigure":
        """A figure read back out of `as_payload`.

        Exists because the run's spend is recorded from the very `cost` event the
        client was shown (#11): the row in `run_costs` and the number on the
        screen are then the same number by construction, rather than two
        renderings of one ledger that could drift apart.

        `label` and `nature` are ignored on the way in — they are derived from
        `basis`, and honouring a caller's version of them would let a payload
        assert that a guess is a guarantee.
        """
        return cls(
            basis=CostBasis(payload["basis"]),
            usage=ModelUsage(int(payload["input_tokens"]), int(payload["output_tokens"])),
            usd=float(payload["usd"]),
            seconds=None if payload.get("seconds") is None else float(payload["seconds"]),
            bound=payload.get("bound"),
            by_model=model_costs_of(payload.get("by_model")),
        )


def _info(model: str) -> ModelInfo:
    """`model`'s catalog entry, or a loud failure."""
    info = info_of(model)
    if info is None:
        raise UnpricedModelError(model)
    return info


def _usd(model: str, usage: ModelUsage) -> float:
    """What `usage` on `model` costs, at the catalog's published rates.

    Delegates the arithmetic to `model_catalog.usd_cost` — the same function
    ingest spend (#101) prices through — so there is exactly one place a price
    change or a units bug can happen. Raises `UnpricedModelError` rather than
    passing `usd_cost`'s bare `None` along: a run's cost figure has nowhere
    honest to put a missing price, and ADR-0009 §1's whole point is that this
    can only happen through a bug, never through the front door.
    """
    usd = _catalog_usd_cost(model, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
    if usd is None:
        raise UnpricedModelError(model)
    return usd


def estimated_cost(spec: AgentSpec) -> CostFigure:
    """What a typical run of `spec` probably costs — a guess, and labelled as one.

    Each step is assumed to make `_ASSUMED_CALLS_PER_STEP` call of an ordinary
    size, priced at that step's own model. That per-step pricing is the one thing
    the estimate genuinely knows: a spec that puts extraction on a cheap model and
    reasoning on a strong one is cheaper than one that does not, and the estimate
    is where a creator sees that before running anything.
    """
    per_call = ModelUsage(_ASSUMED_INPUT_TOKENS_PER_CALL, _ASSUMED_OUTPUT_TOKENS_PER_CALL)
    calls = _ASSUMED_CALLS_PER_STEP
    return CostFigure(
        basis=CostBasis.ESTIMATE,
        usage=ModelUsage(
            len(spec.steps) * calls * per_call.input_tokens, len(spec.steps) * calls * per_call.output_tokens
        ),
        usd=sum(calls * _usd(step.model, per_call) for step in spec.steps),
    )


@dataclass(frozen=True, slots=True)
class _Bound:
    """One ceiling on a run: how many tokens, and what that comes to.

    A type rather than a `tuple[int, float]` because two of these are compared
    and one is chosen, and a figure whose entire value is that it never
    underestimates cannot afford a silent swap of the two positions.
    """

    tokens: int
    usd: float


def _runs_each_step_at_most_once(spec: AgentSpec) -> bool:
    """Whether `spec` can be relied on to visit every step at most once.

    True when each step's successor is later in the list than the step itself,
    which is what makes the structural bound below a *sum* rather than a
    starting point. `Step.next` is free-form (`AgentSpec.next_step_id` resolves
    any id, and no validator forbids a back-edge), so a spec whose last step
    points at its first is expressible today — and for that spec, "every step
    runs once" is simply false.
    """
    position = {step.id: index for index, step in enumerate(spec.steps)}
    for index, step in enumerate(spec.steps):
        # `next_step_id` and not `step.next`: "which step comes after this one"
        # is `AgentSpec`'s rule, and re-deriving it here is how the two start
        # disagreeing about what a `None` means.
        successor = spec.next_step_id(step.id)
        if successor is None:
            continue  # the run ends here
        if position.get(successor, len(spec.steps)) <= index:
            return False
    return True


def _structural_bound(spec: AgentSpec) -> _Bound | None:
    """The most `spec` could spend if nothing but its own shape stopped it.

    Every step running its full `max_iterations`, every call as large as its
    model permits. Priced entirely at the **output** rate, which is the dearer of
    the two: a worst case that assumed a favourable input/output mix would not be
    a worst case, and the one property this figure must never lose is that it is
    an upper bound.

    `None` when the spec can revisit a step, because then there is no such
    bound — summing each step once would be an *under*estimate, and quoting it as
    a guarantee is worse than quoting nothing. The budget bound below still
    holds, and holds for any spec at all, so the figure loses precision and keeps
    its promise.
    """
    if not _runs_each_step_at_most_once(spec):
        return None
    tokens = 0
    usd = 0.0
    for step in spec.steps:
        info = _info(step.model)
        step_tokens = step.max_iterations * info.max_tokens_per_call
        tokens += step_tokens
        usd += step_tokens * info.usd_per_million_output_tokens / 1_000_000
    return _Bound(tokens, usd)


def _enforced_bound(spec: AgentSpec) -> _Bound:
    """The most the runtime will actually let a run of `spec` spend.

    `budget.max_total_tokens` **plus one call**, and that addend is not padding.
    The ledger is consulted *before* each unit of work and never straight after
    charging one (ADR-0008 §2), so the call that crosses the ceiling is paid for
    and kept — deliberately, because it was billed and its answer was streamed. A
    "guarantee" that ignored it would be the one thing this figure cannot afford
    to be: an underestimate. The largest such call is bounded by the most
    permissive model the spec uses.

    Priced at the highest output rate among the spec's models, because the ledger
    counts tokens and not which model spent them — so the dearest model is where
    they might all have gone.

    Holds for *any* spec, whatever shape its edges make: the ledger is cumulative
    over the whole run and knows nothing about steps.
    """
    largest_call = max(_info(step.model).max_tokens_per_call for step in spec.steps)
    dearest_rate = max(_info(step.model).usd_per_million_output_tokens for step in spec.steps)
    tokens = spec.budget.max_total_tokens + largest_call
    return _Bound(tokens, tokens * dearest_rate / 1_000_000)


def worst_case_cost(spec: AgentSpec) -> CostFigure:
    """The ceiling a creator can promise their team — never exceeded, by construction.

    Two independent ceilings bound a run, and the guarantee is the tighter of
    them: the spec's own shape (`_structural_bound`) and the budget the runtime
    enforces (`_enforced_bound`). Reporting only the structural one would quote a
    ceiling far above what actually happens; reporting only the budget one would
    overstate a spec too small to reach it. The minimum is true of both — and
    when the structural one does not exist, the budget one alone still is.

    `bound` says which one binds, and it is worth showing: "budget" means the
    number moves when the creator changes `max_total_tokens`, "structure" means
    it moves when they add steps or iterations. Those are different levers, and a
    creator staring at a number too high for them needs to know which one to pull.
    """
    enforced = _enforced_bound(spec)
    structural = _structural_bound(spec)
    binds_on_structure = structural is not None and structural.usd < enforced.usd
    chosen = structural if binds_on_structure and structural is not None else enforced
    bound: WorstCaseBound = "structure" if binds_on_structure else "budget"
    # Attributed entirely to output, matching how both bounds are priced: the
    # split is what makes a *worst* case worst, not an incidental detail.
    return CostFigure(basis=CostBasis.WORST_CASE, usage=ModelUsage(0, chosen.tokens), usd=chosen.usd, bound=bound)


def measured_cost(ledger: BudgetLedger) -> CostFigure:
    """What a run that just happened actually cost, priced per model.

    Read off the ledger the run was already enforced against, so the number on
    the cost screen and the number the ceiling was applied to are the same number.

    A run that paused at a `HumanApproval` node (#47) is priced one **leg** at a
    time — `usage_by_model` and `seconds_this_leg` both report the stretch that
    has not been banked yet — because each leg ends by writing its own
    `run_costs` row. The ceiling still applies to the whole run; only the money
    is divided, and dividing it is what stops the same tokens being charged
    again every time somebody approves something.
    """
    by_model = {model: ModelCost(usage=usage, usd=_usd(model, usage)) for model, usage in ledger.usage_by_model.items()}
    total = ModelUsage()
    for entry in by_model.values():
        total += entry.usage
    return CostFigure(
        basis=CostBasis.MEASURED,
        usage=total,
        usd=sum(entry.usd for entry in by_model.values()),
        seconds=ledger.seconds_this_leg,
        by_model=by_model,
    )
