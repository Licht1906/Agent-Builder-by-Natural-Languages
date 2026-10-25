"""Whether one trial measured the agent at all — ADR-0061.

A run's third outcome. `src.evals.checks` answers *"did the agent do the right
thing"*; this answers the question that comes before it — *"was there a run in
which the agent could have"* — and a tally counts a trial only once this says
yes.

Read off the record the trial already left, never re-derived from a second
source: the runtime stamps `SandboxTranscriptEntry.fault` at the place it knew
(`src.workflow.faults`), and this module's whole job is to decide what a stamped
line means for the tally.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from src.evals.checks import CheckVerdict
from src.models.schemas import SandboxTranscriptEntry
from src.services.llm import PROVIDER_ERROR_TYPES
from src.workflow.faults import PlatformFault, fault_of_reason


@dataclass(frozen=True, slots=True)
class TrialObservation:
    """What one run of one case counts as, once the void question is settled.

    Three outcomes since ADR-0061, not two. `void` says the trial never
    happened as far as the agent is concerned — the platform's own plumbing
    stopped it — and a void observation's `passed` is not read at all, because
    there was nothing to pass or fail.

    A void trial may still be a `hard_failure`, and the ordering is deliberate
    rather than an oversight: a run that called a forbidden action still called
    it (`workflow_runner._result_for`'s own rule, ADR-0028 §6), so the caller
    must decide *safety violation first, void second*. `void` is the caller's
    answer once that question is settled.

    It lives here rather than in a module of its own since ADR-0065 retired the
    scenario grid: `observation_of` below is the only thing that builds one, so
    the type and the decision that produces it now sit in one file.
    """

    passed: bool
    hard_failure: bool = False
    sandbox_run_id: int | None = None
    cost_usd: float = 0.0
    void: PlatformFault | None = None


def trial_fault(
    transcript: Sequence[SandboxTranscriptEntry], *, run_fault: PlatformFault | None = None
) -> PlatformFault | None:
    """The first platform fault this trial hit, or `None` when it is a real observation.

    **Any fault voids the whole trial, whatever the cell's check is about**, and
    that is the deliberate simplification. The alternative — voiding only the
    checks a given fault could have reached, so that `ReachesEnd` still counts
    on a run whose every tool died — is more precise and is rejected in
    ADR-0061: it asks a per-check judgement to be got right in the *permissive*
    direction, and any mistake there keeps a verdict nobody measured. A trial
    the platform broke is not a draw from the distribution being estimated, even
    when some weaker rule could still have been read off the wreckage.

    In transcript order, so a cell whose message says *"5/5 không hợp lệ:
    credential"* names the thing that stopped the run first — which is the one
    the creator has to go and fix.

    `run_fault` is what the run died of rather than what a tool reported, and it
    comes last because it happened last. A run that lost its credential on line
    one and then also lost its model provider is a credential problem.
    """
    for entry in transcript:
        if entry.kind != "tool_result" or entry.ok is not False:
            continue
        fault = _fault_of_entry(entry)
        if fault is not None:
            return fault
    return run_fault


def _fault_of_entry(entry: SandboxTranscriptEntry) -> PlatformFault | None:
    """One failed `tool_result`'s fault: the stamp when it has one, the sentence when it cannot.

    A line written since ADR-0061 carries `fault` and is believed. A line
    without one is ambiguous in exactly one way — an old record, or a failure
    the runtime decided was the agent's — and `fault_of_reason` breaks the tie
    the only way it safely can: by recognising sentences **this platform
    composes**, which the runtime never writes for a fault it has ruled out.
    Concretely, the `full`-mode fallback line `gmail.` is not among them.
    """
    if entry.fault:
        try:
            return PlatformFault(entry.fault)
        except ValueError:
            # A value this build does not know: a record written by a newer
            # deployment, or a hand-edited row. Not silently a fault and not
            # silently fine — but a tally is not the place to raise, and the
            # sentence below is still the platform's own.
            return fault_of_reason(entry.content or "")
    return fault_of_reason(entry.content or "")


def fault_of_run_error(exc: BaseException) -> PlatformFault | None:
    """`MODEL` when the exception that ended a run came from the provider, else `None`.

    `PROVIDER_ERROR_TYPES` rather than a list of SDK classes named here:
    `src.services.llm` is the one module allowed to know which SDK answers for a
    model, and it already publishes that tuple for `src.main`'s handler. A
    second import of `openai` in the eval package would be the beginning of a
    second opinion about it.

    Everything else a run can die of stays `None`, and the exclusions are the
    point of the function. A budget ceiling is the agent looping; a graph the
    runtime cannot execute is the creator's drawing; an unhandled error in a
    node is a bug somebody has to see fail. None of those is a reason to throw
    a trial away.
    """
    return PlatformFault.MODEL if isinstance(exc, PROVIDER_ERROR_TYPES) else None


def observation_of(
    *,
    verdict: CheckVerdict | None,
    transcript: Sequence[SandboxTranscriptEntry],
    error: str | None,
    run_fault: PlatformFault | None,
    answer: str,
) -> TrialObservation:
    """What one trial counts as: passed, failed, or void — ADR-0061 §3.

    A function rather than a chain of `elif`s inside a runner's closure,
    because the **order** is the decision and it has to be readable and
    testable on its own:

    1. **A safety violation outranks everything, void included.** A run that
       called a forbidden action still called it, whatever else was broken
       (ADR-0028 §6, and `workflow_runner._result_for`'s own note on why the
       rule is applied before the run's health). This is also the guard that
       keeps void from becoming a way to hide a real failure: the one verdict
       nobody may waive cannot be waived by a broken credential either.
    2. **Then: did the trial happen at all.** A platform fault means no agent
       had a turn, so the verdict below would describe the plumbing under the
       agent's name. It counts toward neither `k` nor `n`.
    3. **Then the ordinary tally** — a run that did not finish fails, a graded
       run gets its verdict, and a cell with no rule at all falls back to
       "did it say anything", unchanged from before ADR-0061.
    """
    if verdict is not None and verdict.safety_violation:
        return TrialObservation(passed=False, hard_failure=True)
    fault = trial_fault(transcript, run_fault=run_fault)
    if fault is not None:
        return TrialObservation(passed=False, void=fault)
    if error is not None:
        return TrialObservation(passed=False)
    if verdict is not None:
        return TrialObservation(passed=verdict.passed)
    return TrialObservation(passed=bool(answer))


__all__ = ["TrialObservation", "fault_of_run_error", "observation_of", "trial_fault"]
