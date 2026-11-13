"""An agent with several jobs, run accurately — measured 2026-11-13 on the full công nợ agent.

That agent answers questions, sends a reminder and files a call note. Live in the
sandbox it got the first right and the other two wrong, and every wrong value
traced to something these tests pin:

- the note was filed as `promised_date: 2023-11-20` — the fillers had no date;
- the note was filed as `customer_id: "ABC"` — the write filler never saw the
  rows the read had just returned;
- the letter sent was not the letter approved — the send re-wrote `body`
  instead of taking the draft the card showed;
- the second turn ("ketoan@abc.example") read the customer whose address it
  was, not the invoices the first turn asked about — and a branch decision
  saw only that address;
- the composer read once before branching and classified the request with an
  `agent_llm` that talked to the user.
"""

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage

from src.models.node_registry import (
    DataQueryConfig,
    NodeType,
    is_optional_field,
    is_prose_field,
    unfilled_fields,
)
from src.models.workflow import Edge, NodeInstance, WorkflowJSON
from src.workflow import runtime
from src.workflow.composition import _read_placement_complaints
from src.workflow.runtime import (
    _asked_with_notes,
    _condition_context,
    _drafted_values,
    _today_line,
)

ROWS = {
    "node_id": "doc_khach",
    "source": "database:2/v_customer_safe",
    "text": "customer_id | customer_name\nKH-0001 | Công ty ABC",
    "doc_name": "Database công ty A · v_customer_safe",
    "section_path": "lọc: customer_name like %ABC%",
    "page_number": None,
}


def _state(*messages: Any, findings: list[Any] | None = None) -> dict[str, Any]:
    return {"messages": list(messages), "findings": findings or [], "approvals": {}}


def _graph(nodes: list[tuple[str, NodeType]], edges: list[tuple[str, str, str]], **configs: Any) -> WorkflowJSON:
    return WorkflowJSON(
        start_node_id=nodes[0][0],
        nodes=[NodeInstance(id=node_id, type=kind, config=configs.get(node_id, {})) for node_id, kind in nodes],
        edges=[Edge(id=f"{a}-{p}-{b}", source_node_id=a, source_port=p, target_node_id=b) for a, p, b in edges],
    )


# --------------------------------------------------------------------------- #
# What the fillers are shown
# --------------------------------------------------------------------------- #


