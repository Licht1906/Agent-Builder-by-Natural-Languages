"""The AI Solution Designer's one model call: read everything said, ask one thing.

Where ADR-0020 §3's departure from V1 actually lives. V1 asked a model only to
*extract* and kept every judgement — what to ask, when to stop — in arithmetic
over empty slots (ADR-0014). Here the model is asked for three things at once:
what the conversation says about the seven fields, what in it **contradicts
itself**, and which single question is worth putting next. Detecting a
contradiction is not something emptiness can express, which is why V1's shape
could not be reused.

What it does *not* get is the last word. Everything it returns is a proposal that
`src.solution.sessions` weighs against what is already known — a question about a
field the creator has already answered is dropped there, and a session that the
model thinks is finished but that still has empty fields is not finished. This
module's own discipline is narrower and mechanical:

- it never raises. A model that answered in prose, fenced its JSON or invented a
  field has filled nothing, and the interview goes on by asking. Surfacing a
  parse error would turn the platform's flakiness into the creator's dead end.
- it never keeps a `ref`. `SpecDraft.read` drops it, because a blob key or a
  connection id is something the creator attaches, not something a model reading
  a description can know.

It runs on `document_model()` and nothing else, for the reason V1's extraction
gives (ADR-0010 §1): the models a creator's admin configures are the ones their
*agent's steps* run on, and reading somebody's description of their business is
the platform's own hardest judgement call.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.db.models import PlatformCallPurpose
from src.evals.model_call import ask
from src.evals.model_json import UnreadableModelReplyError, json_object
from src.services.llm import ChatModelFactory, document_model
from src.solution.draft import FIELD_SCHEDULE, SpecDraft, SpecField, field_named, lines


@dataclass(frozen=True, slots=True)
class Turn:
    """One exchange of the interview: what was asked, and what the creator answered.

    Kept verbatim and re-sent every turn. The designer has no memory of its own —
    `ask` is single-shot — so the conversation *is* this list, and a question the
    creator answered three turns ago is only "already answered" because these
    words are still in front of the model.
    """

    question: str
    answer: str
    #: Which field the question was about (`None` for a contradiction question).
    #: Stored so `sessions` can count how often one field has been asked — the
    #: cap that ended the ten-turn repeat measured on 2026-08-30.
    field: str | None = None


@dataclass(frozen=True, slots=True)
class Question:
    """The one thing to put to the creator now, and which field it is about.

    The field is what makes the question checkable. A bare sentence could be
    asking about anything, including something the creator already said; naming
    the field lets `sessions` refuse a question about a field that is already
    answered, which is the criterion *"không phải trả lời lại điều đã nói"*.

    `None` is reserved for the one question that genuinely is not about a single
    field: *"these two things you told me contradict each other"*. The model is
    never allowed to leave it out — a question nobody can attribute is dropped in
    `_question` — but `sessions` writes one when a contradiction is all that is
    left standing between the creator and a finished interview.
    """

    field: SpecField | None
    text: str


@dataclass(frozen=True, slots=True)
class DesignerReading:
    """One pass of the designer over the whole conversation.

    Three parts because they answer three different questions and one of them is
    not derivable from the others: what is now known, what does not hold together,
    and what to ask. An empty reading — nothing read, nothing flagged, nothing
    asked — is an ordinary outcome and means the turn falls back to what
    `sessions` already knew.
    """

    draft: SpecDraft
    conflicts: tuple[str, ...]
    question: Question | None


_DESIGNER_PROMPT = """\
Bạn là AI Solution Designer của một nền tảng dựng agent AI. Bạn đang phỏng vấn một người \
để hiểu bài toán nghiệp vụ của họ, rồi điền vào một đặc tả bảy trường.

Người này mô tả bài toán của họ như sau:
\"\"\"
{description}
\"\"\"
{conversation}
{known}
Bảy trường của đặc tả:

{fields}

Việc của bạn, trả về trong một JSON object:
1. Điền những trường mà mọi thứ nói ở trên đã thực sự trả lời. Trường nào chưa được nói tới \
thì **bỏ hẳn khỏi JSON**, đừng đoán hộ.
2. "conflicts": CHỈ những chỗ hai điều người này nói mâu thuẫn nhau (không thể cùng đúng), mỗi chỗ một \
câu ngắn nói rõ hai điều nào. Điều chưa nói tới hay còn chung chung KHÔNG phải mâu thuẫn — để trường trống \
hoặc hỏi ở mục 3. Không có thì để mảng rỗng.
3. "question": **đúng một** câu hỏi cho điều còn thiếu hoặc mâu thuẫn quan trọng nhất, kèm tên \
trường nó hỏi về. Đã đủ rõ để dựng agent thì để null.

