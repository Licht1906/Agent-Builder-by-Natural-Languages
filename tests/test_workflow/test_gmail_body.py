"""Email đi ra là `text/plain`, nên nó phải *là* plain text.

Ảnh chụp màn hình từ một lần chạy thật: bản nháp của agent viết
`- **Thời gian:** 19h, thứ Bảy`. `EmailMessage.set_content` gửi đúng
những gì nó nhận, Gmail chuyển đi dưới dạng text/plain, nên người được
mời đọc dấu sao. Model coi đó là định dạng; không ai bên nào muốn nó ở đó.

Cùng lúc nó cũng là lỗi hiển thị trong khung chat — chỗ đó sửa bằng
`frontend/src/components/Markdown.jsx`, dựng phần nhấn mạnh ra cho người
đọc. Ở đây là nửa còn lại: thứ rời khỏi nền tảng.
"""

import base64
import email
from email import policy

import pytest

from src.workflow.google_tools import GoogleToolError, _plain_text, _rfc822


def sent_body(**args: str) -> str:
    """Thân thư như Gmail sẽ nhận, đọc ngược từ payload `_rfc822` dựng ra."""
    raw = base64.urlsafe_b64decode(_rfc822(args).encode("ascii"))
    # `policy.default` là thứ dựng lại `EmailMessage` và giải mã tiêu đề
    # RFC 2047 — chính sách mặc định (compat32) trả về `Message` thô và
    # sẽ để bài test đọc `=?utf-8?q?...?=` thay vì chữ người ta nhận được.
    return email.message_from_bytes(raw, policy=policy.default).get_content()


def test_bold_markers_do_not_reach_the_recipient():
    body = sent_body(to="ai@do.com", subject="Thư mời", body="- **Thời gian:** 19h\n- **Địa điểm:** Hà Nội")

    assert "**" not in body
    assert "- Thời gian: 19h" in body
    assert "- Địa điểm: Hà Nội" in body


def test_the_subject_is_stripped_too():
    raw = base64.urlsafe_b64decode(_rfc822({"to": "ai@do.com", "subject": "**Khẩn**", "body": "chào"}).encode("ascii"))

    assert email.message_from_bytes(raw, policy=policy.default)["Subject"] == "Khẩn"


@pytest.mark.parametrize(
    ("written", "read"),
    [
        ("*nghiêng*", "nghiêng"),
        ("`ma_lenh`", "ma_lenh"),
        ("## Chi tiết", "Chi tiết"),
        ("###### sâu", "sâu"),
    ],
)
def test_the_other_markers_come_off(written: str, read: str):
    assert _plain_text(written) == read


@pytest.mark.parametrize(
    "untouched",
    [
        # Gạch dưới là nội dung bình thường ở nền tảng này, không phải cú pháp.
        "node_id và run_approvals.caller",
        "ten_file_dinh_kem.pdf",
        # Dấu sao không cặp: phép nhân, và một cặp chưa đóng.
        "2*3 = 6",
        "**chưa đóng",
        # Gạch đầu dòng và xuống dòng đọc đúng ở dạng text rồi.
        "- một\n- hai",
        # Dấu thăng không có khoảng trắng sau nó thì không phải tiêu đề.
        "#hashtag và C# vẫn là chữ",
    ],
)
def test_what_is_not_emphasis_is_left_alone(untouched: str):
    assert _plain_text(untouched) == untouched


def test_a_body_with_no_markdown_is_returned_unchanged():
    plain = "Kính gửi anh Nam,\n\nMời anh dự tiệc lúc 19h.\n\nTrân trọng,\nĐức"

    assert _plain_text(plain) == plain


def test_a_missing_recipient_is_still_refused_before_anything_is_built():
    with pytest.raises(GoogleToolError, match="Thiếu người nhận"):
        _rfc822({"subject": "x", "body": "y"})
