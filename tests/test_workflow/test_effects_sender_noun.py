"""*"người gửi"* is the sender, not the act of sending.

A read-only Gmail search spec — *"Liệt kê các thư tìm được với thông tin người
gửi, tiêu đề, ngày"* — was classified as an unrecoverable effect because "gui"
sits inside "nguoi gui". The composer was then required to put an approval
before a read-only search, refused its honest graph three times, and the
creator got an unconfirmable template instead of a workflow.
"""

from src.workflow.effects import has_high_risk_action, is_high_risk


def test_listing_the_sender_column_is_not_sending() -> None:
    assert is_high_risk("Liệt kê các thư tìm được với thông tin người gửi, tiêu đề, ngày") is False
    assert has_high_risk_action(["Tìm thư theo từ khoá", "Liệt kê người gửi, tiêu đề"]) is False


def test_sending_is_still_sending() -> None:
    assert is_high_risk("Gửi email lời mời từ người chủ trì đến người được mời") is True
    assert is_high_risk("gửi thư cho khách") is True
    assert is_high_risk("GỬI THƯ") is True
    # The verb beside the noun phrase still counts: "người gửi" is dropped, "gửi lại" is not.
    assert is_high_risk("người gửi muốn gửi lại thư") is True
