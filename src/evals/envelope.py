"""Zone rollups and autonomy decisions for the evidence envelope — ADR-0045 (read-only part), ported from `develop-eval` (EVAL-4).

The envelope deliberately has no version-wide score.  It answers one question
per risk zone: what is the worst evidence state in that zone, and what may the
runtime do with that verdict?  Cells are the source of truth; this module only
derives the read-time decision used by the later certification and runtime
seams (ADR-0045 §1–2).
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Protocol

from src.db.models import EvidenceState, EvidenceZone


class Autonomy(StrEnum):
    """What the runtime may do for one zone."""

    TU_CHU = "tu_chu"
    CO_DIEU_KIEN = "co_dieu_kien"
    CHAN = "chan"


class EvidenceCellLike(Protocol):
    """The small cell surface needed by the pure envelope functions."""

    zone: EvidenceZone
    state: EvidenceState
    n: int


# The worst state must win regardless of the order in which cells arrive.
_STATE_SEVERITY: Mapping[EvidenceState, int] = {
    EvidenceState.DU_TIN: 0,
    EvidenceState.CHUA_CHAC: 1,
    EvidenceState.DANG_HONG: 2,
}

# Kept explicit so certification JSON has stable, human-facing ordering.
ZONE_ORDER: tuple[EvidenceZone, ...] = (
    EvidenceZone.NGUOI,
    EvidenceZone.AM,
    EvidenceZone.NONG,
)


def state_of(cell: EvidenceCellLike) -> EvidenceState:
    """Read a cell's effective state, treating an unrun cell as uncertain.

    ``EvidenceCell.state`` is a useful snapshot for display, but a caller must
    not turn a zero-trial cell into evidence by supplying or retaining a trusted
    state.  This explicit check keeps the envelope fail-closed even when it is
    handed a stale object or a test double.
    """

    return EvidenceState.CHUA_CHAC if cell.n == 0 else cell.state


def worst_state(states: Iterable[EvidenceState]) -> EvidenceState:
    """Return the worst state, or ``CHUA_CHAC`` when no evidence exists."""

    observed = tuple(states)
    if not observed:
        return EvidenceState.CHUA_CHAC
    return max(observed, key=_STATE_SEVERITY.__getitem__)


def zone_state(cells: Iterable[EvidenceCellLike], zone: EvidenceZone) -> EvidenceState:
    """Roll the cells in ``zone`` up to one fail-closed state."""

    return worst_state(state_of(cell) for cell in cells if cell.zone is zone)


def autonomy_for(zone: EvidenceZone, state: EvidenceState) -> Autonomy:
    """Map one zone/state pair to the envelope's autonomy level.

    A broken cell always blocks approval.  Uncertainty only creates a runtime
    condition in warm/hot zones; a cold zone remains autonomous to avoid
    approval fatigue for harmless work.
    """

    if state is EvidenceState.DANG_HONG:
        return Autonomy.CHAN
    if state is EvidenceState.CHUA_CHAC and zone in {EvidenceZone.AM, EvidenceZone.NONG}:
        return Autonomy.CO_DIEU_KIEN
    return Autonomy.TU_CHU


def zone_autonomy(cells: Iterable[EvidenceCellLike], zone: EvidenceZone) -> Autonomy:
    """Roll up one zone and map its result to autonomy."""

    return autonomy_for(zone, zone_state(cells, zone))


def zone_verdicts(cells: Iterable[EvidenceCellLike]) -> tuple[dict[str, str], ...]:
    """Build stable, score-free zone verdicts from the supplied cells.

    Only zones represented by the supplied cells are returned.  An absent zone
    is not a failed zone; when a zone is represented but has no run, its state
    is uncertain through ``zone_state``'s fail-closed empty-input rule.
    """

    grouped: dict[EvidenceZone, list[EvidenceCellLike]] = {}
    for cell in cells:
        grouped.setdefault(cell.zone, []).append(cell)
    verdicts: list[dict[str, str]] = []
    for zone in ZONE_ORDER:
        if zone not in grouped:
            continue
        state = zone_state(grouped[zone], zone)
        verdicts.append(
            {
                "zone": zone.value,
                "state": state.value,
                "autonomy": autonomy_for(zone, state).value,
            }
        )
    return tuple(verdicts)


__all__ = [
    "Autonomy",
    "EvidenceCellLike",
    "ZONE_ORDER",
    "autonomy_for",
    "state_of",
    "worst_state",
    "zone_autonomy",
    "zone_state",
    "zone_verdicts",
]
