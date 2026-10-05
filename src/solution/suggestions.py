"""Đề xuất câu trả lời cho câu hỏi phỏng vấn đang mở — gợi ý, không phải trích xuất.

Kiểu recommend của Claude Code: mỗi câu hỏi của AI Solution Designer kèm vài
phương án trả lời bấm-chọn-được, cái đầu có thể đánh dấu "khuyến nghị". Người tạo
vẫn gõ tay được — gợi ý là chỗ dựa, không phải rào.

Một lời gọi model **riêng**, ngoài `read_problem`. Vòng phỏng vấn cố tình chỉ tốn
một model call mỗi lượt để đọc hội thoại (ADR-0020 §3); đề xuất là việc khác, do
người dùng kích hoạt khi mở màn hình câu hỏi, nên nó có prompt riêng và không bao
giờ chen vào đường trả lời.

Nó không quyết định gì. Trả 0–5 câu; rỗng là kết quả bình thường — nghĩa là mô tả
chưa đủ tín hiệu cho trường này, và màn hình khi đó chỉ còn ô nhập trơn. Không bao
giờ raise: model trả prose, rào ```json hay bịa khoá đều quy về "không gợi ý được".

Chạy trên `document_model()` như mọi lời gọi khác của vòng phỏng vấn (ADR-0010 §1):
đọc mô tả nghiệp vụ của người ta là việc khó nhất cả luồng, không phải chỗ để một
model rẻ tiền chen vào.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.db.models import PlatformCallPurpose
from src.evals.model_call import ask
from src.evals.model_json import UnreadableModelReplyError, json_object
from src.services.llm import ChatModelFactory, document_model
from src.solution.designer import Question, Turn
from src.solution.draft import FIELD_SCHEDULE, FieldSpec, SpecDraft, SpecField

#: Trường dạng danh sách — người tạo chọn được nhiều gợi ý một lúc. `GOAL` là
#: trường vô hướng duy nhất; câu hỏi về mâu thuẫn (`field is None`) không có gợi ý.
_LIST_FIELDS = frozenset(SpecField) - {SpecField.GOAL}

#: Nhiều nhất năm gợi ý. Quá số này thì không còn là "chọn nhanh" — người tạo đọc
#: hết cũng lâu bằng tự nghĩ.
_MAX = 5

#: `FIELD_SCHEDULE` keyed by field, for the one lookup this module needs.
_SPEC_OF: Mapping[SpecField, FieldSpec] = {spec.field: spec for spec in FIELD_SCHEDULE}

#: Extra shaping for the two structured fields, so a picked suggestion carries the
#: `kind` / `category` those fields route on — a plain sentence for either gets
#: dropped by `SpecDraft.read` and asked again (`draft.py`, `_entries`).
_SHAPE_HINT: Mapping[SpecField, str] = {
    SpecField.DATA: (
        "- Mỗi phương án nói rõ nguồn đó là loại nào: file bạn tải lên, tài khoản bạn kết nối "
        "(Gmail/Drive/...), thông tin tìm trên web, hay dữ liệu người dùng nhập lúc chạy.\n"
    ),
    SpecField.SUCCESS_CRITERIA: (
        "- Mỗi phương án nói rõ nó kiểm điều gì: chạy đúng quy trình, gọi đúng công cụ, tuân thủ "
        "quyết định người duyệt, bám đúng nguồn tài liệu, trả lời chính xác, hay trả lời đúng trọng tâm.\n"
    ),
}

_SUGGESTIONS_PROMPT = """\
Bạn là AI Solution Designer đang phỏng vấn một người để hiểu bài toán nghiệp vụ của họ.
Bạn vừa hỏi họ một câu và cần gợi ý sẵn vài câu trả lời để họ bấm chọn cho nhanh — họ vẫn
tự gõ được, nên gợi ý chỉ cần hợp lý, không phải vét hết.

Người này mô tả bài toán như sau:
\"\"\"
{description}
\"\"\"
{conversation}{known}
Câu bạn vừa hỏi (về trường "{field}"): {question}
Một câu trả lời tốt cho câu này: {hint}

Hãy đề xuất tối đa {max} phương án trả lời, trả về trong một JSON object:
- Mỗi phương án là một câu ngắn, cụ thể, bám vào mô tả và hội thoại ở trên — đừng bịa thêm
  chi tiết người ta chưa nói tới.
- Xếp theo mức độ khớp giảm dần. Nếu bạn đủ tự tin phương án **đầu tiên** chính là câu trả lời
  đúng, đặt "recommended": true cho đúng nó; không chắc thì để tất cả false.
