"""Running the meta agent's regression gate from the control room, and keeping the answer.

`src.evals.meta_agent_gate` already has the cases, the grader and the
pre-registered thresholds; what it lacked was a *memory* (every run printed
and forgot) and a *stand-in* for the model, so the pipeline could be driven
before anyone spends tokens. This module adds both, plus the one thing the
playbook borrows from n8n's harness: a **failure classification**, so a case
that fails reads as "the builder got it wrong" or "the case cannot be got
right", which is the difference between fixing the prompt and fixing the deck.

**The oracle.** `ScriptedOracle` is a turn runner that knows the answer key:
given a request it finds the golden case the message belongs to and builds a
reply that satisfies `expect` as far as the grader allows — the behaviour of
a *perfect* builder. Run at full fidelity it is the static check the playbook
never had: any case the oracle cannot pass is a case no model can pass, and
the failure is the case's or the grader's, not the builder's. Run with noise
(`fidelity < 1`) it sabotages a share of replies in the ways a real builder
fails — a missing approval step, a question not asked, a node touched that
should not have been — so the dashboard's failure breakdown and `pass^k` have
something realistic to show before the first real run. Nothing it produces is
a measurement of the meta agent, and every row it writes says `simulated`.

**Classification** is the same rule in both modes: a failed case the oracle
also fails is `case_issue`; one the oracle passes is `builder_issue`; one that
raised is `framework_issue`. Rule-based and auto, so it can be wrong in the
way n8n's human triage is not — it is a first sort, not a verdict.
"""

import asyncio
import json
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from src.db.models import MetaAgentGateRun
from src.evals.meta_agent_gate import (
    THRESHOLDS,
    CaseVerdict,
    GoldenCase,
    grade,
    load_cases,
    run_case,
)
from src.metaagent.loop import TurnReply, TurnRequest
from src.models.workflow import WorkflowJSON

SIMULATED, REAL = "simulated", "real"
BUILDER_ISSUE, CASE_ISSUE, FRAMEWORK_ISSUE = "builder_issue", "case_issue", "framework_issue"

#: Default share of final turns the noisy oracle answers correctly. Chosen to
#: land near n8n's published 40–65% suite pass rate once family thresholds
#: are applied, so a simulated board looks like a real one rather than green.
DEFAULT_FIDELITY = 0.78


# -- The oracle ------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Turn:
    case: GoldenCase
    index: int

    @property
    def final(self) -> bool:
        return self.index == len(self.case.turns) - 1


class ScriptedOracle:
    """A turn runner that answers from the golden set's own expectations.

    Keyed by the message text of each turn, which is what `run_case` hands a
    runner and the only thing two cases are guaranteed not to share.
    """

    def __init__(self, cases: Iterable[GoldenCase], *, fidelity: float = 1.0, seed: int = 0) -> None:
        self._turns: dict[str, _Turn] = {}
        for case in cases:
            for index, message in enumerate(case.turns):
                self._turns[message] = _Turn(case=case, index=index)
        self._fidelity = fidelity
        self._rng = random.Random(seed)

    def __call__(self, request: TurnRequest) -> TurnReply:
        turn = self._turns.get(request.message)
        if turn is None:
            return TurnReply(loi_nhan="Tôi không có kịch bản cho câu này.", cau_hoi="Bạn có thể nói rõ hơn không?")
        if not turn.final:
            # Intermediate turn: acknowledge, change nothing. The grade reads
            # the final reply, and the canvas-carry in `run_case` then hands
            # the untouched graph to the last turn as `before`.
            return TurnReply(loi_nhan="Đã ghi nhận, tôi sẽ làm ở bước tiếp theo.", workflow=request.workflow)
        reply = _ideal_reply(turn.case, request.workflow)
        if self._rng.random() < self._fidelity:
            return reply
        return _sabotage(turn.case, reply, request.workflow, self._rng)


def _ideal_reply(case: GoldenCase, current: WorkflowJSON) -> TurnReply:
    """What a builder that read the answer key would say."""
    expect = case.expect
    if expect.get("asks_question"):
        return TurnReply(
            loi_nhan="Tôi cần biết thêm một điều trước khi dựng.",
            cau_hoi="Ai là người sẽ duyệt, và dữ liệu lấy từ kết nối nào?",
        )
    if expect.get("declines_out_of_scope"):
        return TurnReply(
            loi_nhan="Việc này nằm ngoài phạm vi một workflow của nền tảng — tôi không dựng được, và không nên vờ là có thể."
        )
    mentions = [str(node_id) for node_id in _listed(expect.get("mentions"))]
    graph = _build_graph(case, current)
    said = "Tôi đã rà lại graph"
    if mentions:
        said += ": vấn đề nằm ở bước " + ", ".join(f"`{m}`" for m in mentions)
    if graph is not None:
        said += ". Đã đề xuất bản sửa — mọi bước ghi ra ngoài đều đi qua duyệt trước."
    return TurnReply(loi_nhan=said + ".", workflow=graph)


