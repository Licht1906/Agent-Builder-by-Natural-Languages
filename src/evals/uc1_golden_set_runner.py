"""UC1-17 (#133), spec.md §11.2/§13, FR-33 — run the whole 23-case golden set
against a compiled-but-undeployed version and grade it, aggregating
`src.evals.uc1_deepeval_runner` (the 8 `judge: llm` cases) and
`src.evals.uc1_rule_checker` (the 15 `judge: rule` ones) into one pass rate.

Neither of those two modules runs the agent — `judge_uc1_cases` takes an
answer as given, `check_uc1_rule_cases` takes a transcript as given. This
module is what actually produces both: one `run_sandbox_turn`
(`src.compiler.sandbox`, UC1-38 / #126) per case, on the case's own
`principal` — never the version's creator, the same delegated-authority rule
every other UC-1 read obeys — so `check_cross_tenant`'s `row_filter.scope`
reads what the PEP decided for *that* principal, not a shared one.

`submit-for-approval` (`src.api.uc1_approval_routes`) reads only this run's
aggregate `pass_rate`; `src.evals.uc1_eval_runs` is where a full
`GoldenSetRunResult` gets persisted so the approval dossier (UC1-16) can
render every case, not just the number.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session

from src.agentspec.spec import AgentSpec
from src.compiler.graph import AGENT_MODEL, CompileDependencies, compile_spec
from src.compiler.sandbox import run_sandbox_turn
from src.compiler.state import TranscriptStep
from src.compiler.tools import ToolImplementation
from src.evals.uc1_deepeval_runner import UC1JudgeResult, judge_uc1_cases
from src.evals.uc1_golden_set import (
    DEFAULT_GOLDEN_SET_PATH,
    CorrectLookupCase,
    GoldenCaseGroup,
    GoldenCaseJudge,
    UC1GoldenCase,
    assert_golden_set_composition,
    load_golden_set,
)
from src.evals.uc1_rule_checker import RuleCheckResult, check_uc1_rule_cases
from src.evals.uc1_suite import import_golden_set_into_suite, load_uc1_golden_cases
from src.policy.pep import AuditSink, PEPClient, Principal
from src.services.llm import ChatModelFactory


@dataclass(frozen=True, slots=True)
class GoldenSetCaseResult:
    """One golden-set case's verdict — grouped and judged whichever way §11.2
    assigns it, flattened to the same shape so a dossier can render all 23 in
    one list without branching on which grader produced each row.
    """

    case_id: str
    group: GoldenCaseGroup
    judge: GoldenCaseJudge
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class GoldenSetRunResult:
    passed_count: int
    total_count: int
    pass_rate: float
    cases: tuple[GoldenSetCaseResult, ...]


@dataclass(frozen=True, slots=True)
class GoldenSetRunDeps:
    """Everything about *how* to run the golden set, bundled into one argument —
    the same "3 parameters" fix `CompileDependencies` (`src.compiler.graph`)
    already applies, leaving `spec` as `run_uc1_golden_set`'s one positional
    argument that varies on its own axis (which version), the rest fixed for
    the whole of one call.
    """

    model_factory: ChatModelFactory
    judge_model_factory: ChatModelFactory
    pep_client: PEPClient
    tool_implementations: Mapping[str, ToolImplementation]
    #: UC1-32 (#144) — a golden-set run makes real PEP decisions the same way
    #: a production turn does (§11.2's rule-based cases read `row_filter`
    #: straight off the transcript a real `execute_tool_call` produced), so
    #: they get audited the same way too. Required, not defaulted, matching
    #: `CompileDependencies.audit_sink`.
    audit_sink: AuditSink
    verified_connection_objects: Mapping[str, Sequence[str]] = field(default_factory=dict)
    #: ADR-0054 §6: the golden set now lives in the agent's suite
    #: (`src.evals.uc1_suite`), not this file — kept only as the **seed** a
    #: still-empty suite is lazily imported from on a version's first golden-
    #: set run (`scripts/import_golden_set.py` does the same import for CI /
    #: air-gap DB rebuilds ahead of time, so this path is rarely taken).
    golden_set_path: Path | str = DEFAULT_GOLDEN_SET_PATH


def _principal_of(case: UC1GoldenCase) -> Principal:
    """A golden case's own `principal` block, as the identity `run_sandbox_turn`
    runs its turn under — `sub` keyed off `employee_id` since a golden case
    names no numeric user id, the same substitution
    `scripts/verify_uc1_data_surface.py` makes for the same reason.
    """
    return Principal(
        sub=case.principal.employee_id,
        employee_id=case.principal.employee_id,
        roles=list(case.principal.roles),
        department=case.principal.department,
    )


def _thread_id(spec: AgentSpec, case: UC1GoldenCase) -> str:
    return f"golden:{spec.metadata.id}:{spec.metadata.version}:{case.id}"


def _run_every_case(
    spec: AgentSpec, cases: Sequence[UC1GoldenCase], deps: CompileDependencies
) -> tuple[dict[str, list[TranscriptStep]], dict[str, str]]:
    """One `run_sandbox_turn` per case, on its own thread and its own principal.

    `deps.checkpointer` must be a checkpointer this call may treat as scratch —
    the caller is expected to hand in a fresh one per run (`run_uc1_golden_set`
    always does), so two runs of the same version's golden set never see each
    other's turns on `golden:<agent>:<version>:<case_id>`.
    """
    compiled = compile_spec(spec, deps)
    transcripts: dict[str, list[TranscriptStep]] = {}
    answers: dict[str, str] = {}
    for case in cases:
        result = run_sandbox_turn(
            compiled, message=case.input, principal=_principal_of(case), thread_id=_thread_id(spec, case)
        )
        transcripts[case.id] = result.transcript
        answers[case.id] = result.final_answer or ""
    return transcripts, answers


def _case_result_of_rule(case: UC1GoldenCase, result: RuleCheckResult) -> GoldenSetCaseResult:
    return GoldenSetCaseResult(case_id=case.id, group=case.group, judge=case.judge, passed=result.passed, detail=result.reason)


def _case_result_of_judge(case: CorrectLookupCase, result: UC1JudgeResult) -> GoldenSetCaseResult:
    detail = (
        f"faithfulness={result.faithfulness.score:.2f} ({result.faithfulness.reason}); "
        f"answer_relevancy={result.answer_relevancy.score:.2f} ({result.answer_relevancy.reason})"
    )
    return GoldenSetCaseResult(case_id=case.id, group=case.group, judge=case.judge, passed=result.passed, detail=detail)


async def run_uc1_golden_set(spec: AgentSpec, deps: GoldenSetRunDeps, db: Session) -> GoldenSetRunResult:
    """FR-33: the whole smoke test, one call. Compiles `spec` fresh (its own
    `InMemorySaver`, never the sandbox chat endpoint's shared one — see
    `_run_every_case`), runs all 23 cases, and returns one aggregate plus the
    per-case log `src.evals.uc1_eval_runs.record_eval_run` persists for the
    dossier.

    Cases come from `spec.metadata.id`'s suite (ADR-0054 §6), not the JSONL
    file directly. A suite with nothing in it yet (no one has run
    `scripts/import_golden_set.py`) is seeded once from `deps.golden_set_path`
    before running, so a fresh DB behaves exactly as it did before this suite
    existed — the file is `import_golden_set_into_suite`'s **seed**, read
    only on that first touch.
    """
    cases = load_uc1_golden_cases(db, spec.metadata.id)
    if not cases:
        import_golden_set_into_suite(db, spec.metadata.id, load_golden_set(deps.golden_set_path))
        cases = load_uc1_golden_cases(db, spec.metadata.id)
    assert_golden_set_composition(cases)

    compile_deps = CompileDependencies(
        model=deps.model_factory(AGENT_MODEL),
        tool_implementations=deps.tool_implementations,
        pep_client=deps.pep_client,
        audit_sink=deps.audit_sink,
        checkpointer=InMemorySaver(),
        verified_connection_objects=deps.verified_connection_objects,
    )
    transcripts, answers = _run_every_case(spec, cases, compile_deps)

    rule_results = {result.case_id: result for result in check_uc1_rule_cases(cases, transcripts=transcripts, spec=spec)}
    llm_cases = [case for case in cases if isinstance(case, CorrectLookupCase)]
    llm_results = {
        result.case_id: result
        for result in await judge_uc1_cases(llm_cases, answers=answers, model_factory=deps.judge_model_factory)
    }

    case_results = tuple(
        _case_result_of_judge(case, llm_results[case.id])
        if isinstance(case, CorrectLookupCase)
        else _case_result_of_rule(case, rule_results[case.id])
        for case in cases
    )
    passed_count = sum(1 for result in case_results if result.passed)
    total_count = len(case_results)
    return GoldenSetRunResult(
        passed_count=passed_count,
        total_count=total_count,
        pass_rate=passed_count / total_count,
        cases=case_results,
    )
