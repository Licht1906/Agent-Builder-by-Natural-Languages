"""UC1-27 (#128) — the six §11.1 guardrails as actually wired into the compiled
graph's agent node (`src.compiler.graph`), on top of `test_guardrails.py`'s own
coverage of each function in isolation.

`ScriptedToolModel.received_message_batches` (`tests.conftest`) is how a test
observes what the model was actually handed — the only way to prove `pii_redactor`
ran *before* the model saw anything, matching this ticket's own AC.
"""

from langchain_core.messages import AIMessage

from src.compiler.tools import default_tool_implementations
from tests.conftest import ScriptedToolModel
from tests.test_compiler.test_graph import _allow_pep, _compile, _run_turn, _spec


def _tool_implementations():
    return default_tool_implementations(resolve_credential=lambda ref: "postgresql://ro:pw@erp/db")


def test_pii_in_the_users_own_message_is_redacted_before_it_reaches_the_model() -> None:
    """AC: "câu hỏi chứa PII giả → bị redact trước khi tới LLM"."""
    model = ScriptedToolModel(responses=[AIMessage(content="Bạn không có công nợ quá hạn.")])
    compiled = _compile(_spec(), model=model, tool_implementations=_tool_implementations(), pep_client=_allow_pep())

    _run_turn(compiled, "Số điện thoại của tôi là 0912345678, cho tôi biết công nợ.")

    assert model.received_message_batches, "the model was never invoked"
    sent_message = model.received_message_batches[0][-1]
    assert "0912345678" not in str(sent_message.content)
    assert "[phone_redacted]" in str(sent_message.content)


def test_ordinary_questions_reach_the_model_unmodified() -> None:
    model = ScriptedToolModel(responses=[AIMessage(content="7 khách hàng quá hạn, nguồn v_ar_open.")])
    compiled = _compile(_spec(), model=model, tool_implementations=_tool_implementations(), pep_client=_allow_pep())

    _run_turn(compiled, "Khách nào quá hạn trên 30 ngày?")

    sent_message = model.received_message_batches[0][-1]
    assert sent_message.content == "Khách nào quá hạn trên 30 ngày?"


def test_pii_in_the_final_answer_is_masked_before_it_reaches_the_user() -> None:
    """AC: "câu trả lời có PII giả → bị mask trước khi tới người dùng"."""
    model = ScriptedToolModel(responses=[AIMessage(content="Liên hệ khách qua contact@example.com nhé.")])
    compiled = _compile(_spec(), model=model, tool_implementations=_tool_implementations(), pep_client=_allow_pep())

    final_state = _run_turn(compiled, "Thông tin liên hệ khách hàng là gì?")

    final_message = final_state["messages"][-1]
    assert "contact@example.com" not in str(final_message.content)
    assert "[email_masked]" in str(final_message.content)


def test_a_denied_topic_short_circuits_the_turn_without_ever_calling_the_model() -> None:
    spec = _spec(prohibitions={"external_egress": False, "deny_topics": ["salary"]})
    model = ScriptedToolModel(responses=[AIMessage(content="não câu trả lời nào sẽ được sinh ra")])
    compiled = _compile(spec, model=model, tool_implementations=_tool_implementations(), pep_client=_allow_pep())

    final_state = _run_turn(compiled, "Cho tôi biết salary của nhân viên phòng kinh doanh.")

    assert model.received_message_batches == [], "a denied topic must never reach the model"
    final_message = final_state["messages"][-1]
    assert "chuyển yêu cầu tới bộ phận phụ trách" in str(final_message.content)


def test_a_low_confidence_reply_is_escalated_instead_of_shown_verbatim() -> None:
    model = ScriptedToolModel(
        responses=[AIMessage(content="có lẽ khoảng 5 khách", response_metadata={"confidence": 0.2})]
    )
    compiled = _compile(_spec(), model=model, tool_implementations=_tool_implementations(), pep_client=_allow_pep())

    final_state = _run_turn(compiled, "Khách nào quá hạn?")

    final_message = final_state["messages"][-1]
    assert "có lẽ khoảng 5 khách" not in str(final_message.content)
    assert "chuyển yêu cầu tới bộ phận phụ trách" in str(final_message.content)


def test_a_confident_reply_is_shown_as_is() -> None:
    model = ScriptedToolModel(
        responses=[AIMessage(content="7 khách hàng quá hạn, nguồn v_ar_open.", response_metadata={"confidence": 0.95})]
    )
    compiled = _compile(_spec(), model=model, tool_implementations=_tool_implementations(), pep_client=_allow_pep())

    final_state = _run_turn(compiled, "Khách nào quá hạn?")

    final_message = final_state["messages"][-1]
    assert final_message.content == "7 khách hàng quá hạn, nguồn v_ar_open."


def test_a_numeric_answer_with_no_source_gets_a_citation_disclaimer() -> None:
    model = ScriptedToolModel(responses=[AIMessage(content="7 khách hàng quá hạn.")])
    compiled = _compile(_spec(), model=model, tool_implementations=_tool_implementations(), pep_client=_allow_pep())

    final_state = _run_turn(compiled, "Khách nào quá hạn?")

    final_message = final_state["messages"][-1]
    assert "Không có trích dẫn nguồn" in str(final_message.content)


def test_a_successful_read_records_the_peps_own_row_filter_on_the_transcript_step() -> None:
    pep = _allow_pep(row_filter={"scope": "own_records"})
    compiled = _compile(
        _spec(),
        model=ScriptedToolModel(
            responses=[
                AIMessage(content="", tool_calls=[{"name": "erp.query_receivables", "args": {}, "id": "c1"}]),
                AIMessage(content="7 khách hàng quá hạn, nguồn v_ar_open."),
            ]
        ),
        tool_implementations=_tool_implementations(),
        pep_client=pep,
    )

    final_state = _run_turn(compiled, "Khách nào quá hạn trên 30 ngày?")

    tool_step = next(step for step in final_state["transcript"] if step["node"] == "tools")
    assert tool_step["row_filter"] == {"scope": "own_records"}
