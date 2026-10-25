"""Spec-lint results as scores — step 2 of the PM playbook.

The graph validator already judges every save (`graph_problems`); what was
missing is that judgement becoming a *data point*: today the red rings vanish
the moment they are fixed, so "how broken are drafts, per version, over time"
— the D1–D3 half of the draft-quality rubric — has no history anywhere.

This module writes that history twice, on purpose. **Locally**, into
`quality_scores` — the table the PM dashboard (`src.pm.metrics`) reads, so
draft health is a query on a deployment with no trace backend at all. And
**to Langfuse**, as one small trace per save carrying the same three scores,
when a key is configured. Same numbers, same names, written by one function,
so the two can never disagree about what a save scored.

Fire-and-forget like everything in `tracing.py`, and for the same reason: a
telemetry backend having a bad day must never cost a creator their save. Each
sink is guarded on its own, so a Langfuse outage does not take the local row
with it.
"""

import logging
from collections.abc import Sequence
from typing import Any

from src.db.base import SessionLocal
from src.db.models import QualityScore, ScoreSource

_log = logging.getLogger(__name__)

#: The three D1–D3 scores, by name, as both sinks spell them.
SPEC_LINT_PASS, GRAPH_PROBLEMS, GRAPH_WARNINGS = "spec_lint_pass", "graph_problems", "graph_warnings"


def record_spec_lint(
    *,
    agent_id: int,
    version_id: int,
    problems: Sequence[str],
    warnings: Sequence[str],
    moment: str,
) -> None:
    """File one save's lint verdict. `moment` is `save` or `confirm` — the same
    numbers mean different things mid-edit and at the gate, and collapsing them
    would make every half-wired draft look like a failed confirmation."""
    try:
        _record_locally(agent_id=agent_id, version_id=version_id, problems=problems, warnings=warnings, moment=moment)
    except Exception:  # noqa: BLE001 — telemetry must never fail the save it describes
        _log.debug("Không ghi được spec-lint score vào quality_scores", exc_info=True)
    try:
        _record_langfuse(agent_id=agent_id, version_id=version_id, problems=problems, warnings=warnings, moment=moment)
    except Exception:  # noqa: BLE001 — same rule, other sink
        _log.debug("Không ghi được spec-lint score lên Langfuse", exc_info=True)


def _record_locally(
    *, agent_id: int, version_id: int, problems: Sequence[str], warnings: Sequence[str], moment: str
) -> None:
    """Three `quality_scores` rows, source `code`, on a session of their own —
    the route's session is mid-request and its commit is not this module's to
    take or to poison."""
    comment = f"{moment}: " + ("; ".join(problems)[:500] if problems else "không có vấn đề")
    db = SessionLocal()
    try:
        db.add_all(
            [
                QualityScore(
                    agent_id=agent_id,
                    version_id=version_id,
                    name=name,
                    value=value,
                    comment=comment if name == SPEC_LINT_PASS else None,
                    source=ScoreSource.CODE,
                )
                for name, value in (
                    (SPEC_LINT_PASS, 0.0 if problems else 1.0),
                    (GRAPH_PROBLEMS, float(len(problems))),
                    (GRAPH_WARNINGS, float(len(warnings))),
                )
            ]
        )
        db.commit()
    finally:
        db.close()


def _record_langfuse(
    *, agent_id: int, version_id: int, problems: Sequence[str], warnings: Sequence[str], moment: str
) -> None:
    client = _langfuse()
    if client is None:
        return
    span = client.start_observation(
        name="spec-lint",
        as_type="event",
        metadata={
            "agent_id": agent_id,
            "version_id": version_id,
            "moment": moment,
            "problems": list(problems)[:20],
            "warnings": list(warnings)[:20],
        },
    )
    trace_id = getattr(span, "trace_id", None)
    span.end()
    if trace_id is None:
        return
    client.create_score(name=SPEC_LINT_PASS, value=1 if not problems else 0, trace_id=trace_id, data_type="BOOLEAN")
    client.create_score(name=GRAPH_PROBLEMS, value=float(len(problems)), trace_id=trace_id)
    client.create_score(name=GRAPH_WARNINGS, value=float(len(warnings)), trace_id=trace_id)


def _langfuse() -> Any:
    """The shared Langfuse client, or `None` — `tracing._langfuse_client`'s judgement,
    reused rather than re-decided, so "is telemetry on" has one answer."""
    from src.services.tracing import _langfuse_client

    return _langfuse_client()