def _build_graph(case: GoldenCase, current: WorkflowJSON) -> WorkflowJSON | None:
    """A graph meeting `required_node_types` / `precedence` / locality as far as they can be met together."""
    expect = case.expect
    wanted = expect.get("required_node_types")
    required: dict[str, int] = (
        {str(k): int(v) for k, v in wanted.items()} if isinstance(wanted, Mapping) else {}  # type: ignore[call-overload]
    )
    forbidden = {str(t) for t in _listed(expect.get("forbidden_node_types"))}
    precedence = [(str(p[0]), str(p[1])) for p in _listed(expect.get("precedence"))]  # type: ignore[index]
    allowed = expect.get("changed_node_ids_subset_of")
    needs_graph = (
        bool(required) or bool(precedence) or isinstance(allowed, list | tuple) or case.current_workflow is not None
    )
    if not needs_graph:
        return None

    if case.current_workflow is None:
        return _fresh_linear_graph(required, forbidden, precedence)
    return _edited_graph(current, required, precedence, allowed if isinstance(allowed, list | tuple) else None)


_DEFAULT_CONFIG: dict[str, dict[str, Any]] = {
    "mcp": {"tool_id": "erp_sales", "action": "query_overdue_invoices", "require_approval": "auto"},
    "agent_llm": {
        "prompt": "Đọc dữ liệu ở bước trước và soạn câu trả lời ngắn gọn, có dẫn nguồn.",
        "model": "gpt-4o-mini",
    },
    "knowledge_search": {"source_ids": []},
    "human_approval": {"approver_role": "manager", "message_template": "Duyệt trước khi thực hiện bước tiếp theo?"},
    "if_else": {"condition": "Kết quả có khẩn cấp không?"},
    "while": {"condition": "Còn mục chưa xử lý", "max_iterations": 5},
    "web_search": {"query_hint": ""},
    "end": {},
}

_PORT: dict[str, str] = {
    "mcp": "done",
    "agent_llm": "done",
    "knowledge_search": "found",
    "human_approval": "approved",
    "if_else": "true",
    "while": "exit",
    "web_search": "done",
}


def _fresh_linear_graph(
    required: Mapping[str, int], forbidden: set[str], precedence: Sequence[tuple[str, str]]
) -> WorkflowJSON:
    """A straight line of the required types, guards first where precedence asks, ending in `end`."""
    order = ["human_approval", "knowledge_search", "mcp", "if_else", "agent_llm", "web_search", "while"]
    guards = {guard for guard, _ in precedence}
    types: list[str] = []
    for type_name in order:
        if type_name in forbidden:
            continue
        wanted = int(required.get(type_name, 0)) or (1 if type_name in guards else 0)
        types.extend([type_name] * wanted)
    if "agent_llm" not in types and "agent_llm" not in forbidden:
        types.append("agent_llm")
    # Guards go to the very front, so every path is shielded from the start:
    # the grader's precedence rule is path-global, not "before the write step".
    types.sort(key=lambda t: 0 if t in guards else 1)

    nodes: list[dict[str, Any]] = [
        {"id": f"{t}_{i + 1}", "type": t, "config": dict(_DEFAULT_CONFIG.get(t, {}))} for i, t in enumerate(types)
    ]
    nodes.append({"id": "end_1", "type": "end", "config": {}})
    edges: list[dict[str, Any]] = []
    for i in range(len(nodes) - 1):
        src, dst = nodes[i], nodes[i + 1]
        edges.append(
            {
                "id": f"e{i + 1}",
                "source_node_id": src["id"],
                "source_port": _PORT[src["type"]],
                "target_node_id": dst["id"],
            }
        )
        if src["type"] == "if_else":
            edges.append(
                {"id": f"e{i + 1}f", "source_node_id": src["id"], "source_port": "false", "target_node_id": "end_1"}
            )
        if src["type"] == "human_approval":
            edges.append(
                {"id": f"e{i + 1}r", "source_node_id": src["id"], "source_port": "rejected", "target_node_id": "end_1"}
            )
    return WorkflowJSON.model_validate({"start_node_id": nodes[0]["id"], "nodes": nodes, "edges": edges})


