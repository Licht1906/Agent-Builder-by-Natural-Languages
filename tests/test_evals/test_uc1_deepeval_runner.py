"""UC1-29 (#130) — one test per rule `judge_uc1_case`/`judge_uc1_cases` enforce.

Scripted through the model-factory seam (`ScriptedToolModel`, `tests.conftest`)
like every other model call in this platform — no live LLM, no network.
"""

import json

from langchain_core.messages import AIMessage

from src.evals.uc1_deepeval_runner import METRIC_PASS_THRESHOLD, judge_uc1_case, judge_uc1_cases
from src.evals.uc1_golden_set import GoldenCaseGroup, GoldenCaseJudge, load_golden_set
from tests.conftest import ScriptedToolModel


def _lookup_case():
    cases = load_golden_set()
    return next(case for case in cases if case.group is GoldenCaseGroup.CORRECT_LOOKUP)


def _factory_returning(reply_text: str):
    model = ScriptedToolModel(responses=[AIMessage(content=reply_text)])

    def factory(model_id: str) -> ScriptedToolModel:
        return model

    return factory, model


async def test_a_grounded_relevant_answer_scores_high_on_both_metrics_and_passes() -> None:
    case = _lookup_case()
    verdict = json.dumps(
        {
            "faithfulness": {"score": 1.0, "reason": "mọi số liệu khớp ngữ cảnh"},
            "answer_relevancy": {"score": 0.95, "reason": "trả lời đúng trọng tâm câu hỏi"},
        }
    )
    factory, _ = _factory_returning(verdict)

    result = await judge_uc1_case(case, answer=case.expected_output, model_factory=factory)

    assert result.case_id == case.id
    assert result.faithfulness.score == 1.0
    assert result.faithfulness.reason == "mọi số liệu khớp ngữ cảnh"
    assert result.answer_relevancy.score == 0.95
    assert result.passed is True


async def test_a_score_under_the_per_metric_threshold_fails_the_case() -> None:
    case = _lookup_case()
    verdict = json.dumps(
        {
            "faithfulness": {"score": METRIC_PASS_THRESHOLD - 0.01, "reason": "thiếu căn cứ"},
            "answer_relevancy": {"score": 1.0, "reason": "đúng trọng tâm"},
        }
    )
    factory, _ = _factory_returning(verdict)

    result = await judge_uc1_case(case, answer="một câu trả lời lạc đề", model_factory=factory)

    assert result.passed is False


async def test_an_unreadable_reply_scores_zero_on_both_metrics_rather_than_crashing() -> None:
    case = _lookup_case()
    factory, _ = _factory_returning("tôi không trả JSON gì cả")

    result = await judge_uc1_case(case, answer="bất kỳ", model_factory=factory)

    assert result.faithfulness.score == 0.0
    assert result.answer_relevancy.score == 0.0
    assert result.passed is False


async def test_the_judge_model_is_the_platforms_own_not_a_model_from_the_spec() -> None:
    case = _lookup_case()
    requested_model_ids: list[str] = []
    model = ScriptedToolModel(
        responses=[
            AIMessage(
                content=json.dumps(
                    {
                        "faithfulness": {"score": 1.0, "reason": "ok"},
                        "answer_relevancy": {"score": 1.0, "reason": "ok"},
                    }
                )
            )
        ]
    )

    def factory(model_id: str) -> ScriptedToolModel:
        requested_model_ids.append(model_id)
        return model

    await judge_uc1_case(case, answer="bất kỳ", model_factory=factory)

    assert requested_model_ids  # a model was actually requested
    assert "spec" not in requested_model_ids[0].lower()


async def test_judge_uc1_cases_scores_the_whole_group_in_order_and_treats_a_missing_answer_as_unanswered() -> None:
    cases = [case for case in load_golden_set() if case.group is GoldenCaseGroup.CORRECT_LOOKUP]
    assert len(cases) == 8
    verdict = json.dumps(
        {"faithfulness": {"score": 1.0, "reason": "ok"}, "answer_relevancy": {"score": 1.0, "reason": "ok"}}
    )
    model = ScriptedToolModel(responses=[AIMessage(content=verdict) for _ in cases])

    def factory(model_id: str) -> ScriptedToolModel:
        return model

    # Deliberately omit an answer for the last case — a case UC1-17's
    # orchestration never ran must still be scored, not silently skipped.
    answers = {case.id: case.expected_output for case in cases[:-1]}
    results = await judge_uc1_cases(cases, answers=answers, model_factory=factory)

    assert [result.case_id for result in results] == [case.id for case in cases]
    assert len(results) == 8


def test_every_case_scored_by_this_runner_is_flagged_judge_llm() -> None:
    cases = [case for case in load_golden_set() if case.group is GoldenCaseGroup.CORRECT_LOOKUP]
    assert all(case.judge is GoldenCaseJudge.LLM for case in cases)
