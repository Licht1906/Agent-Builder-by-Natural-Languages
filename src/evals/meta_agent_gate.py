"""The meta agent's own regression gate — ADR-0050 §8, runnable end to end.

`golden/meta_agent_v1.jsonl` holds four families of cases (draft, multi_turn,
must_ask, diagnose). This module loads them, drives the loop through each —
multi-turn cases feed every turn in order, carrying the standing proposal
forward the way the canvas would — and grades the outcome **deterministically**:
node-type requirements, precedence (an approval before every irreversible
step), locality (only the nodes the creator pointed at may change), whether a
question was asked when one was owed. No LLM judges an LLM here; the judge
half (wording quality, D4/D7 of the PM playbook) is sampled by humans off the
same transcript.

**Thresholds are pre-registered** (guardrail #5): the numbers in `THRESHOLDS`
were fixed before the gate's first real run, and the gate answers pass/fail
against them exactly. Moving a threshold is a decision that gets its own diff
and its own reviewer — never a quiet edit after seeing a score.

Run it (needs a model key; each case costs real tokens):

    python -m src.evals.meta_agent_gate
"""

import asyncio
import json
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from src.metaagent.loop import CatalogAction, CatalogTool, HistoryTurn, TurnReply, TurnRequest
from src.models.workflow import WorkflowJSON

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "golden" / "meta_agent_v1.jsonl"

#: Minimum pass counts per family, fixed 2026-08-20 **before** the first scored
#: run against any model. The gate passes only when every family clears its bar.
THRESHOLDS: dict[str, tuple[int, int]] = {
    "draft": (5, 6),
    "multi_turn": (4, 5),
    "must_ask": (3, 4),
    "diagnose": (3, 4),
}


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """One case of the golden set, as `golden/meta_agent_v1.jsonl` spells it."""

    id: str
    family: str
    lang: str
    turns: tuple[str, ...]
    current_workflow: WorkflowJSON | None
    viewing_run: Mapping[str, object] | None
    expect: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CaseVerdict:
    """How one case fared: every failed check by name, or none."""

    case_id: str
    family: str
    failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures


#: The blank canvas draft cases start from. `WorkflowJSON` refuses a graph with
#: no nodes at all, so "nothing yet" is spelled the smallest honest way: one
#: `end` node the start points at — a workflow that does nothing, immediately.
_EMPTY = WorkflowJSON.model_validate(
    {"start_node_id": "end", "nodes": [{"id": "end", "type": "end", "config": {}}], "edges": []}
)

#: What an IT team has wired up in the deployment these cases live in: the two
#: built-in Google connections, plus one creator-registered MCP connection into
#: the company's own ERP — the shape ADR-0046's world actually has, where the
#: sensitive data is internal and reached through an approved registration.
#:
#: Fixed here rather than read from a database: a gate whose catalog depended on
#: whichever rows a developer's machine happened to hold would score two runs
#: differently for reasons having nothing to do with the meta agent. `erp_sales`
#: is deliberately **not** a code-defined built-in — it stands for the
#: registered connections a real deployment adds, and a case that names it is
#: checking that the meta agent uses what the creator connected rather than what
#: the platform ships.
EVAL_CATALOG: tuple[CatalogTool, ...] = (
    CatalogTool(
        id="erp_sales",
        name="ERP — Kinh doanh",
        actions=(
            CatalogAction(
                name="query_overdue_invoices",
                description="Danh sách hóa đơn quá hạn, lọc theo số ngày quá hạn",
                read_only=True,
                risk="low",
                input_schema={"type": "object", "properties": {"days_overdue": {"type": "integer"}}},
            ),
            CatalogAction(
                name="query_customer",
                description="Thông tin một khách hàng theo mã",
                read_only=True,
                risk="low",
                input_schema={"type": "object", "properties": {"customer_id": {"type": "string"}}},
            ),
        ),
    ),
    CatalogTool(
        id="gmail",
        name="Gmail",
        actions=(
            CatalogAction(name="search_emails", description="Tìm thư theo cú pháp Gmail", read_only=True, risk="low"),
            CatalogAction(name="read_email", description="Đọc toàn văn một thư", read_only=True, risk="low"),
            CatalogAction(
                name="create_draft", description="Tạo bản nháp thư, chưa phát đi", read_only=False, risk="medium"
            ),
            CatalogAction(
                name="send_email", description="Gửi email — không lấy lại được", read_only=False, risk="high"
            ),
        ),
    ),
    CatalogTool(
        id="google_drive",
        name="Google Drive",
        actions=(
            CatalogAction(name="search", description="Tìm tệp theo từ khoá", read_only=True, risk="low"),
            CatalogAction(name="fetch", description="Đọc nội dung một tệp", read_only=True, risk="low"),
            CatalogAction(
                name="create_doc", description="Tạo Google Doc mới", read_only=False, risk="medium"
            ),
        ),
    ),
)