def _edited_graph(
    current: WorkflowJSON,
    required: Mapping[str, int],
    precedence: Sequence[tuple[str, str]],
    allowed: Sequence[object] | None,
) -> WorkflowJSON:
    """`current` with the smallest edit that adds what is required, touching only allowed nodes.

    Insertion goes between two *allowed* adjacent nodes when locality binds
    (both of their incident-edge sets change); otherwise in front of the first
    node of the guarded type. When neither is possible the required node is
    added where the creator pointed, and the grader says what it says — that
    is the `case_issue` signal this oracle exists to surface.
    """
    doc = current.model_dump(mode="json")
    nodes: list[dict[str, Any]] = [dict(n) for n in doc["nodes"]]
    edges: list[dict[str, Any]] = [dict(e) for e in doc["edges"]]
    by_id = {n["id"]: n for n in nodes}
    allowed_ids = {str(a) for a in allowed} if allowed is not None else None
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["type"]] = counts.get(n["type"], 0) + 1

    def insert_between(src_id: str, edge: dict[str, Any], new_node: dict[str, Any]) -> None:
        nodes.append(new_node)
        target = edge["target_node_id"]
        edge["target_node_id"] = new_node["id"]
        edges.append(
            {
                "id": f"e_{new_node['id']}",
                "source_node_id": new_node["id"],
                "source_port": _PORT[new_node["type"]],
                "target_node_id": target,
            }
        )
        if new_node["type"] == "human_approval":
            end_id = next((n["id"] for n in nodes if n["type"] == "end"), target)
            # Wiring `rejected` to the end touches the end node's incident
            # edges; when locality forbids that, leave the port unwired rather
            # than hand the grader a failure the deck did not ask for.
            if allowed_ids is None or end_id in allowed_ids:
                edges.append(
                    {
                        "id": f"e_{new_node['id']}_r",
                        "source_node_id": new_node["id"],
                        "source_port": "rejected",
                        "target_node_id": end_id,
                    }
                )

    missing = {t: int(w) - counts.get(t, 0) for t, w in required.items() if int(w) > counts.get(t, 0)}
    for guard, _guarded in precedence:
        missing.setdefault(guard, 0)
        if counts.get(guard, 0) == 0:
            missing[guard] = max(missing[guard], 1)

    for type_name, how_many in missing.items():
        for k in range(how_many):
            new_node = {
                "id": f"{type_name}_moi_{k + 1}",
                "type": type_name,
                "config": dict(_DEFAULT_CONFIG.get(type_name, {})),
            }
            candidates = [
                e
                for e in edges
                if allowed_ids is None or (e["source_node_id"] in allowed_ids and e["target_node_id"] in allowed_ids)
            ]
            guarded_types = {guarded for g, guarded in precedence if g == type_name}
            # Prefer the edge right before a guarded-type node (the write step).
            preferred = [e for e in candidates if by_id.get(e["target_node_id"], {}).get("type") in guarded_types]
            edge = (preferred or candidates or edges)[-1] if (preferred or candidates or edges) else None
            if edge is None:
                nodes.append(new_node)
                continue
            insert_between(edge["source_node_id"], edge, new_node)

    if allowed_ids is not None and not missing:
        # A pure config edit on an allowed node — the "change the threshold" / "reword the prompt" cases.
        for node in nodes:
            if node["id"] in allowed_ids and node["type"] != "end":
                node["config"] = {**node["config"], "note": "đã chỉnh theo yêu cầu"}
                break
    return WorkflowJSON.model_validate({"start_node_id": doc["start_node_id"], "nodes": nodes, "edges": edges})


