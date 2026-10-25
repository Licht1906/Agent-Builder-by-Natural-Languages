"""Pre-registered thresholds the PM board reads numbers against — ADR-0053 §5.

Written down *before* the numbers, because a threshold decided after seeing
the score is a negotiation, not a standard (slide 49: *"78% cũng ổn mà" —
không phải threshold, là thương lượng*). Changing one is a reviewed diff to
this file, and the record should say what had been seen when it changed.

Every figure is a plain constant rather than a setting: a setting can be
changed on a server without a diff, which is precisely the move this module
exists to prevent.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Thresholds:
    #: Widget 1 / alert Q1. Below this on `manual` traffic the hero goes red.
    #: 0.80 and not higher: every write action on this platform sits behind
    #: a human approval, which is the deck's "email draft — có người duyệt"
    #: tier (slide 48), not the medical/financial one.
    production_success_min: float = 0.80
    #: Alert Q2 — offline pass rate (eval) minus online success rate. Larger
    #: than this and the reference set is not describing production.
    offline_online_gap_max: float = 0.15
    #: Alert Q4 — thumbs-up ratio below this while the judge is fine means
    #: the rubric is measuring the wrong thing.
    thumbs_up_min: float = 0.70
    #: Threshold checks (slide 45): an answer that is right but far slower or
    #: dearer is still a regression.
    latency_p90_max_seconds: float = 60.0
    cost_per_run_max_usd: float = 0.05
    #: Calibration (slide 54, 56): the judge is trusted when both rates hold
    #: on at least `calibration_min_labels` expert labels.
    judge_tpr_min: float = 0.85
    judge_tnr_min: float = 0.85
    calibration_min_labels: int = 30
    #: Slide 18: on a small set one flipped case is two points — a delta this
    #: small is noise, not a change.
    noise_delta: float = 0.02
    #: A version whose offline pass rate fell by more than this against its
    #: predecessor is flagged `regressed`.
    regression_drop: float = 0.05
    #: Slide 50: sample this share of production runs for the judge.
    judge_sample_rate: float = 0.10
    #: Fewer labels/votes than this and a ratio is shown with a "small n" mark.
    small_n: int = 20


PM_THRESHOLDS = Thresholds()
