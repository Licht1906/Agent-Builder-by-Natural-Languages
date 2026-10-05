"""`suggest_answers`: click-to-pick answers for the V2 interview's open question.

One test per rule the module owns — the shape it returns, which fields allow
multiple picks, where `recommended` is allowed to land, and the two ways it
declines to suggest anything. The model is a scripted double, so what is measured
is the code around it, not the model.
"""

import json

import pytest
from langchain_core.messages import AIMessage

from src.solution.designer import Question
from src.solution.draft import SpecDraft, SpecField
from src.solution.suggestions import suggest_answers
from tests.conftest import ScriptedMetaAgent

pytestmark = pytest.mark.asyncio

DESCRIPTION = "Trợ lý cho nhân viên chăm sóc khách hàng: đọc email khiếu nại rồi soạn thư trả lời."

GOAL_Q = Question(field=SpecField.GOAL, text="Bài toán nghiệp vụ bạn muốn agent này giải là gì?")
USERS_Q = Question(field=SpecField.USERS, text="Ai sẽ dùng agent này?")
CONFLICT_Q = Question(field=None, text="Chỗ này chưa khớp nhau, bạn nói rõ giúp: ...")


def _reply(**document: object) -> AIMessage:
    return AIMessage(content=json.dumps(document, ensure_ascii=False))


async def _suggest(*replies: AIMessage, question: Question, known: SpecDraft = SpecDraft.empty()) -> object:
    return await suggest_answers(
        description=DESCRIPTION,
        turns=(),
        known=known,
        question=question,
        model_factory=ScriptedMetaAgent(*replies),
    )


async def test_keeps_the_models_order_and_passes_the_top_recommendation_through() -> None:
    result = await _suggest(
        _reply(
            suggestions=[
                {"text": "Trả lời mọi khiếu nại trong một ngày làm việc", "recommended": True},
                {"text": "Giảm thời gian chờ trung bình của khách", "recommended": False},
            ]
        ),
        question=GOAL_Q,
    )
    assert [item.text for item in result.items] == [
        "Trả lời mọi khiếu nại trong một ngày làm việc",
        "Giảm thời gian chờ trung bình của khách",
    ]
    assert [item.recommended for item in result.items] == [True, False]


async def test_a_list_field_allows_multiple_picks() -> None:
    result = await _suggest(_reply(suggestions=[{"text": "Nhân viên CSKH", "recommended": False}]), question=USERS_Q)
    assert result.multi is True


async def test_the_scalar_goal_field_does_not() -> None:
    result = await _suggest(_reply(suggestions=[{"text": "x", "recommended": False}]), question=GOAL_Q)
    assert result.multi is False


async def test_a_recommendation_below_the_top_is_dropped_not_promoted() -> None:
    result = await _suggest(
        _reply(
            suggestions=[
                {"text": "A", "recommended": False},
                {"text": "B", "recommended": True},
            ]
        ),
        question=GOAL_Q,
    )
    assert [item.recommended for item in result.items] == [False, False]


async def test_an_unreadable_reply_yields_no_suggestions() -> None:
    result = await _suggest(AIMessage(content="Đây là vài gợi ý: 1. ... 2. ..."), question=GOAL_Q)
    assert result.items == ()
    assert result.field is SpecField.GOAL


async def test_a_contradiction_question_never_reaches_the_model() -> None:
    factory = ScriptedMetaAgent()  # no scripted replies: a call would raise
    result = await suggest_answers(
        description=DESCRIPTION,
        turns=(),
        known=SpecDraft.empty(),
        question=CONFLICT_Q,
        model_factory=factory,
    )
    assert result.items == ()
    assert result.field is None
    assert factory.requested_model_ids == []
