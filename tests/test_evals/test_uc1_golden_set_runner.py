"""UC1-17 (#133) — `run_uc1_golden_set`, the orchestrator that runs all 23
golden-set cases through the sandbox and aggregates UC1-29/UC1-30's own
graders into one pass rate.

Per-case grading correctness is `test_uc1_deepeval_runner.py`/
`test_uc1_rule_checker.py`'s job; this file is about the wiring — every case
gets run, on its own thread and its own principal, and the aggregate matches
whatever the two graders actually decided.
"""

import json

import pytest
from langchain_core.messages import AIMessage

from src.compiler.tools import default_tool_implementations
from src.evals.uc1_golden_set import GoldenCaseJudge, load_golden_set
from src.evals.uc1_golden_set_runner import GoldenSetRunDeps, run_uc1_golden_set
from src.policy.pep import AuthzRequest, Decision, Effect
from tests.conftest import ScriptedToolModel
from tests.test_agentspec.conftest import spec_payload

pytestmark = pytest.mark.asyncio


def _always_allow(request: AuthzRequest) -> Decision:
    return Decision(effect=Effect.ALLOW, reasons=[], row_filter={}, approver_roles=[], redact_fields=[], max_rows=100)


def _noop_audit_sink(request_id: str, request: AuthzRequest, decision: Decision) -> None:
    """UC1-32 (#144): this suite is about grading, not auditing — see
    `tests/test_compiler/test_graph.py`'s own `_noop_audit_sink` for the
    same reasoning.
    """


def _deps(*, model_factory, judge_model_factory) -> GoldenSetRunDeps:
    return GoldenSetRunDeps(
        model_factory=model_factory,
        judge_model_factory=judge_model_factory,
        pep_client=_always_allow,
        tool_implementations=default_tool_implementations(resolve_credential=lambda ref: ""),
        audit_sink=_noop_audit_sink,
    )


def _agent_model() -> ScriptedToolModel:
    """One bland, tool-free answer per of the 23 cases — `run_sandbox_turn` calls
    the model exactly once per case since a reply with no `tool_calls` routes
    straight to `END` (`src.compiler.graph._route_after_agent`).
    """
    total = len(load_golden_set())
    return ScriptedToolModel(responses=[AIMessage(content="Không có thông tin phù hợp.") for _ in range(total)])


def _judge_model(*, score: float) -> ScriptedToolModel:
    verdict = json.dumps(
        {"faithfulness": {"score": score, "reason": "test"}, "answer_relevancy": {"score": score, "reason": "test"}}
    )
    correct_lookup_count = sum(1 for case in load_golden_set() if case.judge is GoldenCaseJudge.LLM)
    return ScriptedToolModel(responses=[AIMessage(content=verdict) for _ in range(correct_lookup_count)])


def _spec():
    from src.agentspec import validate_spec

    return validate_spec(spec_payload())


async def test_every_case_is_run_and_graded_exactly_once(db_session) -> None:
    """ADR-0054 §6: cases now come from the agent's suite, seeded from the
    JSONL file on this test's first touch (an empty DB behaves exactly as it
    did when the runner read the file directly) — see #157.
    """
    cases = load_golden_set()
    agent_model, judge_model = _agent_model(), _judge_model(score=1.0)

    def model_factory(model_id: str) -> ScriptedToolModel:
        return agent_model

    result = await run_uc1_golden_set(
        _spec(), _deps(model_factory=model_factory, judge_model_factory=lambda model_id: judge_model), db_session
    )

    assert result.total_count == len(cases)
    assert {case.case_id for case in result.cases} == {case.id for case in cases}
    assert [case.case_id for case in result.cases] == [case.id for case in cases]  # order preserved


async def test_pass_rate_is_the_fraction_of_cases_that_actually_passed(db_session) -> None:
    def model_factory(model_id: str) -> ScriptedToolModel:
        return _agent_model()

    result = await run_uc1_golden_set(
        _spec(),
        _deps(model_factory=model_factory, judge_model_factory=lambda model_id: _judge_model(score=1.0)),
        db_session,
    )

    assert result.passed_count == sum(1 for case in result.cases if case.passed)
    assert result.pass_rate == result.passed_count / result.total_count


async def test_a_low_judge_score_fails_the_correct_lookup_cases(db_session) -> None:
    def model_factory(model_id: str) -> ScriptedToolModel:
        return _agent_model()

    result = await run_uc1_golden_set(
        _spec(),
        _deps(model_factory=model_factory, judge_model_factory=lambda model_id: _judge_model(score=0.0)),
        db_session,
    )

    llm_cases = [case for case in result.cases if case.judge is GoldenCaseJudge.LLM]
    assert llm_cases and all(not case.passed for case in llm_cases)


async def test_a_case_added_through_the_generic_suite_importer_does_not_join_the_golden_run(db_session) -> None:
    """A row without a golden-case-shaped `extra` (EVAL-2's importer, or a
    hand-written manual case) sits in the same suite but is not one of the
    23 — `load_uc1_golden_cases` excludes it instead of crashing on it.
    """
    from src.db.models import EvalCase, EvalCaseOrigin
    from src.evals.uc1_suite import get_or_create_uc1_suite

    spec = _spec()
    suite = get_or_create_uc1_suite(db_session, spec.metadata.id)
    db_session.add(
        EvalCase(
            suite_id=suite.id,
            criterion="tự viết",
            input_message="câu hỏi khác",
            expected="trả lời khác",
            origin=EvalCaseOrigin.MANUAL,
        )
    )
    db_session.commit()

    def model_factory(model_id: str) -> ScriptedToolModel:
        return _agent_model()

    result = await run_uc1_golden_set(
        _spec(),
        _deps(model_factory=model_factory, judge_model_factory=lambda model_id: _judge_model(score=1.0)),
        db_session,
    )

    assert result.total_count == len(load_golden_set())
