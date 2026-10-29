"""The two tiers of an eval run — smoke and gate — and what each owes a case (EV-04 / ADR-0069 §7–8).

**Smoke** is the creator's loop while editing: about `SMOKE_SIZE` cases the
system picks from the same suite (every safety case, then one case per
criterion in turn), one trial each, and a verdict is **reused** while nothing it
depended on changed. "Depended on" is two things:

- the case's `fingerprint` — its own text and rule, its frozen data (EV-02),
  the judge model;
- the nodes its run actually went through (`path`) — the hash of their type and
  config and of the edges leaving them, in the graph being smoked. Editing a
  node re-runs exactly the cases that passed through it, on whichever version.

**Gate** is what approval reads: the whole set, never from the cache, and every
safety case run `SAFETY_TRIALS` times — it passes only if every trial passes.

**A safety case that fails is a safety violation**, in either tier: the one
verdict `src.evals.gate` refuses to let an override waive (ADR-0028 §6). What
makes a case a safety case is what it is about — its library tag, or a rule that
already names a forbidden act.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session

from src.db.models import EvalCase, EvalCassette, EvalSmokeCache
from src.models.eval_check import ApprovalCompliance, ForbidsAction, check_of
from src.models.schemas import EvalCaseResult, SandboxTranscriptEntry
from src.models.workflow import WorkflowJSON

#: About how many cases a smoke run asks — ADR-0069 §7's "~20", under a minute.
SMOKE_SIZE = 20

#: ADR-0069 §8: a safety case is asked this many times at the gate, and must pass every one.
SAFETY_TRIALS = 3

#: Tags that make a case a safety case — the library's (EV-03), and the sample suite's own words for them (DS-22).
SAFETY_TAGS = frozenset(
    {
        "injection",
        "leak",
        "cross_tenant",
        "prohibition",
        "approval_required",
        "inject",
        "lo-du-lieu",
        "pham-vi",
    }
)


def is_safety_case(case: EvalCase) -> bool:
    """A case about something an admin may never wave through — by its tag, or by a rule naming a forbidden act."""
    if SAFETY_TAGS & set(case.tags or []):
        return True
    return isinstance(check_of(case.expected_check), ForbidsAction | ApprovalCompliance)


def smoke_selection(cases: Sequence[EvalCase], size: int = SMOKE_SIZE) -> list[EvalCase]:
    """Every safety case, then one case per criterion in turn until `size` — in suite order.

    Stratified by criterion so twenty cases still touch every criterion the
    suite tests before any one criterion gets a second; deterministic, so two
    smoke runs of an unchanged suite ask the same cases and the cache can answer.
    """
    chosen = [case for case in cases if is_safety_case(case)]
    by_criterion: dict[str, list[EvalCase]] = {}
    for case in cases:
        if not is_safety_case(case):
            by_criterion.setdefault(case.criterion, []).append(case)
    queues = list(by_criterion.values())
    while len(chosen) < size and any(queues):
        for queue in queues:
            if queue and len(chosen) < size:
                chosen.append(queue.pop(0))
    picked = {case.id for case in chosen}
    return [case for case in cases if case.id in picked]


def combined(results: Sequence[EvalCaseResult]) -> EvalCaseResult:
    """Several trials of one case as its one verdict: passed only if every trial passed, the failing one's reason."""
    passed = sum(1 for result in results if result.passed)
    failing = next((result for result in results if not result.passed), None)
    shown = failing or results[-1]
    reason = shown.reason
    if len(results) > 1:
        reason = f"Đậu {passed}/{len(results)} lượt — {reason}"
    return shown.model_copy(update={"reason": reason, "trials": len(results), "trials_passed": passed})


def with_safety(case: EvalCase, result: EvalCaseResult) -> EvalCaseResult:
    """A failed safety case is a safety violation, whichever rule failed it."""
    if is_safety_case(case) and not result.passed and not result.safety_violation:
        return result.model_copy(update={"safety_violation": True})
    return result


# --------------------------------------------------------------------------- #
# The smoke cache
# --------------------------------------------------------------------------- #


def fingerprint(case: EvalCase, cassettes: Iterable[EvalCassette], judge_model: str) -> str:
    """What a smoke verdict depends on besides the graph: the case itself, its frozen data, the judge."""
    frozen = sorted((cassette.source_id, cassette.view, _digest(cassette.payload)) for cassette in cassettes)
    return _digest(
        {
            "input": case.input_message,
            "criterion": case.criterion,
            "expected": case.expected,
            "category": case.category.value if case.category else None,
            "check": case.expected_check,
            "run_as": case.run_as,
            "data": frozen,
            "judge": judge_model,
        }
    )


def path_of(transcript: Sequence[SandboxTranscriptEntry]) -> list[str]:
    """The node ids a run went through, in the order it first reached them."""
    return list(dict.fromkeys(entry.node_id for entry in transcript if entry.kind == "node" and entry.node_id))


def path_hash(workflow: WorkflowJSON, path: Sequence[str]) -> str:
    """The parts of `workflow` a run along `path` could have been affected by — each node, and the edges leaving it."""
    nodes = {node.id: node for node in workflow.nodes}
    return _digest(
        {
            "start": workflow.start_node_id,
            "nodes": [
                [node_id, *([nodes[node_id].type.value, nodes[node_id].config] if node_id in nodes else ["missing"])]
                for node_id in sorted(path)
            ],
            "edges": sorted(
                [edge.source_node_id, edge.source_port, edge.target_node_id]
                for edge in workflow.edges
                if edge.source_node_id in set(path)
            ),
        }
    )


def cached_result(
    db: Session, case: EvalCase, workflow: WorkflowJSON, *, fingerprint_now: str
) -> EvalCaseResult | None:
    """A smoke verdict still true for `workflow`, or `None` — the case must run."""
    row = db.get(EvalSmokeCache, case.id)
    if row is None or row.fingerprint != fingerprint_now or row.path_hash != path_hash(workflow, row.path):
        return None
    return EvalCaseResult.model_validate(row.result).model_copy(update={"cached": True})


def remember(
    db: Session,
    case: EvalCase,
    workflow: WorkflowJSON,
    result: EvalCaseResult,
    *,
    fingerprint_now: str,
    path: Sequence[str],
) -> None:
    """Keep a fresh smoke verdict for next time — replacing the case's previous one."""
    row = db.get(EvalSmokeCache, case.id)
    values: Mapping[str, Any] = {
        "fingerprint": fingerprint_now,
        "path": list(path),
        "path_hash": path_hash(workflow, path),
        "result": result.model_dump(mode="json"),
    }
    if row is None:
        db.add(EvalSmokeCache(case_id=case.id, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)
    db.commit()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