- Nếu mô tả đã nói rõ trường này rồi thì trả "suggestions" là mảng rỗng — đừng nhắc lại thứ
  người ta vừa nói.
{shape_hint}
Chỉ trả lời bằng một JSON object, không kèm giải thích:
{{"suggestions": [{{"text": "...", "recommended": false}}]}}
"""

_CONVERSATION_PREFIX = "\nCác câu bạn đã hỏi và câu trả lời của họ:\n{turns}\n"
_KNOWN_PREFIX = "\nĐã biết (đừng gợi ý lại những trường này):\n{known}\n"


@dataclass(frozen=True, slots=True)
class Suggestion:
    """Một phương án trả lời bấm-chọn-được. `recommended` chỉ đúng cho cái đầu."""

    text: str
    recommended: bool


@dataclass(frozen=True, slots=True)
class SuggestionSet:
    """Gợi ý cho một câu hỏi: nhắm trường nào, chọn được nhiều không, và danh sách.

    `multi` bám vào trường chứ không vào số gợi ý: một trường danh sách vẫn cho
    chọn nhiều kể cả khi model chỉ trả về một phương án.
    """

    field: SpecField | None
    multi: bool
    items: tuple[Suggestion, ...]

    @classmethod
    def none(cls, field: SpecField | None) -> "SuggestionSet":
        """A set with nothing in it — the graceful path when there is no signal."""
        return cls(field=field, multi=field in _LIST_FIELDS, items=())


async def suggest_answers(
    *,
    description: str,
    turns: Sequence[Turn],
    known: SpecDraft,
    question: Question,
    model_factory: ChatModelFactory,
) -> SuggestionSet:
    """Tối đa năm câu trả lời gợi ý cho `question`, cái đầu có thể là "khuyến nghị".

    Câu hỏi về mâu thuẫn (`question.field is None`) không có gợi ý và không gọi
    model: hai trường đá nhau thì thứ cần là lời người tạo nói lại cho rõ, không
    phải một menu.

    Không bao giờ raise. Một model trả lời bằng prose hay JSON hỏng là một model
    không gợi ý được gì, và màn hình lùi về ô nhập tay — cùng cách `designer` và
    `extraction` lùi về việc hỏi.
    """
    if question.field is None:
        return SuggestionSet.none(None)

    spec = _SPEC_OF[question.field]
    model = model_factory(document_model())
    reply = await ask(
        model,
        _SUGGESTIONS_PROMPT.format(
            description=description,
            conversation=_conversation_block(turns),
            known=_known_block(known),
            field=question.field.value,
            question=question.text,
            hint=spec.hint,
            max=_MAX,
            shape_hint=_SHAPE_HINT.get(question.field, ""),
        ),
        purpose=PlatformCallPurpose.INTERVIEW_SUGGESTIONS,
    )
    try:
        document = json_object(reply)
    except UnreadableModelReplyError:
        return SuggestionSet.none(question.field)
    return SuggestionSet(
        field=question.field,
        multi=question.field in _LIST_FIELDS,
        items=_items(document.get("suggestions")),
    )


def _items(raw: object) -> tuple[Suggestion, ...]:
    """The usable suggestions in `raw`, at most five, with `recommended` on the first only.

    The flag is clamped to index 0: the prompt asks the model to rank and put a
    recommendation first, but "only the first may be recommended" is the screen's
    rule, not something a prompt is trusted to keep. A recommendation the model
    hangs on a later entry is read as "not confident about the top one" and
    dropped.
    """
    entries = raw if isinstance(raw, list) else []
    texts: list[str] = []
    flags: list[bool] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        texts.append(text.strip())
        flags.append(bool(entry.get("recommended")))
        if len(texts) == _MAX:
            break
    if not texts:
        return ()
    top_is_recommended = flags[0]
    return tuple(
        Suggestion(text=text, recommended=top_is_recommended and index == 0)
        for index, text in enumerate(texts)
    )


def _conversation_block(turns: Sequence[Turn]) -> str:
    """Every question and answer so far, or nothing on the opening turn.

    Its own copy rather than `designer._conversation_block`: this prompt is a
    different one, and a shared formatter between two prompts is a place for one
    to change and quietly reshape the other.
    """
    if not turns:
        return ""
    body = "\n".join(f"- Hỏi: {turn.question}\n  Đáp: {turn.answer}" for turn in turns)
    return _CONVERSATION_PREFIX.format(turns=body)


def _known_block(known: SpecDraft) -> str:
    """What the interview already holds, so the model does not re-suggest a settled field."""
    stored: Mapping[str, object] = known.to_stored()
    if not stored:
        return ""
    body = "\n".join(f"- {field}: {_readable(value)}" for field, value in stored.items())
    return _KNOWN_PREFIX.format(known=body)


def _readable(value: object) -> str:
    """One stored field as a line of prose — a list becomes its items, an entry its keys."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " / ".join(f"{key}={val}" for key, val in value.items() if val is not None)
    if isinstance(value, list):
        return "; ".join(_readable(item) for item in value)
    return str(value)
