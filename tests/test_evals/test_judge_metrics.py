"""`judge_context_precision`/`judge_task_completion` (ADR-0054 §4), and `Settings.judge_model` wiring.

Scripted through the model-factory seam like every other model call in this
platform — no live LLM, no network.
"""

import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from src.config import Settings, get_settings
from src.evals.judge import ToolEvidence, judge_answer, judge_context_precision, judge_task_completion
from src.evals.metrics import DEFAULT_THRESHOLD
from src.models.model_catalog import JUDGE_MODEL, META_AGENT_MODEL
from tests.conftest import ScriptedToolModel


def _factory_returning(reply_text: str):
    model = ScriptedToolModel(responses=[AIMessage(content=reply_text)])

    def factory(model_id: str):
        return model

    return factory, model


def _factory_recording():
    requested: list[str] = []
    model = ScriptedToolModel(responses=[AIMessage(content=json.dumps({"score": 1.0, "reason": "ok"}))])

    def factory(model_id: str):
        requested.append(model_id)
        return model

    return factory, requested


async def test_the_default_judge_model_is_a_dated_snapshot() -> None:
    """EV-04 / ADR-0069 §9: the judge ships pinned to a dated snapshot, never an alias the provider moves.

    Replaces ADR-0054 §4's "the knob ships pointing at the meta agent's model":
    `gpt-6-luna` has no dated snapshot, and a drifting judge would quietly make
    every baseline mean something else. Asserted on the field's declared
    default, not on `get_settings()`, for the reason that test gave — a
    deployment's own `.env` sets it.
    """
    assert Settings.model_fields["judge_model"].default == JUDGE_MODEL
    assert JUDGE_MODEL.endswith("-2025-04-14")


@pytest.mark.parametrize(
    ("written", "pinned"),
    [("gpt-4.1-mini", "gpt-4.1-mini-2025-04-14"), ("gpt-4.1-mini-2025-04-14", "gpt-4.1-mini-2025-04-14")],
)
async def test_an_alias_the_platform_knows_is_pinned_to_its_snapshot(written: str, pinned: str) -> None:
    assert Settings(judge_model=written).judge_model == pinned


async def test_an_undated_judge_is_refused_at_startup() -> None:
    with pytest.raises(ValidationError, match="snapshot có ngày"):
        Settings(judge_model=META_AGENT_MODEL)


async def test_context_precision_passes_at_or_above_the_threshold() -> None:
    factory, _ = _factory_returning(json.dumps({"score": DEFAULT_THRESHOLD, "reason": "mọi đoạn đều cần thiết"}))

    verdict = await judge_context_precision(
        expected="nêu đúng chính sách đổi trả",
        retrieval_context=["[so-tay.md] Khách được đổi trả trong 30 ngày."],
        answer="Khách được đổi trả trong 30 ngày.",
        model_factory=factory,
    )

    assert verdict.score == DEFAULT_THRESHOLD
    assert verdict.passed is True
    assert "cần thiết" in verdict.reason


async def test_context_precision_fails_under_the_threshold_and_names_the_extra_passage() -> None:
    factory, _ = _factory_returning(
        json.dumps({"score": 0.3, "reason": "đoạn [chinh-sach-luong.md] thừa, không liên quan tới đổi trả"})
    )

    verdict = await judge_context_precision(
        expected="nêu đúng chính sách đổi trả",
        retrieval_context=[
            "[so-tay.md] Khách được đổi trả trong 30 ngày.",
            "[chinh-sach-luong.md] Lương trả vào ngày 5 hàng tháng.",
        ],
        answer="Khách được đổi trả trong 30 ngày.",
        model_factory=factory,
    )

    assert verdict.passed is False
    assert "thừa" in verdict.reason


async def test_context_precision_with_no_passages_still_asks_and_scores_what_it_is_told() -> None:
    factory, _ = _factory_returning(json.dumps({"score": 0.0, "reason": "không tìm được đoạn nào"}))

    verdict = await judge_context_precision(
        expected="nêu đúng chính sách đổi trả", retrieval_context=[], answer="", model_factory=factory
    )

    assert verdict.passed is False


async def test_task_completion_reads_the_whole_run_not_just_the_final_sentence() -> None:
    factory, _ = _factory_returning(json.dumps({"score": 0.9, "reason": "đã tra sổ tay và trả lời đúng yêu cầu"}))

    verdict = await judge_task_completion(
        input_message="đổi trả trong bao lâu?",
        expected="trả lời đúng số ngày",
        answer="30 ngày.",
        model_factory=factory,
        evidence=[ToolEvidence(tool="knowledge_search", args={}, ok=True, result="[so-tay.md] 30 ngày")],
    )

    assert verdict.passed is True
    assert verdict.score == 0.9


async def test_task_completion_fails_when_the_run_stopped_short() -> None:
    factory, _ = _factory_returning(json.dumps({"score": 0.2, "reason": "dừng giữa chừng, chưa gửi câu trả lời"}))

    verdict = await judge_task_completion(
        input_message="gửi báo cáo tuần cho sếp",
        expected="đã gửi thư báo cáo",
        answer="",
        model_factory=factory,
    )

    assert verdict.passed is False


async def test_an_unreadable_metric_reply_scores_zero_rather_than_crashing() -> None:
    factory, _ = _factory_returning("không phải JSON")

    verdict = await judge_context_precision(expected="x", retrieval_context=["y"], answer="z", model_factory=factory)

    assert verdict.score == 0.0
    assert verdict.passed is False


async def test_judge_answer_and_the_new_metrics_all_request_settings_judge_model() -> None:
    """ADR-0054 §4: the judge runs on `Settings.judge_model`, not a hard-coded constant."""
    factory, requested = _factory_recording()
    await judge_answer(criterion="c", expected="e", input_message="i", answer="a", model_factory=factory)
    await judge_context_precision(expected="e", retrieval_context=[], answer="a", model_factory=factory)
    await judge_task_completion(input_message="i", expected="e", answer="a", model_factory=factory)

    assert requested == [get_settings().judge_model] * 3
