"""Telling an `AgentLlm` node what day it is.

A model has no clock. Asked for "giá vàng hôm nay" it has to infer "hôm nay"
from whatever the search results show, and search results skew a day old —
yesterday's article has had a day to accumulate the links that rank it. The
observed failure was exactly that: an agent asked for today's gold price
answering with yesterday's, confidently and with sources.

So the runtime states the date instead of leaving it to be inferred. These tests
pin the three things that has to get right: the date is present, it is stated in
the platform's own timezone rather than UTC, and it sits above the creator's
prompt rather than after it.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.workflow.runtime import _dated, _grounded_prompt, _now_in_app_timezone, _utc_offset

#: A moment chosen so UTC and UTC+7 disagree about the date. 01:30 on the 16th
#: in Hanoi is still 18:30 on the 15th in UTC — the window in which a
#: UTC-stamped prompt is wrong by a whole day, which is the bug.
_EARLY_MORNING_HANOI = datetime(2026, 8, 16, 1, 30, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))


def test_the_prompt_states_todays_date() -> None:
    dated = _dated("Trả lời câu hỏi.", _EARLY_MORNING_HANOI)

    assert "16/08/2026" in dated


def test_the_date_is_the_local_one_not_utcs() -> None:
    """The whole point: at 01:30 in Hanoi, UTC still says the 15th."""
    dated = _dated("Trả lời câu hỏi.", _EARLY_MORNING_HANOI)

    assert "16/08/2026" in dated
    assert "15/08/2026" not in dated


def test_the_prompt_names_the_offset_a_model_can_do_arithmetic_with() -> None:
    """`UTC+07:00`, never the host-dependent spelling `tzname()` returns."""
    assert _utc_offset(_EARLY_MORNING_HANOI) == "UTC+07:00"
    assert _utc_offset(_EARLY_MORNING_HANOI) in _dated("x", _EARLY_MORNING_HANOI)


def test_the_weekday_is_named_so_relative_questions_are_answerable() -> None:
    """16/08/2026 is a Sunday — "cuối tuần này" needs that, not just the date."""
    assert "Chủ Nhật" in _dated("Trả lời câu hỏi.", _EARLY_MORNING_HANOI)


def test_the_creators_prompt_comes_last() -> None:
    """Context the creator writes against, not a correction appended after them."""
    dated = _dated("HƯỚNG DẪN CỦA NGƯỜI TẠO", _EARLY_MORNING_HANOI)

    assert dated.index("16/08/2026") < dated.index("HƯỚNG DẪN CỦA NGƯỜI TẠO")
    assert dated.endswith("HƯỚNG DẪN CỦA NGƯỜI TẠO")


# --------------------------------------------------------------------------- #
# Every node gets it, searched or not
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("searched", "findings"),
    [
        pytest.param(False, [], id="node that never searched"),
        pytest.param(True, [], id="searched and found nothing"),
        pytest.param(
            True,
            [{"node_id": "search", "source": "sjc.md", "text": "giá vàng", "doc_name": "sjc.md", "section_path": None, "page_number": None}],
            id="searched and found something",
        ),
    ],
)
def test_the_date_is_added_whatever_the_evidence(searched: bool, findings: list) -> None:
    """A node that searched is the *most* exposed: the passages it is holding
    carry dates of their own for the model to mistake for today's."""
    prompt = _grounded_prompt("Trả lời câu hỏi.", findings, searched=searched)

    assert "## Thời điểm hiện tại" in prompt


def test_evidence_still_lands_under_the_creators_prompt() -> None:
    """The date going on top must not have moved the sources on top too."""
    finding = {"node_id": "search", "source": "sjc.md", "text": "giá vàng", "doc_name": "sjc.md", "section_path": None, "page_number": None}
    prompt = _grounded_prompt("HƯỚNG DẪN", [finding], searched=True)

    assert prompt.index("HƯỚNG DẪN") < prompt.index("## Nguồn tham khảo")


def test_the_clock_is_read_in_the_configured_timezone() -> None:
    """Not UTC, and not the host's zone either — the one `APP_TIMEZONE` names."""
    now = _now_in_app_timezone()

    assert now.tzinfo is not None
    assert now.utcoffset() == ZoneInfo("Asia/Ho_Chi_Minh").utcoffset(now)
