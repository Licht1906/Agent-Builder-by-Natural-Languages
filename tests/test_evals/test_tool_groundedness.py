"""A link or id the answer names must be one a tool really returned — ADR-0052, principle 3's deterministic half.

The field case: the first Drive agent answered `create_doc` with
*"…/document/d/your_document_id_here"*. The judge read prose and passed it.
"""

from src.evals.checks import grade_tool_groundedness
from src.models.schemas import SandboxTranscriptEntry

DOC_ID = "1C9SkfMqOB40thznzTRt-uHZ6NGxwD_b38a4cVzwMa2E"
LINK = f"https://docs.google.com/document/d/{DOC_ID}/edit?usp=drivesdk"


def result(tool: str, content: str) -> SandboxTranscriptEntry:
    return SandboxTranscriptEntry(kind="tool_result", node_id="n", tool=tool, ok=True, simulated=False, content=content)


def test_no_tool_result_means_nothing_to_check() -> None:
    assert grade_tool_groundedness([], "xem https://example.com/abc") is None
    only_search = [result("knowledge_search", "[Điều 1] …")]
    assert grade_tool_groundedness(only_search, "xem https://example.com/abc") is None


def test_an_invented_link_fails_with_the_link_named() -> None:
    transcript = [result("google_drive.create_doc", '{"file_id": "doc-mo-phong-001", "name": "Tài liệu"}')]

    verdict = grade_tool_groundedness(transcript, "Đã tạo: https://docs.google.com/document/d/your_document_id_here")

    assert verdict is not None and verdict.passed is False
    assert "your_document_id_here" in verdict.reason


def test_a_real_link_passes_even_when_the_model_dropped_the_query_string() -> None:
    transcript = [result("google_drive.create_doc", f'{{"file_id": "{DOC_ID}", "link": "{LINK}"}}')]

    trimmed = f"Xem tại https://docs.google.com/document/d/{DOC_ID}/edit."
    verdict = grade_tool_groundedness(transcript, trimmed)

    assert verdict is not None and verdict.passed is True


def test_an_id_the_user_typed_is_grounded_even_if_the_tool_failed_to_echo_it() -> None:
    transcript = [result("gmail.read_email", '{"error": "not found"}')]

    verdict = grade_tool_groundedness(
        transcript, "Không tìm thấy thư 1a028e02a9473d8f.", input_message="đọc thư 1a028e02a9473d8f"
    )

    assert verdict is not None and verdict.passed is True


def test_an_invented_message_id_fails() -> None:
    transcript = [result("gmail.get_recent_emails", '{"messages": [{"message_id": "1a028e02a9473d8f"}]}')]

    verdict = grade_tool_groundedness(transcript, "Thư mới nhất có mã 18c2f9a0b1e4d7c3.")

    assert verdict is not None and verdict.passed is False
    assert "18c2f9a0b1e4d7c3" in verdict.reason


def test_ordinary_words_dates_and_short_numbers_are_not_claims() -> None:
    transcript = [result("gmail.get_profile", '{"emailAddress": "a@b.vn", "messagesTotal": 3364}')]

    verdict = grade_tool_groundedness(
        transcript, "Hộp thư a@b.vn có 3364 thư, cập nhật 22/08/2026 lúc 16:49, phòng 301."
    )

    assert verdict is not None and verdict.passed is True
