"""The one place a model touches the interview: reading free text into slots.

Its whole job is *"mô tả viết kỹ thì gần như không bị hỏi câu nào"*. A creator who
already said who the agent is for should never be asked who it is for, and the
only way to know they said it is to read what they wrote.

What this module deliberately does **not** do is decide anything. It is handed the
slots that are currently empty, and it returns values for however many of them the
text actually covers. Whether the interview continues, what gets asked next and
when it stops are all decided by `src.interview.slots` from the shape of the sheet
afterwards. A model that returned nothing costs the creator a question; it cannot
cost them a stop condition.

It runs on `document_model()` and nothing else. The models a creator can
configure are the ones their *agent's steps* run on (ADR-0010); the meta-agent is
the platform's, fixed by the team, and reading a description is the hardest
judgement call in the whole flow. A creator who could point this at a cheap model
would be choosing how well their own description gets understood.
"""

from collections.abc import Sequence

from src.db.models import PlatformCallPurpose
from src.evals.model_call import ask
from src.evals.model_json import UnreadableModelReplyError, json_object
from src.interview.slots import Slot, SlotSpec, SlotValue, normalise
from src.services.llm import ChatModelFactory, document_model

_EXTRACTION_PROMPT = """\
Bạn đang đọc mô tả của một người muốn dựng agent AI, để điền vào một biểu mẫu có sẵn.

{context}

Đoạn cần đọc:
\"\"\"
{text}
\"\"\"

Hãy điền những mục dưới đây **chỉ khi** đoạn trên thực sự nói tới:

{fields}

Quy tắc:
- Mục nào đoạn trên không nói tới thì **bỏ hẳn khỏi JSON**, đừng đoán và đừng để trống.
- Chỉ dùng thông tin có trong đoạn trên. Không suy diễn thêm điều người viết chưa nói.
- Giữ nguyên ý và cách diễn đạt của người viết, đừng viết hoa mỹ hơn.

Chỉ trả lời bằng một JSON object, không kèm giải thích:
{example}
"""

_CONTEXT_PREFIX = """\
Người này đã mô tả nghiệp vụ của họ như sau:
\"\"\"
{description}
\"\"\"
"""


async def read_slots(
    text: str,
    *,
    wanted: Sequence[SlotSpec],
    model_factory: ChatModelFactory,
    description: str | None = None,
) -> dict[Slot, SlotValue]:
    """Whatever `text` says about the slots in `wanted`.

    Returns only slots the text genuinely covered, so an empty result is an
    ordinary outcome rather than a failure — it means the next turn asks a
    question instead of skipping one.

    Never raises on a bad reply. A model that answered in prose, fenced its JSON
    or invented a field is a model that filled nothing, and the interview carries
    on by asking. The alternative — surfacing a parse error to the creator — would
    turn the platform's own flakiness into their dead end, when the fallback costs
    them one question they could answer anyway.

    `description` is passed as context on later turns so a one-word answer can
    still be read against what the creator originally wrote. It is context only:
    values are read out of `text`, which is why answering "kế toán viên" fills
    `audience` and does not re-fill slots the description already settled.
    """
    if not wanted:
        return {}
    model = model_factory(document_model())
    reply = await ask(
        model,
        _EXTRACTION_PROMPT.format(
            context=_CONTEXT_PREFIX.format(description=description) if description else "",
            text=text,
            fields=_fields_block(wanted),
            example=_example_block(wanted),
        ),
        purpose=PlatformCallPurpose.INTERVIEW,
    )
    try:
        document = json_object(reply)
    except UnreadableModelReplyError:
        return {}
    return _slots_from(document, wanted)


def _slots_from(document: dict[str, object], wanted: Sequence[SlotSpec]) -> dict[Slot, SlotValue]:
    """The usable values in `document`, for the slots we asked about and no others.

    Keyed by `wanted` rather than by what the document happens to contain, which
    is the same discipline `src.evals.cases` applies to a generated case's
    criterion: the labels are ours. A model that answered about a slot nobody
    asked about is answering about a slot the creator has already filled, and
    letting that through would be an overwrite dressed as an extraction.
    """
    found: dict[Slot, SlotValue] = {}
    for spec in wanted:
        if spec.slot.value not in document:
            continue
        value = normalise(spec.slot, document[spec.slot.value])
        if value is not None:
            found[spec.slot] = value
    return found


def _fields_block(wanted: Sequence[SlotSpec]) -> str:
    """The slots to fill, written as the questions the creator would have been asked.

    The same sentence either way, which is the point: a slot filled by reading the
    description and a slot filled by answering out loud have to mean the same
    thing, or the review screen shows two kinds of answer to one question.
    """
    return "\n".join(f'- "{spec.slot.value}" — {spec.question} ({spec.hint}){_shape_note(spec)}' for spec in wanted)


def _shape_note(spec: SlotSpec) -> str:
    """How this slot wants its answer shaped, said in the prompt rather than fixed after."""
    return " — trả về mảng chuỗi" if spec.is_list else " — trả về một chuỗi"


def _example_block(wanted: Sequence[SlotSpec]) -> str:
    """A skeleton of the exact document being asked for, with only the wanted keys."""
    fields = ", ".join(f'"{spec.slot.value}": {"[...]" if spec.is_list else "..."}' for spec in wanted)
    return "{" + fields + "}"