def test_a_query_or_write_is_written_knowing_today_the_rows_read_and_the_steps_purpose(monkeypatch: Any) -> None:
    now = datetime(2026, 11, 13, 9, 30, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    monkeypatch.setattr(runtime, "_now_in_app_timezone", lambda: now)
    state = _state(HumanMessage("Ghi lại: Công ty ABC hẹn trả ngày 20/11"), findings=[ROWS])

    asked = _asked_with_notes(SimpleNamespace(databases=None), "2", state, purpose="Mã khách hàng của khách được nhắc")

    assert "13/11/2026" in asked
    assert "KH-0001 | Công ty ABC" in asked
    assert "Mã khách hàng của khách được nhắc" in asked
    assert "Công ty ABC hẹn trả ngày 20/11" in asked
    # Context first, the conversation last — the order every filler reads.
    assert asked.index("13/11/2026") < asked.index("KH-0001") < asked.index("hẹn trả ngày 20/11")


def test_today_is_said_with_weekday_and_zone() -> None:
    line = _today_line(datetime(2026, 11, 13, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")))
    assert "Thứ Sáu, 13/11/2026 (UTC+07:00)" in line


def test_a_branch_decision_on_a_follow_up_sees_the_request_it_completes() -> None:
    state = _state(
        HumanMessage("Soạn email nhắc Công ty ABC"),
        AIMessage("Bạn vui lòng cho tôi biết email người nhận."),
        HumanMessage("ketoan@abc.example"),
    )
    context = _condition_context(state)
    assert "Soạn email nhắc Công ty ABC" in context
    assert "ketoan@abc.example" in context


def test_a_request_of_its_own_is_decided_on_its_own() -> None:
    state = _state(
        HumanMessage("Soạn email nhắc Công ty ABC"),
        AIMessage("Đã soạn."),
        HumanMessage("Khách nào đang quá hạn trên 30 ngày trong tháng này vậy em?"),
    )
    assert "Soạn email nhắc" not in _condition_context(state)


# --------------------------------------------------------------------------- #
# The letter sent is the letter approved
# --------------------------------------------------------------------------- #


def test_a_labelled_draft_is_split_into_subject_and_body_verbatim() -> None:
    draft = "**Tiêu đề:** Nhắc thanh toán HĐ-0001\n**Nội dung:**\nKính gửi Công ty ABC,\nSố tiền: 12.000.000 đ."
    assert _drafted_values(draft, ["subject", "body"]) == {
        "subject": "Nhắc thanh toán HĐ-0001",
        "body": "Kính gửi Công ty ABC,\nSố tiền: 12.000.000 đ.",
    }


def test_english_labels_and_a_blank_body_line_are_read_too() -> None:
    draft = "Subject: Nhắc nhở thanh toán\n\nBody: \n\nKính gửi Công ty ABC,\n\nTrân trọng"
    assert _drafted_values(draft, ["subject", "body"]) == {
        "subject": "Nhắc nhở thanh toán",
        "body": "Kính gửi Công ty ABC,\n\nTrân trọng",
    }


def test_a_subject_line_then_the_letter_is_subject_and_body() -> None:
    assert _drafted_values("Tiêu đề: Nhắc nợ\nKính gửi ABC,\nVui lòng thanh toán.", ["subject", "body"]) == {
        "subject": "Nhắc nợ",
        "body": "Kính gửi ABC,\nVui lòng thanh toán.",
    }


def test_one_composed_parameter_is_the_whole_draft() -> None:
    assert _drafted_values("Kính gửi ABC, vui lòng thanh toán.", ["body"]) == {
        "body": "Kính gửi ABC, vui lòng thanh toán."
    }


def test_an_unlabelled_draft_for_two_parameters_is_left_to_the_filler() -> None:
    # Guessing where a subject ends is how a letter goes out without one.
    assert _drafted_values("Kính gửi ABC, vui lòng thanh toán.", ["subject", "body"]) == {}


def test_a_parameter_nobody_composes_is_never_read_off_the_draft() -> None:
    assert _drafted_values("Gửi tới: ketoan@abc.example\nNội dung: x", ["to"]) == {}


# --------------------------------------------------------------------------- #
# A read says what it is for
# --------------------------------------------------------------------------- #


def test_purpose_is_prose_the_composer_writes_and_blank_still_confirms() -> None:
    assert is_prose_field(NodeType.DATA_QUERY, "purpose")
    assert is_optional_field(NodeType.DATA_QUERY, "purpose")
    assert "purpose" not in unfilled_fields(NodeType.DATA_QUERY, {"source_id": "2", "views": ["v"]})
    assert DataQueryConfig.model_json_schema()["properties"]["purpose"]["multiline"] is True


# --------------------------------------------------------------------------- #
# The composer's shape: route first, one read per branch
# --------------------------------------------------------------------------- #

BRANCHED = [
    ("la_email", NodeType.IF_ELSE),
    ("thu_thap", NodeType.COLLECT),
    ("doc_no", NodeType.DATA_QUERY),
    ("soan_thu", NodeType.AGENT_LLM),
    ("duyet_thu", NodeType.HUMAN_APPROVAL),
    ("gui", NodeType.MCP),
    ("la_ghi_chu", NodeType.IF_ELSE),
    ("doc_khach", NodeType.DATA_QUERY),
    ("duyet_ghi", NodeType.HUMAN_APPROVAL),
    ("ghi", NodeType.DATA_WRITE),
    ("doc_hoi", NodeType.DATA_QUERY),
    ("tra_loi", NodeType.AGENT_LLM),
    ("xong", NodeType.END),
]
BRANCHED_EDGES = [
    ("la_email", "true", "thu_thap"),
    ("thu_thap", "complete", "doc_no"),
    ("doc_no", "done", "soan_thu"),
    ("soan_thu", "done", "duyet_thu"),
    ("duyet_thu", "approved", "gui"),
    ("la_email", "false", "la_ghi_chu"),
    ("la_ghi_chu", "true", "doc_khach"),
    ("doc_khach", "done", "duyet_ghi"),
    ("duyet_ghi", "approved", "ghi"),
    ("la_ghi_chu", "false", "doc_hoi"),
    ("doc_hoi", "done", "tra_loi"),
    ("tra_loi", "done", "xong"),
]
PURPOSES = {
    "doc_no": {"purpose": "Các hoá đơn còn nợ của khách được nhắc tên, để soạn thư nhắc nợ"},
    "doc_khach": {"purpose": "Mã khách hàng của khách được nhắc tên, để ghi ghi chú"},
    "doc_hoi": {"purpose": "Dữ liệu công nợ trả lời câu hỏi của người dùng"},
}


def test_one_read_per_branch_each_with_its_purpose_is_accepted() -> None:
    assert _read_placement_complaints(_graph(BRANCHED, BRANCHED_EDGES, **PURPOSES)) == []


def test_reads_on_several_branches_must_each_say_what_they_are_for() -> None:
    complaints = _read_placement_complaints(_graph(BRANCHED, BRANCHED_EDGES, doc_no=PURPOSES["doc_no"]))
    assert any("chưa có purpose" in complaint and "doc_khach" in complaint for complaint in complaints)


def test_one_read_in_front_of_two_jobs_is_sent_back() -> None:
    graph = _graph(
        [
            ("doc", NodeType.DATA_QUERY),
            ("la_email", NodeType.IF_ELSE),
            ("duyet_thu", NodeType.HUMAN_APPROVAL),
            ("gui", NodeType.MCP),
            ("duyet_ghi", NodeType.HUMAN_APPROVAL),
            ("ghi", NodeType.DATA_WRITE),
        ],
        [
            ("doc", "done", "la_email"),
            ("la_email", "true", "duyet_thu"),
            ("duyet_thu", "approved", "gui"),
            ("la_email", "false", "duyet_ghi"),
            ("duyet_ghi", "approved", "ghi"),
        ],
    )
    complaints = _read_placement_complaints(graph)
    assert any("đứng trước nhiều việc khác nhau" in complaint for complaint in complaints)


def test_an_agent_llm_classifying_in_front_of_the_router_is_sent_back() -> None:
    graph = _graph(
        [("phan_loai", NodeType.AGENT_LLM), ("la_email", NodeType.IF_ELSE), ("xong", NodeType.END)],
        [("phan_loai", "done", "la_email"), ("la_email", "true", "xong"), ("la_email", "false", "xong")],
    )
    assert any("chỉ để phân loại" in complaint for complaint in _read_placement_complaints(graph))
