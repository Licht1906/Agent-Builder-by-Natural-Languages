"""A later reading adds and corrects; it never forgets — and a category in the model's own words still counts.

Both measured on 2026-08-30 (`benchmark/pitch_kinh_doanh`, agent 2, runs 20–24;
ADR-0067 §4): a reading that wrote `data` back with one source of three erased
the Gmail account the agent existed for, and a four-criterion answer was read
as zero criteria because every `category` was spelled in the creator's words.
"""

from src.models.solution_spec import DataSourceKind, SuccessCriterionCategory
from src.solution.draft import SpecDraft


def test_a_reading_that_omits_a_source_does_not_erase_it() -> None:
    known = SpecDraft.read(
        {
            "data": [
                {"name": "Chính sách nhắc nợ", "kind": "uploaded_file"},
                {"name": "Gmail của phòng", "kind": "connected_account"},
                {"name": "Thông tin khoản nợ", "kind": "user_input"},
            ]
        }
    )
    later = SpecDraft.read({"data": [{"name": "Chính sách nhắc nợ", "kind": "uploaded_file"}]})

    merged = known.merged_with(later)

    assert [source.name for source in merged.data] == ["Chính sách nhắc nợ", "Gmail của phòng", "Thông tin khoản nợ"]


def test_a_re_read_entry_with_the_same_name_is_the_correction_and_wins() -> None:
    known = SpecDraft.read({"data": [{"name": "Chính sách hạng khách", "kind": "user_input"}]})
    corrected = SpecDraft.read({"data": [{"name": "chính sách hạng khách", "kind": "uploaded_file"}]})

    merged = known.merged_with(corrected)

    assert [(source.name, source.kind) for source in merged.data] == [
        ("chính sách hạng khách", DataSourceKind.UPLOADED_FILE)
    ]


def test_criteria_accumulate_across_turns_and_the_goal_is_replaced() -> None:
    first = SpecDraft.read(
        {"goal": "Gửi thư nhắc nợ", "success_criteria": [{"text": "Không gửi khi chưa duyệt", "category": "human_approval_compliance"}]}
    )
    second = SpecDraft.read(
        {"goal": "Gửi thư nhắc nợ đúng bậc", "success_criteria": [{"text": "Bậc thư đúng theo số ngày quá hạn", "category": "source_groundedness"}]}
    )

    merged = first.merged_with(second)

    assert merged.goal == "Gửi thư nhắc nợ đúng bậc"
    assert [criterion.text for criterion in merged.success_criteria] == [
        "Không gửi khi chưa duyệt",
        "Bậc thư đúng theo số ngày quá hạn",
    ]


def test_a_category_in_the_creators_words_is_read_not_dropped() -> None:
    draft = SpecDraft.read(
        {
            "success_criteria": [
                {"text": "Thư không gửi khi chưa duyệt", "category": "tuân thủ người duyệt"},
                {"text": "Thư thật sự được gửi", "category": "tool"},
                {"text": "Bậc thư đúng theo chính sách", "category": "bám nguồn"},
                {"text": "Số liệu đúng như nhân viên đưa", "category": "cái gì đó lạ"},
            ]
        }
    )

    assert [criterion.category for criterion in draft.success_criteria] == [
        SuccessCriterionCategory.HUMAN_APPROVAL_COMPLIANCE,
        SuccessCriterionCategory.TOOL_CORRECTNESS,
        SuccessCriterionCategory.SOURCE_GROUNDEDNESS,
        SuccessCriterionCategory.CORRECTNESS,
    ]


def test_a_number_the_creator_said_survives_the_reading() -> None:
    """`sessions._numeric_sentences_lost` + `with_criteria`: the 60-day rule is kept verbatim when the model read it as 'chính xác'."""
    from src.solution.sessions import _numeric_sentences_lost

    draft = SpecDraft.read({"success_criteria": [{"text": "chính xác", "category": "correctness"}]})
    answer = (
        "Thư không bao giờ được gửi khi tôi chưa duyệt — kiểm tuân thủ người duyệt. "
        "Bậc thư đúng theo số ngày quá hạn: dưới 60 ngày là thư nhắc, từ 60 ngày là thư cảnh báo — kiểm bám nguồn."
    )
    lost = _numeric_sentences_lost(answer, draft)
    assert lost == ("Bậc thư đúng theo số ngày quá hạn: dưới 60 ngày là thư nhắc, từ 60 ngày là thư cảnh báo — kiểm bám nguồn.",)
    kept = draft.with_criteria(lost)
    assert [criterion.text for criterion in kept.success_criteria][-1] == lost[0]
    assert kept.success_criteria[-1].category == SuccessCriterionCategory.CORRECTNESS
    # Already carried by a criterion: nothing to add.
    assert _numeric_sentences_lost(answer, kept) == ()
    assert kept.with_criteria(()) == kept


def test_a_kind_in_the_creators_words_is_read_but_an_unknown_kind_is_still_dropped() -> None:
    draft = SpecDraft.read(
        {
            "data": [
                {"name": "Bộ chính sách", "kind": "file"},
                {"name": "Hộp thư", "kind": "Gmail"},
                {"name": "Thứ gì đó", "kind": "không rõ"},
            ]
        }
    )

    assert [(source.name, source.kind) for source in draft.data] == [
        ("Bộ chính sách", DataSourceKind.UPLOADED_FILE),
        ("Hộp thư", DataSourceKind.CONNECTED_ACCOUNT),
    ]
