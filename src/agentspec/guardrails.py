"""The mandatory guardrail baseline (spec.md §11.1) and the two ways it is enforced.

Six guardrails the platform owns. A spec author may add to them and may not take
one away — FR-13 states the exception to "the user may edit every field" and this
module is where it is real rather than promised.

The baseline is also rendered into the meta-agent's prompt (`<baseline_guardrails>`
in `platform/meta-agent-system-prompt.md`), so the model is told not to omit them.
Telling it is not enforcing it, which is why `merge_baseline` runs on the way back
regardless of what the model returned.
"""

from typing import Any

from src.agentspec.spec import InputGuardrail, OutputGuardrail

BASELINE_INPUT: tuple[InputGuardrail, ...] = (
    InputGuardrail.PROMPT_INJECTION_DETECTOR,
    InputGuardrail.PII_REDACTOR,
    InputGuardrail.TOPIC_CLASSIFIER,
)

BASELINE_OUTPUT: tuple[OutputGuardrail, ...] = (
    OutputGuardrail.PII_MASKER,
    OutputGuardrail.CITATION_REQUIRED,
    OutputGuardrail.CONFIDENCE_ROUTER,
)


def merge_baseline(guardrails: Any) -> Any:
    """Union the baseline into a raw `guardrails` object from the meta-agent.

    Baseline entries lead and the author's additions follow, so the same draft
    merges to the same list every time — a version diff should show what the
    author changed, not a reordering the platform introduced.

    Operates on the raw mapping rather than a parsed model because it has to run
    *before* validation: a draft missing a baseline entry is not yet a valid spec.
    """
    if not isinstance(guardrails, dict):
        # Not shaped like a guardrails object at all. Hand it back untouched so the
        # parser refuses it for what it is, rather than for a merge that went wrong.
        return guardrails
    merged: dict[str, Any] = dict(guardrails)
    merged["input"] = _union(BASELINE_INPUT, guardrails.get("input"))
    merged["output"] = _union(BASELINE_OUTPUT, guardrails.get("output"))
    return merged


def _union(baseline: tuple[InputGuardrail | OutputGuardrail, ...], proposed: Any) -> list[Any]:
    entries: list[Any] = [guardrail.value for guardrail in baseline]
    if isinstance(proposed, list):
        entries.extend(entry for entry in proposed if entry not in entries)
    return entries


def missing_baseline(
    proposed_input: list[InputGuardrail],
    proposed_output: list[OutputGuardrail],
) -> tuple[list[InputGuardrail], list[OutputGuardrail]]:
    """Which baseline guardrails a spec has dropped. Empty lists mean the baseline is intact."""
    return (
        [guardrail for guardrail in BASELINE_INPUT if guardrail not in proposed_input],
        [guardrail for guardrail in BASELINE_OUTPUT if guardrail not in proposed_output],
    )
