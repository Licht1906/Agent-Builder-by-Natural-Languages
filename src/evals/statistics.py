"""Evidence statistics for one scenario cell — ADR-0043, ported from `develop-eval` (EVAL-4).

The hard-rule result is deliberately a separate input from the judge tally. A
single forbidden action or other deterministic failure is a broken cell even
when every other trial happened to pass; Wilson only answers the uncertain,
judge-graded part.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from statsmodels.stats.proportion import proportion_confint

from src.db.models import EvidenceState, EvidenceZone
from src.evals.thresholds import N_START

#: Kept as this module's public name (the branch this was ported from defined
#: it here); the values live with the other pre-registered numbers in
#: `src.evals.thresholds` so one reviewed diff moves them all or none.
ZONE_N_START: dict[EvidenceZone, int] = N_START


@dataclass(frozen=True, slots=True)
class EvidenceEstimate:
    """The immutable evidence snapshot used to decide one cell."""

    k: int
    n: int
    wilson_lower: float
    wilson_upper: float
    threshold: float
    state: EvidenceState


def n_start_for(zone: EvidenceZone) -> int:
    """The adaptive starting number of trials for a zone."""
    return ZONE_N_START[zone]


def wilson_interval(k: int, n: int, *, confidence: float = 0.95) -> tuple[float, float]:
    """Return Wilson's interval from the vetted statistics library."""
    _validate_counts(k, n)
    if n == 0:
        return 0.0, 1.0
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence phải nằm giữa 0 và 1")
    lower, upper = proportion_confint(k, n, alpha=1.0 - confidence, method="wilson")
    return float(lower), float(upper)


def classify_evidence(
    k: int,
    n: int,
    *,
    threshold: float,
    hard_failure: bool = False,
) -> EvidenceEstimate:
    """Classify a cell as trusted, uncertain or broken."""
    _validate_threshold(threshold)
    lower, upper = wilson_interval(k, n)
    if n == 0:
        state = EvidenceState.CHUA_CHAC
    elif hard_failure or upper < threshold:
        state = EvidenceState.DANG_HONG
    elif lower >= threshold:
        state = EvidenceState.DU_TIN
    else:
        state = EvidenceState.CHUA_CHAC
    return EvidenceEstimate(k=k, n=n, wilson_lower=lower, wilson_upper=upper, threshold=threshold, state=state)


def summarize_trials(
    passed: Iterable[bool],
    *,
    threshold: float,
    hard_failures: Iterable[bool] = (),
) -> EvidenceEstimate:
    """Count repeated trials and apply hard failures before Wilson."""
    outcomes = tuple(passed)
    hard = any(hard_failures)
    return classify_evidence(sum(outcomes), len(outcomes), threshold=threshold, hard_failure=hard)


def _validate_counts(k: int, n: int) -> None:
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"cần 0 <= k <= n, nhận được k={k}, n={n}")


def _validate_threshold(threshold: float) -> None:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold phải nằm giữa 0 và 1")
