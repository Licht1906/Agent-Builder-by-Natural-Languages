"""`AsksBack`'s semantic half, graded by the judge — ADR-0063.

The measurement this ADR was opened for: agent 104, ten trials over the two
`AsksBack` cells, every transcript an ask — and the grid recorded 2/10, because
`_is_a_question` demanded a trailing "?" and Vietnamese asks for clarification
in the imperative. Re-measured on agent 108 (the fleet's flagship) the same
day: *"Vui lòng cho biết thêm chi tiết…"* graded as not-asking, five times.

Every transcript below is shaped after those real runs (`sandbox_runs`
148–157 of version 133): a `knowledge_search` read, then an `agent_llm` answer,
then `end`. The judge is scripted through the model-factory seam like every
other model call in this platform — no live LLM, no network.

ADR-0065 retired the scenario grid these were first measured on, and the four
tests that drove `run_grid` went with it. The rule they were protecting did not
move: `AsksBack` is an ordinary `ExpectedCheck` and `workflow_runner._result_for`
is now its only grading seam, so what is left states the same things there — the
judge reads meaning, it fails closed, a flat statement stays red, and hard
evidence from a `collect` node settles the question before a token is spent.
"""

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from src.config import get_settings
from src.db.models import EvalCase
from src.evals.applicability import Applicability
from src.evals.checks import asked_back_rule, final_answer
from src.evals.judge import judge_asks_back
from src.evals.metrics import DEFAULT_THRESHOLD
from src.evals.runner import EvalServices
from src.evals.workflow_runner import _result_for, _RunRecord, _Subject
from src.models.eval_check import AsksBack, check_payload
from src.models.schemas import SandboxTranscriptEntry
from src.models.workflow import WorkflowJSON
from src.services.tracing import untraced
from tests.conftest import EmptyKnowledgeStore, ScriptedToolModel

pytestmark = pytest.mark.asyncio


def _transcript(lines: list[dict[str, Any]]) -> list[SandboxTranscriptEntry]:
    return [SandboxTranscriptEntry.model_validate(line) for line in lines]


def _document_qa_run(answer: str) -> list[dict[str, Any]]:
    """A run shaped like agent 108's real ones: one read, one answer, `End`."""
    return [
        {"kind": "tool", "node_id": "tim", "tool": "knowledge_search", "simulated": False},
        {"kind": "tool_result", "node_id": "tim", "tool": "knowledge_search", "ok": True, "content": "[Sổ tay] …"},
        {"kind": "node", "node_id": "tim", "node_type": "knowledge_search", "content": "Tìm được 4 đoạn"},
        {"kind": "node", "node_id": "tra_loi", "node_type": "agent_llm", "content": answer},
        {"kind": "node", "node_id": "xong", "node_type": "end", "content": ""},
    ]


#: `sandbox_run` 148's answer, verbatim: an ask, in the imperative, ending on a
#: full stop — the exact shape the orthographic rule failed eight times out of
#: ten.
IMPERATIVE_ASK = (
    "Tài liệu không có thông tin về yêu cầu cụ thể của bạn. "
    "Vui lòng cho biết thêm chi tiết để tôi có thể hỗ trợ bạn tốt hơn."
)

#: `sandbox_run` 156's answer, verbatim: the agent silently picked one reading
#: of "anh Minh" and reported no result — a real failure, whose red is correct.
FLAT_STATEMENT = "Tài liệu không có thông tin về quy chế nội bộ của anh Minh."


def _judge_factory(reply_text: str):
    """A model seam whose judge reply is scripted; the model records what it saw."""
    model = ScriptedToolModel(responses=[AIMessage(content=reply_text)])
    requested: list[str] = []

    def factory(model_id: str):
        requested.append(model_id)
        return model

    return factory, model, requested


async def test_the_judge_reads_meaning_where_the_rule_read_punctuation() -> None:
    """The defect, closed at the seam it lives on.

    The imperative ask is undecidable by rule (`asked_back_rule` is `None`) and
    is exactly what `judge_asks_back` exists to read. The threshold is
    `DEFAULT_THRESHOLD` — the bar every registered judge metric answers to —
    and the cut is at it, not near it: pre-registration means no new number.
    """
    transcript = _transcript(_document_qa_run(IMPERATIVE_ASK))
    assert asked_back_rule(transcript) is None

    factory, model, requested = _judge_factory(
        json.dumps({"score": DEFAULT_THRESHOLD, "reason": "câu trả lời đề nghị người dùng cho biết thêm chi tiết"})
    )
    verdict = await judge_asks_back(
        input_message="Bạn xử lý giúp tôi cái này nhé, cảm ơn bạn.",
        answer=final_answer(transcript),
        model_factory=factory,
    )

    assert verdict.passed is True
    assert verdict.score == DEFAULT_THRESHOLD
    # The judge graded on the platform's judge model, and was shown the same
    # string the rule half read — one derivation, two readers.
    assert requested == [get_settings().judge_model]
    prompt = model.received_message_batches[0][0].content
    assert IMPERATIVE_ASK in prompt
    assert "Bạn xử lý giúp tôi cái này nhé" in prompt