def load_cases(path: Path = GOLDEN_PATH) -> tuple[GoldenCase, ...]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        workflow = raw.get("current_workflow")
        cases.append(
            GoldenCase(
                id=raw["id"],
                family=raw["family"],
                lang=raw.get("lang", "en"),
                turns=tuple(raw["turns"]),
                current_workflow=WorkflowJSON.model_validate(workflow) if workflow else None,
                viewing_run=raw.get("viewing_run"),
                expect=raw.get("expect") or {},
            )
        )
    return tuple(cases)


def grade(case: GoldenCase, reply: TurnReply, *, before: WorkflowJSON) -> CaseVerdict:
    """The deterministic verdict on one finished case.

    `before` is the graph the *final* turn started from, which is what locality
    is judged against — the same diff the creator would see on the canvas.
    """
    failures: list[str] = []
    expect = case.expect

    asked = reply.cau_hoi is not None
    if bool(expect.get("asks_question")) != asked:
        failures.append(f"asks_question: expected {bool(expect.get('asks_question'))}, got {asked}")

    if expect.get("declines_out_of_scope") and reply.workflow is not None:
        failures.append("declines_out_of_scope: a workflow was proposed for out-of-scope work")

    graph = reply.workflow
    counts = _type_counts(graph)
    required = expect.get("required_node_types")
    for type_name, wanted in (required.items() if isinstance(required, Mapping) else ()):
        if counts.get(str(type_name), 0) < int(wanted):  # type: ignore[call-overload]
            failures.append(f"required_node_types: {type_name} x{wanted} wanted, found {counts.get(str(type_name), 0)}")
    for type_name in _listed(expect.get("forbidden_node_types")):
        if counts.get(str(type_name), 0) > 0:
            failures.append(f"forbidden_node_types: {type_name} present")

    for pair in _listed(expect.get("precedence")):
        guard, guarded = str(pair[0]), str(pair[1])  # type: ignore[index]
        if graph is not None and not _guards_every_path(graph, guard=guard, guarded=guarded):
            failures.append(f"precedence: a path reaches {guarded} without passing {guard}")

    allowed = expect.get("changed_node_ids_subset_of")
    if isinstance(allowed, list | tuple) and graph is not None:
        touched = _touched_existing_nodes(before, graph)
        stray = touched - {str(node_id) for node_id in allowed}
        if stray:
            failures.append(f"locality: untouched-node rule broken by {sorted(stray)}")

    said = f"{reply.loi_nhan} {reply.cau_hoi or ''}"
    for node_id in _listed(expect.get("mentions")):
        if str(node_id) not in said:
            failures.append(f"mentions: explanation never names {node_id}")

    return CaseVerdict(case_id=case.id, family=case.family, failures=tuple(failures))


#: How one turn is executed — injected so the gate itself stays model-free.
#: The production runner builds this over `loop.run_turn` with the real model.
TurnRunner = Callable[[TurnRequest], "asyncio.Future[TurnReply] | object"]


async def run_case(case: GoldenCase, run: Callable[[TurnRequest], "object"]) -> CaseVerdict:
    """Drive every turn of `case` through `run`, then grade the last reply.

    Between turns the standing proposal becomes the next turn's current graph
    and both utterances enter the history — exactly the carry the canvas does
    when a creator applies a proposal and keeps talking.
    """
    current = case.current_workflow or _EMPTY
    history: list[HistoryTurn] = []
    reply = TurnReply(loi_nhan="")
    before = current
    fetcher = _run_fetcher_of(case)
    for message in case.turns:
        before = current
        request = TurnRequest(
            message=message,
            workflow=current,
            catalog=EVAL_CATALOG,
            history=tuple(history),
            viewing_run_id="1" if case.viewing_run else None,
            run_fetcher=fetcher,
        )
        result = run(request)
        reply = await result if asyncio.iscoroutine(result) else result  # type: ignore[assignment]
        history.append(HistoryTurn(role="creator", message=message))
        history.append(HistoryTurn(role="meta_agent", message=reply.loi_nhan))
        if reply.workflow is not None:
            current = reply.workflow
    return grade(case, reply, before=before)