Quy tắc:
- Không hỏi lại thứ người ta đã nói. Trường nào ở phần "Đã biết" bên trên đã có thì coi như xong, \
trừ khi chính nó nằm trong một mâu thuẫn bạn vừa nêu.
- Chỉ hỏi về NGHIỆP VỤ: agent làm gì, cho ai, đọc từ đâu, ra kết quả gì, thế nào là đúng, cấm gì. Không \
hỏi cách làm: không hỏi "bằng cách nào", định dạng hay cấu trúc file, tài khoản đã kết nối chưa, kỹ thuật \
nhận quyết định duyệt — nền tảng lo những việc đó.
- Một câu trả lời chưa đầy đủ vẫn là câu trả lời: điền nó vào trường rồi hỏi sang trường khác còn trống; \
không hỏi lại cùng một trường vì muốn chi tiết hơn.
- "data": liệt kê ĐỦ mọi nguồn được nhắc tới, mỗi nguồn đúng loại — file đã tải lên = uploaded_file; tài \
khoản Gmail/Drive/… đã kết nối = connected_account; tìm trên web = web; thứ người dùng gõ lúc chạy = user_input; \
dữ liệu nghiệp vụ có cấu trúc nằm trong hệ thống của công ty (công nợ, hoá đơn, đơn hàng, tồn kho, khách hàng, \
nhân viên… trong ERP, CRM hay cơ sở dữ liệu) = database — đó là bảng số liệu agent truy vấn, không phải tài liệu \
để đọc, kể cả khi người này không nói chữ "cơ sở dữ liệu". \
Nguồn được nhắc tới mà người này KHÔNG nói rõ nó ở đâu (ví dụ "chính sách của công ty mình" — file đã tải \
lên hay ở đâu khác?) thì KHÔNG đoán loại và KHÔNG đưa vào "data"; đặt "question" về trường "data" hỏi đúng \
nguồn đó ở đâu, kể cả khi "data" đã có nguồn khác.
- "success_criteria" và "prohibitions" CHỈ điền khi người này nói rõ thế nào là làm đúng ("làm đúng nghĩa \
là", "phải", "kiểm") hay cấm gì ("không được", "tuyệt đối không", hoặc "không cấm gì"). Mô tả chỉ kể việc \
cần làm thì để hai trường này trống để hỏi — đừng tự suy ra tiêu chí hay điều cấm từ mục tiêu.
- Bài toán nói tới BẬC, MỨC, NGƯỠNG hay "giọng nào theo số ngày / số tiền / tỷ lệ" mà chưa nêu con số cụ thể \
(ví dụ "quá hạn từng ấy ngày thì viết giọng nào") thì đó là quy tắc nghiệp vụ agent phải làm đúng: đặt "question" \
về trường "success_criteria" hỏi rõ các bậc và con số của từng bậc — kể cả khi trường đó đã có tiêu chí khác. \
Không tự đoán con số. Người này đã trả lời có con số (dù chưa đủ mọi bậc — phần còn lại nằm trong tài liệu) \
thì ghi câu trả lời đó thành tiêu chí và KHÔNG hỏi lại.
- "text" của mỗi tiêu chí là NGUYÊN CÂU người tạo nói, giữ mọi con số và ngưỡng ("dưới 60 ngày là thư nhắc, \
từ 60 ngày là thư cảnh báo"); không rút gọn thành tên loại kiểm — "chính xác", "gọi đúng công cụ" không phải \
tiêu chí, đó chỉ là category.
- "success_criteria": đọc lời người tạo thành category — "bám nguồn tài liệu" = source_groundedness, "chính \
xác" = correctness, "đúng trọng tâm" = relevance, "đúng quy trình / đi tới cuối" = workflow_success, "gọi đúng \
công cụ / thật sự gửi hay tạo" = tool_correctness, "tuân thủ người duyệt" = human_approval_compliance; câu \
không rõ loại thì dùng correctness.
- Chỉ dùng thông tin có trong mô tả và các câu trả lời ở trên, giữ nguyên cách diễn đạt của người viết.
- Đặc tả nói agent cần **gì**, không nói làm **bằng cách nào** — đừng nhắc tới bước, node, tool hay model.
- Không tự bịa "ref" cho nguồn dữ liệu; người tạo sẽ tự gắn file hoặc tài khoản thật sau.

