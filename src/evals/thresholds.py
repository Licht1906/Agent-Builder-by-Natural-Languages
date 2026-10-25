"""The retired scenario grid's pre-registered numbers — ADR-0054 §3 (ADR-0042/0043 imported).

**Nothing imports these since ADR-0065 retired the grid**, and they are kept
unchanged on purpose. A pre-registration is a record of what a bar *was* before
anybody saw a score; deleting it in the same change that stops measuring would
leave nothing to check the retirement against. `src.evals.statistics` and
`src.evals.envelope` are kept beside it for the same reason and by the same
rule — no number here moves.

Constants, not settings, for `src/pm/thresholds.py`'s reason: a bar that can be
moved after seeing a score is not a bar. Every number here was fixed before any
grid produced a measurement, and changes only by a reviewed diff that says why —
never in the same change that benefits from it. `eval_pass_threshold` (the
global, admin-configured bar for the non-grid part of a suite) is deliberately
untouched: two numbers, two sets, never mixed (ADR-0054 §3).
"""

from src.db.models import EvidenceZone

#: The per-zone bar a cell's Wilson **lower** bound is compared against
#: (ADR-0043 §4–§5). The deck's framing: these are the organisation's risk
#: appetite, not mathematical truth — the mathematics is in comparing the lower
#: bound, not in the values.
T_ZONE: dict[EvidenceZone, float] = {
    EvidenceZone.NONG: 0.90,
    EvidenceZone.AM: 0.75,
    EvidenceZone.NGUOI: 0.60,
}

#: How many trials a cell starts with, by zone (ADR-0043 §6): spend lands where
#: consequences are large instead of being spread evenly. At small n a loose
#: lower bound is correct and intended — it reads "chưa chắc", which blocks
#: nothing in a cold zone.
N_START: dict[EvidenceZone, int] = {
    EvidenceZone.NGUOI: 3,
    EvidenceZone.AM: 5,
    EvidenceZone.NONG: 8,
}

#: "Thử thêm" grows a cell's n by its zone's own step — the same ladder the
#: cell started on, so uncertainty is bought down fastest where it costs most.
N_STEP: dict[EvidenceZone, int] = dict(N_START)

#: The most one grid run may spend before stopping politely and keeping every
#: finished trial (ADR-0044 §4 — evidence already keyed correctly is evidence,
#: not a half-done result). Generous for scripted/sandboxed trials, a real
#: brake for model-backed ones.
GRID_COST_CAP_USD: float = 2.0