def _sabotage(case: GoldenCase, reply: TurnReply, current: WorkflowJSON, rng: random.Random) -> TurnReply:
    """One realistic builder mistake, chosen among those this case can exhibit."""
    expect = case.expect
    options: list[Callable[[], TurnReply]] = []
    if expect.get("asks_question"):
        options.append(
            lambda: TurnReply(
                loi_nhan="Tôi đã dựng theo cách hiểu của mình.",
                workflow=_fresh_linear_graph({"agent_llm": 1}, set(), []),
            )
        )
    else:
        options.append(lambda: TurnReply(loi_nhan=reply.loi_nhan, cau_hoi="Bạn muốn tôi làm gì tiếp?", workflow=None))
    if reply.workflow is not None:
        graph = reply.workflow
        required = expect.get("required_node_types") or {}
        if isinstance(required, Mapping) and required:
            drop = rng.choice(sorted(required))
            options.append(lambda: TurnReply(loi_nhan=reply.loi_nhan, workflow=_without_type(graph, str(drop))))
        forbidden = _listed(expect.get("forbidden_node_types"))
        if forbidden:
            options.append(lambda: TurnReply(loi_nhan=reply.loi_nhan, workflow=_with_stray(graph, str(forbidden[0]))))
        if expect.get("changed_node_ids_subset_of") is not None:
            options.append(
                lambda: TurnReply(loi_nhan=reply.loi_nhan, workflow=_touch_untouchable(graph, current, expect))
            )
    if _listed(expect.get("mentions")):
        options.append(
            lambda: TurnReply(loi_nhan="Có vẻ model trả lời chưa đúng, tôi đã chỉnh prompt.", workflow=reply.workflow)
        )
    return rng.choice(options)() if options else reply


def _without_type(graph: WorkflowJSON, type_name: str) -> WorkflowJSON:
    doc = graph.model_dump(mode="json")
    victims = [n["id"] for n in doc["nodes"] if n["type"] == type_name]
    if not victims or type_name == "end":
        return graph
    victim = victims[0]
    doc["nodes"] = [n for n in doc["nodes"] if n["id"] != victim]
    incoming = [e for e in doc["edges"] if e["target_node_id"] == victim]
    outgoing = [e for e in doc["edges"] if e["source_node_id"] == victim]
    doc["edges"] = [e for e in doc["edges"] if victim not in (e["source_node_id"], e["target_node_id"])]
    if incoming and outgoing:
        for e in incoming:
            doc["edges"].append({**e, "target_node_id": outgoing[0]["target_node_id"]})
    if doc["start_node_id"] == victim:
        doc["start_node_id"] = outgoing[0]["target_node_id"] if outgoing else doc["nodes"][0]["id"]
    return WorkflowJSON.model_validate(doc)


def _with_stray(graph: WorkflowJSON, type_name: str) -> WorkflowJSON:
    doc = graph.model_dump(mode="json")
    end_id = next((n["id"] for n in doc["nodes"] if n["type"] == "end"), doc["nodes"][-1]["id"])
    stray = {"id": f"{type_name}_thua", "type": type_name, "config": dict(_DEFAULT_CONFIG.get(type_name, {}))}
    doc["nodes"].append(stray)
    doc["edges"].append(
        {
            "id": "e_thua",
            "source_node_id": stray["id"],
            "source_port": _PORT.get(type_name, "done"),
            "target_node_id": end_id,
        }
    )
    return WorkflowJSON.model_validate(doc)


def _touch_untouchable(graph: WorkflowJSON, before: WorkflowJSON, expect: Mapping[str, object]) -> WorkflowJSON:
    allowed = {str(a) for a in _listed(expect.get("changed_node_ids_subset_of"))}
    doc = graph.model_dump(mode="json")
    for node in doc["nodes"]:
        if node["id"] not in allowed and any(n.id == node["id"] for n in before.nodes) and node["type"] != "end":
            node["config"] = {**node["config"], "prompt": "đã sửa nhầm chỗ"}
            break
    return WorkflowJSON.model_validate(doc)


def _listed(value: object) -> list[object]:
    return list(value) if isinstance(value, list | tuple) else []


# -- Running and recording -------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GateOutcome:
    passed: bool
    per_family: dict[str, dict[str, int]]
    verdicts: list[dict[str, Any]]
    #: Cases the perfect oracle itself cannot pass — the deck's own defects.
    unsatisfiable: tuple[str, ...]


async def satisfiability(cases: Sequence[GoldenCase]) -> dict[str, CaseVerdict]:
    """Every case graded against the perfect oracle: the static check on the deck."""
    oracle = ScriptedOracle(cases, fidelity=1.0)
    return {case.id: await run_case(case, oracle) for case in cases}