Chỉ trả lời bằng một JSON object, không kèm giải thích:
{skeleton}
"""

_CONVERSATION_PREFIX = """
Các câu bạn đã hỏi và câu trả lời của họ:
{turns}
"""

_KNOWN_PREFIX = """
Đã biết (không hỏi lại những trường này):
{known}
"""

_SKELETON = """\
{
  "goal": "...",
  "users": ["..."],
  "data": [{"name": "...", "kind": "uploaded_file"}],
  "actions": ["..."],
  "expected_outcome": ["..."],
  "success_criteria": [{"text": "...", "category": "correctness"}],
  "prohibitions": ["..."],
  "conflicts": ["..."],
  "question": {"field": "...", "text": "..."}
}"""


async def read_problem(
    *,
    description: str,
    turns: Sequence[Turn],
    known: SpecDraft,
    model_factory: ChatModelFactory,
) -> DesignerReading:
    """One designer pass over `description` plus everything said since.

    The whole conversation goes in on every turn, and so does everything already
    known. That is what makes *"chỉ hỏi về những gì còn thiếu hoặc mâu thuẫn"*
    possible at all: a model shown only the latest answer cannot tell a gap from
    something the creator settled two questions ago.
    """
    model = model_factory(document_model())
    reply = await ask(
        model,
        _DESIGNER_PROMPT.format(
            description=description,
            conversation=_conversation_block(turns),
            known=_known_block(known),
            fields=_fields_block(),
            skeleton=_SKELETON,
        ),
        purpose=PlatformCallPurpose.INTERVIEW,
    )
    try:
        document = json_object(reply)
    except UnreadableModelReplyError:
        return DesignerReading(draft=SpecDraft.empty(), conflicts=(), question=None)
    return DesignerReading(
        draft=SpecDraft.read(document),
        conflicts=lines(document.get("conflicts")),
        question=_question(document.get("question")),
    )


def _question(raw: object) -> Question | None:
    """The model's next question, or `None` if it did not ask a usable one.

    A question naming a field that is not one of the seven is dropped rather than
    attached to a guess. The field is the only thing that makes the question
    checkable against what is already known, and a question nobody can attribute
    is no safer than no question — `sessions` writes a better one from the
    schedule.
    """
    if not isinstance(raw, Mapping):
        return None
    text = raw.get("text")
    field = field_named(raw.get("field"))
    if field is None or not isinstance(text, str) or not text.strip():
        return None
    return Question(field=field, text=text.strip())


def _fields_block() -> str:
    """The seven fields, written as the questions the creator would be asked.

    The same sentence either way, and that is the point: a field filled by
    reading the description and a field filled by answering out loud have to mean
    the same thing, or the review screen shows two kinds of answer to one
    question.
    """
    return "\n".join(f'- "{spec.field.value}" — {spec.question} ({spec.hint})' for spec in FIELD_SCHEDULE)


def _conversation_block(turns: Sequence[Turn]) -> str:
    """Every question and answer so far, or nothing at all on the opening turn."""
    if not turns:
        return ""
    body = "\n".join(f"- Hỏi: {turn.question}\n  Đáp: {turn.answer}" for turn in turns)
    return _CONVERSATION_PREFIX.format(turns=body)


def _known_block(known: SpecDraft) -> str:
    """What the interview already holds, so the model can see what not to ask about."""
    stored: Mapping[str, Any] = known.to_stored()
    if not stored:
        return ""
    body = "\n".join(f"- {field}: {_readable(value)}" for field, value in stored.items())
    return _KNOWN_PREFIX.format(known=body)


def _readable(value: Any) -> str:
    """One stored field as a line of prose — a list becomes its items, an entry its text."""
    if isinstance(value, str):
        return value
    return "; ".join(_readable_item(item) for item in value)


def _readable_item(item: Any) -> str:
    """One list entry: a plain sentence, or the sentence inside a structured entry."""
    if isinstance(item, Mapping):
        return " / ".join(f"{key}={item[key]}" for key in item if item[key] is not None)
    return str(item)
