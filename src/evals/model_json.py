"""Reading a JSON answer out of a model's prose.

Both LLM calls in eval ask for JSON and neither is guaranteed to get it: models
wrap their answers in ``` fences, prefix them with "Đây là kết quả:", or simply
answer in prose. This is the one place that copes with that, so the generator
and the judge cannot disagree about what counts as a readable answer.

It is deliberately forgiving about packaging and completely unforgiving about
content. A fence or a preamble is a formatting habit and costs nothing to look
past; a document that is not the shape we asked for is a model that did not do
the job, and guessing at what it meant is how a test case with no input, or a
verdict nobody decided, ends up stored as fact.
"""

import json
from typing import Any

#: The brackets a JSON document can start and end with, as `(opening, closing)`.
_BRACKETS = {"object": ("{", "}"), "array": ("[", "]")}


class UnreadableModelReplyError(ValueError):
    """A model answered with something that is not the JSON document we asked for."""


def json_object(reply: str) -> dict[str, Any]:
    """The JSON object inside `reply`. Raises `UnreadableModelReplyError` if there is none."""
    value = _first_json_value(reply, "object")
    if not isinstance(value, dict):
        raise UnreadableModelReplyError("model trả về JSON nhưng không phải một object")
    return value


def json_array(reply: str) -> list[Any]:
    """The JSON array inside `reply`. Raises `UnreadableModelReplyError` if there is none."""
    value = _first_json_value(reply, "array")
    if not isinstance(value, list):
        raise UnreadableModelReplyError("model trả về JSON nhưng không phải một mảng")
    return value


def _first_json_value(reply: str, kind: str) -> Any:
    """Parse the outermost `kind`-shaped document in `reply`.

    Sliced between the first opening bracket and the last closing one rather
    than parsed from the start: everything before the first bracket is preamble,
    and everything after the last one is commentary. Both are things a model
    adds without being asked, and neither changes what it decided.
    """
    opening, closing = _BRACKETS[kind]
    start = reply.find(opening)
    end = reply.rfind(closing)
    if start == -1 or end < start:
        raise UnreadableModelReplyError(f"không tìm thấy JSON {kind} nào trong câu trả lời của model")
    try:
        return json.loads(reply[start : end + 1])
    except json.JSONDecodeError as exc:
        raise UnreadableModelReplyError(f"JSON {kind} trong câu trả lời của model hỏng: {exc}") from None
