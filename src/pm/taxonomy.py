"""The words a run's failure is filed under — ADR-0053 §2.

Two vocabularies, both the platform's own rather than copied from a slide
(the deck's rule: *"đừng copy taxonomy từ bài khác — dùng trace analysis để
xem use case của mình thật sự fail ở tầng nào"*).

**`outcome`** is decided by code at write time from the runtime's own error
sentence and the transcript. The split the playbook calls the most important
number on the board — `spec_error` (the creator can fix it: a graph that
names no tool, a node the runtime does not run) against `system_error` (call
the platform team: a provider down, a token expired, a socket that timed out)
— is a list of patterns, deterministic, so two runs that died the same way
are filed the same way and a PM filtering on one word gets every one of them.

**Trace codes** are the expert's words, chosen from a list when they read a
run and say why it failed. The starting list is distilled from the eighteen
field defects ADR-0052 fixed, which is the only trace analysis this product
has had so far; it is a list of strings and grows by editing the list.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.db.models import RunOutcome

#: Expert trace codes — `code: what it means`, in the reviewer's language.
#: Ordered as a reviewer scans them: intent → lookup → truth → completeness →
#: action discipline → hygiene.
TRACE_CODES: dict[str, str] = {
    "wrong_intent": "Hiểu sai việc người dùng muốn",
    "missing_lookup": "Trả lời mà không tra cứu / không gọi tool lẽ ra phải gọi",
    "fabricated_content": "Bịa nội dung, link, mã — không có trong kết quả tool",
    "dropped_results": "Tool trả về dữ liệu mà câu trả lời bỏ sót hoặc nói 'không tìm thấy'",
    "premature_action": "Hành động khi chưa đủ thông tin hoặc chưa qua duyệt",
    "over_asking": "Hỏi thứ đã có hoặc tham số tuỳ chọn",
    "tool_failure_hidden": "Tool hỏng nhưng câu trả lời trôi chảy như đã xong",
    "wrong_params": "Gọi đúng tool nhưng sai tham số",
    "no_next_step": "Đúng việc nhưng thiếu bước tiếp theo cho người dùng",
    "format": "Sai định dạng / lộ JSON / ngôn ngữ không phù hợp",
    "other": "Khác — ghi rõ trong ghi chú",
}

_SYSTEM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bHTTP\s*5\d\d\b",
        r"\b50[0-4]\b",
        r"\btimeout\b|\btimed out\b|hết thời gian",
        r"connection|kết nối.*(hết hạn|thất bại|từ chối|bị ngắt)|không kết nối được",
        r"rate ?limit|\b429\b|quá nhiều yêu cầu",
        r"token.*(hết hạn|expired)|expired.*token|\b401\b|unauthori[sz]ed|invalid_grant",
        r"\bECONNRESET\b|\bECONNREFUSED\b|\bENOTFOUND\b",
        r"checkpoint|database|postgres|psycopg|OperationalError",
        r"model.*(unavailable|overloaded)|provider|APIError|ServiceUnavailable",
    )
)

_SPEC_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"UnsupportedNodeError|không hỗ trợ loại node|node type",
        r"tool_id trống|chưa được nối|chưa gắn|không có tool|ToolUnavailableError",
        r"không có action|action .* không tồn tại|unknown action",
        r"thiếu tham số|tham số bắt buộc|missing required|validation error|ValidationError",
        r"source_ids|kho tri thức.*(rỗng|không)",
        r"prompt trống|config.*(thiếu|sai)",
        r"schema",
    )
)

_GUARDRAIL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"bị chặn|blocked|chặn",
        r"từ chối|rejected|denied|không được phép|forbidden|\b403\b",
        r"ngoài miền|ngoài phạm vi|out of scope|policy",
    )
)


def classify_error(error: str | None, *, transcript: Sequence[Mapping[str, Any]] = ()) -> str:
    """`outcome` for a run that died with `error` — one of `RunOutcome`'s words.

    Order matters and is deliberate: a guardrail refusal often quotes the
    thing it refused (an HTTP word, a tool name), and "we stopped it on
    purpose" is the truer fact; a spec fault is checked before a system fault
    because "tool_id trống … HTTP 502" is a graph that never had a tool, not a
    provider outage. Anything unrecognised stays the coarse `error`, which the
    board shows as "chưa phân loại" rather than guessing.
    """
    if not error:
        return RunOutcome.SUCCESS.value
    text = str(error)
    if any(p.search(text) for p in _GUARDRAIL_PATTERNS):
        return RunOutcome.GUARDRAIL_BLOCK.value
    if any(p.search(text) for p in _SPEC_PATTERNS):
        return RunOutcome.SPEC_ERROR.value
    if any(p.search(text) for p in _SYSTEM_PATTERNS):
        return RunOutcome.SYSTEM_ERROR.value
    # A run whose last tool call failed before it died, with an error the
    # patterns do not know: the tool's own failure is the better word.
    for line in reversed(list(transcript)):
        if line.get("kind") == "tool_result" and line.get("ok") is False:
            return classify_error(str(line.get("content") or ""), transcript=())
    return RunOutcome.ERROR.value


@dataclass(frozen=True, slots=True)
class TranscriptSummary:
    """What a transcript says when read as attributes — the derived columns of `agent_runs`."""

    node_count: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    actions: list[str] = field(default_factory=list)
    first_failure: str | None = None
    failure_node_type: str | None = None
    answer: str | None = None


#: Node types whose `node` line is something a person reads — the answer is
#: the last of these. Mirrors `frontend/src/lib/turns.js` `SPEAKING`; V1's
#: `step` lines are all spoken.
_SPEAKING = frozenset({"agent_llm", "collect"})


def summarize_transcript(transcript: Iterable[Mapping[str, Any]], *, error: str | None = None) -> TranscriptSummary:
    """Read the flat transcript once and answer the group-by questions.

    `answer` is the content of the last *spoken* line — an `agent_llm` or
    `collect` node in V2, any `step` in V1 — which is exactly what the chat
    window showed as the reply (`lib/turns.js`). Router and tool nodes speak
    JSON and are skipped, so a run that ended on an `if_else` still reports
    the sentence the person read.

    `first_failure` is where a reader should open the trace: the first tool
    that came back `ok=False`, or — for a run that died with no failed tool —
    the last node that ran, which is where it was when it stopped.
    """
    lines = list(transcript)
    # Two passes, because a V2 node's `tool` / `tool_result` lines are written
    # *before* its own `node` line (the node line is the node finishing), so
    # the type of the node a failed tool ran in is only known once the whole
    # transcript has been read. V1 tool lines carry a `step_id` and every step
    # is a spoken `agent_llm`-kind step.
    node_types: dict[str, str] = {}
    for line in lines:
        if line.get("kind") in ("node", "step"):
            node_types[str(line.get("node_id") or line.get("step_id") or "")] = str(
                line.get("node_type") or "agent_llm"
            )

    node_count = 0
    tool_calls = 0
    tool_failures = 0
    actions: list[str] = []
    first_failure: str | None = None
    failure_node_type: str | None = None
    answer: str | None = None
    last_node: tuple[str, str | None] | None = None

    def type_of(node_id: str, line: Mapping[str, Any]) -> str | None:
        return node_types.get(node_id) or ("agent_llm" if line.get("step_id") else None)

    for line in lines:
        kind = line.get("kind")
        node_id = str(line.get("node_id") or line.get("step_id") or "")
        if kind in ("node", "step"):
            node_count += 1
            node_type = node_types[node_id]
            last_node = (node_id, node_type)
            if node_type in _SPEAKING and str(line.get("content") or "").strip():
                answer = str(line["content"]).strip()
        elif kind == "tool":
            tool_calls += 1
            tool = str(line.get("tool") or "")
            if tool and tool not in actions:
                actions.append(tool)
        elif kind == "tool_result":
            if line.get("ok") is False:
                tool_failures += 1
                if first_failure is None:
                    first_failure = f"{node_id}:{line.get('tool') or ''}"
                    failure_node_type = type_of(node_id, line)
        elif kind == "budget_exceeded" and first_failure is None:
            first_failure = node_id or None
            failure_node_type = type_of(node_id, line)

    if error and first_failure is None and last_node is not None:
        first_failure, failure_node_type = last_node
    return TranscriptSummary(
        node_count=node_count,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
        actions=actions,
        first_failure=first_failure,
        failure_node_type=failure_node_type,
        answer=answer,
    )