async def run_gate(
    cases: Iterable[GoldenCase], run: Callable[[TurnRequest], "object"]
) -> tuple[bool, tuple[CaseVerdict, ...]]:
    """Every case, then the pre-registered verdict: pass iff every family clears its bar."""
    verdicts = [await run_case(case, run) for case in cases]
    by_family: dict[str, list[CaseVerdict]] = defaultdict(list)
    for verdict in verdicts:
        by_family[verdict.family].append(verdict)
    passed = all(
        sum(v.passed for v in by_family.get(family, [])) >= wanted
        for family, (wanted, _out_of) in THRESHOLDS.items()
    )
    return passed, tuple(verdicts)


def _listed(value: object) -> list[object]:
    """`value` as the list a case's `expect` meant, or empty for absent/misshapen."""
    return list(value) if isinstance(value, list | tuple) else []


def _type_counts(graph: WorkflowJSON | None) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    if graph is not None:
        for node in graph.nodes:
            counts[node.type.value] += 1
    return dict(counts)


def _guards_every_path(graph: WorkflowJSON, *, guard: str, guarded: str) -> bool:
    """True iff no walk from the start reaches a `guarded` node before a `guard` node."""
    types = {node.id: node.type.value for node in graph.nodes}
    onward: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        onward[edge.source_node_id].append(edge.target_node_id)
    # BFS over (node, guarded-yet) states; a guarded-type node hit while unguarded is a failure.
    seen: set[tuple[str, bool]] = set()
    frontier: list[tuple[str, bool]] = [(graph.start_node_id, False)]
    while frontier:
        node_id, shielded = frontier.pop()
        if (node_id, shielded) in seen:
            continue
        seen.add((node_id, shielded))
        kind = types.get(node_id)
        if kind == guarded and not shielded:
            return False
        now_shielded = shielded or kind == guard
        frontier.extend((target, now_shielded) for target in onward.get(node_id, []))
    return True


def _touched_existing_nodes(before: WorkflowJSON, after: WorkflowJSON) -> set[str]:
    """Which of `before`'s nodes were edited, removed, or rewired in `after`.

    New nodes are free — locality is a promise about what the creator did *not*
    mention, and every node they had is what that promise covers.
    """
    older = {node.id: node for node in before.nodes}
    newer = {node.id: node for node in after.nodes}
    touched = set()
    for node_id, node in older.items():
        successor = newer.get(node_id)
        if successor is None or successor.type is not node.type or successor.config != node.config:
            touched.add(node_id)
            continue
        if _incident_edges(before, node_id) != _incident_edges(after, node_id):
            touched.add(node_id)
    return touched


def _incident_edges(graph: WorkflowJSON, node_id: str) -> set[tuple[str, str, str]]:
    return {
        (edge.source_node_id, edge.source_port, edge.target_node_id)
        for edge in graph.edges
        if node_id in (edge.source_node_id, edge.target_node_id)
    }


@dataclass(frozen=True, slots=True)
class _ScriptedRunFetcher:
    """The case's seeded transcript, rendered the way the route renders a real one."""

    header: str
    entries: tuple[str, ...] = field(default=())

    def __call__(self, ma_run: str) -> str | None:
        return "\n".join((self.header, *self.entries))


def _run_fetcher_of(case: GoldenCase) -> Callable[[str], str | None] | None:
    if not case.viewing_run:
        return None
    run = case.viewing_run
    entries = tuple(json.dumps(entry, ensure_ascii=False) for entry in _listed(run.get("transcript")))
    return _ScriptedRunFetcher(header=f"Run #1 — input: {run.get('input', '')!r}", entries=entries)


def _real_runner() -> Callable[[TurnRequest], "object"]:
    """The production turn runner: the loop over the real meta-agent model."""
    from src.metaagent.loop import run_turn
    from src.services.llm import get_chat_model_factory, meta_agent_model

    model = get_chat_model_factory()(meta_agent_model())

    def run(request: TurnRequest) -> object:
        return run_turn(model, request)

    return run


async def _main() -> int:
    cases = load_cases()
    passed, verdicts = await run_gate(cases, _real_runner())
    by_family: dict[str, list[CaseVerdict]] = defaultdict(list)
    for verdict in verdicts:
        by_family[verdict.family].append(verdict)
    for family, (wanted, out_of) in THRESHOLDS.items():
        family_verdicts = by_family.get(family, [])
        ok = sum(v.passed for v in family_verdicts)
        print(f"{family}: {ok}/{len(family_verdicts)} passed (gate: ≥{wanted}/{out_of})")
        for verdict in family_verdicts:
            if not verdict.passed:
                print(f"  FAIL {verdict.case_id}: " + "; ".join(verdict.failures))
    print("GATE:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