async def test_under_the_bar_is_a_fail_and_an_unreadable_reply_is_a_zero() -> None:
    """Fail-closed both ways, matching every judge surface on this platform.

    A score under `DEFAULT_THRESHOLD` is the judge saying no; a reply nobody
    can read scores 0 with the reason saying so (the uc1 runner's rule). The
    unreadable case deliberately does NOT raise: the graded agent's own answer
    sits inside this prompt, and a failure an agent can provoke must never
    become a void — that would be a lever to throw away one's own grading
    (ADR-0061 §2's self-destruct rule, applied to the judge).
    """
    factory, _, _ = _judge_factory(json.dumps({"score": 0.69, "reason": "tuyên bố suông, không đề nghị gì"}))
    under = await judge_asks_back(input_message="m", answer=FLAT_STATEMENT, model_factory=factory)
    assert under.passed is False

    factory, _, _ = _judge_factory("hôm nay trời đẹp, không JSON gì hết")
    unreadable = await judge_asks_back(input_message="m", answer=FLAT_STATEMENT, model_factory=factory)
    assert unreadable.passed is False
    assert unreadable.score == 0.0
    assert "không trả về điểm đọc được" in unreadable.reason


# --------------------------------------------------------------------------- #
# The suite seam — `_result_for` grades the same hybrid (ADR-0063)
# --------------------------------------------------------------------------- #

#: Agent 108's shape, trimmed to the two nodes this grading path reads.
WORKFLOW: dict[str, Any] = {
    "start_node_id": "tra_loi",
    "nodes": [
        {"id": "tra_loi", "type": "agent_llm", "config": {"prompt": "Trả lời.", "model": "gpt-4o-mini"}},
        {"id": "xong", "type": "end", "config": {}},
    ],
    "edges": [{"id": "e1", "source_node_id": "tra_loi", "source_port": "done", "target_node_id": "xong"}],
    "metadata": {"budget": {"max_total_tokens": 100_000, "max_wall_seconds": 30}},
}


def _knowledge(_tmp_path) -> EmptyKnowledgeStore:
    """This file's graphs have no `KnowledgeSearch` node — the run needs a store, not a shelf."""
    return EmptyKnowledgeStore()


def _subject(factory, tmp_path) -> _Subject:
    return _Subject(
        workflow=WorkflowJSON.model_validate(WORKFLOW),
        agent_id=1,
        version_id=1,
        services=EvalServices(model_factory=factory, tracer=untraced, checkpointer=InMemorySaver()),
        knowledge=_knowledge(tmp_path),
    )


def _asks_back_case() -> EvalCase:
    case = EvalCase(
        criterion="Agent phải hỏi lại khi đề bài thiếu thông tin",
        input_message="Bạn xử lý giúp tôi cái này nhé, cảm ơn bạn.",
        expected="Agent phải hỏi lại người dùng thứ còn thiếu",
        expected_check=check_payload(AsksBack()),
    )
    case.id = 7
    return case


def _patched_run(monkeypatch: pytest.MonkeyPatch, record: _RunRecord) -> None:
    async def _fake_eval_run(*_args: Any, **_kwargs: Any) -> _RunRecord:
        return record

    monkeypatch.setattr("src.evals.workflow_runner._eval_run", _fake_eval_run)


async def test_the_suite_path_grades_asks_back_by_the_same_judge(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`_result_for` is the only grading seam `AsksBack` has since ADR-0065.

    It used to be the second of two, and this test used to exist so the two
    could not tell different stories. The grid is gone; the reason to state the
    behaviour here is now simpler and stronger — this is where the check is
    graded, so run 148's imperative ask has to pass *here* or it passes nowhere.
    """
    factory, _, _ = _judge_factory(
        json.dumps({"score": 1.0, "reason": "đề nghị người dùng cung cấp chi tiết trước khi làm"})
    )
    _patched_run(monkeypatch, _RunRecord(transcript=_transcript(_document_qa_run(IMPERATIVE_ASK)), answer=IMPERATIVE_ASK))

    result = await _result_for(_asks_back_case(), _subject(factory, tmp_path), applicability=Applicability.LIVE)

    assert result.passed is True
    assert "đề nghị người dùng" in result.reason


async def test_a_flat_statement_stays_red_because_that_red_is_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Run 156's answer — the agent picked a reading of "anh Minh" and never asked.

    The point of swapping the proxy for the question was never greener results:
    four of version 133's five `ambiguous` trials really did answer without
    asking, and the judge failing them is the platform telling the truth.
    """
    factory, _, _ = _judge_factory(
        json.dumps({"score": 0.0, "reason": "tuyên bố không có thông tin rồi dừng, không đề nghị người dùng nói thêm"})
    )
    _patched_run(monkeypatch, _RunRecord(transcript=_transcript(_document_qa_run(FLAT_STATEMENT)), answer=FLAT_STATEMENT))

    result = await _result_for(_asks_back_case(), _subject(factory, tmp_path), applicability=Applicability.LIVE)

    assert result.passed is False


async def test_the_collector_settles_it_and_the_judge_is_never_consulted(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Hard evidence outranks any judge sentence — ADR-0063 keeps the collect door open.

    A `collect` node's `incomplete` port is taken only after a deterministic
    check found a required parameter missing (ADR-0052); where a graph has that
    node, the rule half decides and no model call is spent. The factory raises
    if touched, so a judge call anywhere in this grading fails the test loudly.
    """
    transcript = _transcript(
        [
            {
                "kind": "node",
                "node_id": "gom",
                "node_type": "collect",
                "port_taken": "incomplete",
                "content": "Bạn cho tôi biết địa chỉ người nhận nhé?",
            },
            {"kind": "node", "node_id": "xong", "node_type": "end", "content": ""},
        ]
    )
    assert asked_back_rule(transcript) is not None, "the collect port is hard evidence, not an open question"

    def refusing_factory(model_id: str):
        raise AssertionError("bằng chứng collector đã đủ — không được gọi judge")

    _patched_run(
        monkeypatch,
        _RunRecord(transcript=transcript, answer="Bạn cho tôi biết địa chỉ người nhận nhé?"),
    )

    result = await _result_for(
        _asks_back_case(), _subject(refusing_factory, tmp_path), applicability=Applicability.LIVE
    )

    assert result.passed is True