async def run_gate_classified(cases: Sequence[GoldenCase], run: Callable[[TurnRequest], object]) -> GateOutcome:
    """The gate, with every failed verdict classified against the oracle's own result."""
    baseline = await satisfiability(cases)
    verdicts: list[dict[str, Any]] = []
    for case in cases:
        try:
            verdict = await run_case(case, run)
            classification = None
            if not verdict.passed:
                classification = CASE_ISSUE if not baseline[case.id].passed else BUILDER_ISSUE
            verdicts.append(
                {
                    "case_id": case.id,
                    "family": case.family,
                    "lang": case.lang,
                    "passed": verdict.passed,
                    "failures": list(verdict.failures),
                    "classification": classification,
                }
            )
        except Exception as exc:  # noqa: BLE001 — a crash is a verdict too, and its own class
            verdicts.append(
                {
                    "case_id": case.id,
                    "family": case.family,
                    "lang": case.lang,
                    "passed": False,
                    "failures": [f"exception: {type(exc).__name__}: {exc}"[:300]],
                    "classification": FRAMEWORK_ISSUE,
                }
            )
    per_family: dict[str, dict[str, int]] = {}
    for family, (wanted, out_of) in THRESHOLDS.items():
        mine = [v for v in verdicts if v["family"] == family]
        per_family[family] = {
            "passed": sum(1 for v in mine if v["passed"]),
            "total": len(mine),
            "wanted": wanted,
            "out_of": out_of,
        }
    passed = all(entry["passed"] >= entry["wanted"] for entry in per_family.values())
    return GateOutcome(
        passed=passed,
        per_family=per_family,
        verdicts=verdicts,
        unsatisfiable=tuple(case_id for case_id, v in baseline.items() if not v.passed),
    )


def _real_runner() -> Callable[[TurnRequest], object]:
    from src.evals.meta_agent_gate import _real_runner as production_runner

    return production_runner()


async def run_and_record(
    db: Session,
    *,
    mode: str,
    triggered_by: int | None,
    fidelity: float = DEFAULT_FIDELITY,
    seed: int | None = None,
    cases: Sequence[GoldenCase] | None = None,
) -> MetaAgentGateRun:
    """Run the gate in `mode`, write one `meta_agent_gate_runs` row, return it.

    `seed` makes a simulated run reproducible; left `None` it is drawn, and
    recorded in `thresholds` alongside the bars so the row can be replayed.
    """
    if mode not in (SIMULATED, REAL):
        raise ValueError(f"mode phải là {SIMULATED!r} hoặc {REAL!r}, không phải {mode!r}")
    deck = tuple(cases) if cases is not None else load_cases()
    seed = random.randrange(1_000_000) if seed is None else seed
    runner: Callable[[TurnRequest], object]
    runner = ScriptedOracle(deck, fidelity=fidelity, seed=seed) if mode == SIMULATED else _real_runner()
    outcome = await run_gate_classified(deck, runner)
    row = MetaAgentGateRun(
        mode=mode,
        passed=outcome.passed,
        thresholds={
            **{family: list(bar) for family, bar in THRESHOLDS.items()},
            "_seed": seed,
            "_fidelity": fidelity if mode == SIMULATED else None,
            "_unsatisfiable_cases": list(outcome.unsatisfiable),
        },
        per_family=outcome.per_family,
        verdicts=outcome.verdicts,
        triggered_by=triggered_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def run_and_record_sync(db: Session, **kwargs: Any) -> MetaAgentGateRun:
    """`run_and_record` for callers without a loop (the seed script, a shell)."""
    return asyncio.run(run_and_record(db, **kwargs))


def verdicts_as_text(row: MetaAgentGateRun) -> str:
    """The same report `python -m src.evals.meta_agent_gate` prints, off a stored row."""
    lines = [f"[{row.mode}] GATE: {'PASS' if row.passed else 'FAIL'}"]
    for family, entry in row.per_family.items():
        lines.append(
            f"{family}: {entry['passed']}/{entry['total']} passed (gate: >={entry['wanted']}/{entry['out_of']})"
        )
    for verdict in row.verdicts:
        if not verdict["passed"]:
            lines.append(
                f"  FAIL {verdict['case_id']} [{verdict['classification']}]: " + "; ".join(verdict["failures"])
            )
    return "\n".join(lines)


__all__ = [
    "BUILDER_ISSUE",
    "CASE_ISSUE",
    "DEFAULT_FIDELITY",
    "FRAMEWORK_ISSUE",
    "REAL",
    "SIMULATED",
    "GateOutcome",
    "ScriptedOracle",
    "grade",
    "json",
    "run_and_record",
    "run_and_record_sync",
    "run_gate_classified",
    "satisfiability",
    "verdicts_as_text",
]
