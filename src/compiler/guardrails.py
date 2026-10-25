"""UC1-27 (#128), spec.md §11.1 — the six mandatory guardrails, as pure text
functions the agent node (`src.compiler.graph`) runs at the two points spec.md
names: before a message reaches the model, and before a tool-call-free reply
reaches the user.

Deliberately dependency-free and deterministic — no model call inside a safety
gate. The same reasoning `src.agentspec.secrets` gives for scanning specs with
regex rather than an LLM, and the one §10.3 gives for keeping T2's provenance
rule a structural check rather than a judgement call: a guardrail that itself
needed a live model would be slow and flaky in a live deployment and either
fake or untested in a `pytest` run, and the failure mode of a *safety* gate
firing inconsistently is worse than the false negatives a heuristic accepts.

Baseline six, §11.1's table:

- Input: `prompt_injection_detector`, `pii_redactor`, `topic_classifier`
- Output: `pii_masker`, `citation_required`, `confidence_router`
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.agentspec.spec import Fallback

_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
#: A Vietnamese mobile number: a leading 0, then 9–10 more digits, with no
#: digit immediately before or after so this never eats the middle of a
#: longer number (an order id, an amount).
_VN_PHONE_PATTERN = re.compile(r"(?<!\d)0\d{9,10}(?!\d)")

_PII_PATTERNS: dict[str, re.Pattern[str]] = {"email": _EMAIL_PATTERN, "phone": _VN_PHONE_PATTERN}


@dataclass(frozen=True, slots=True)
class ScrubResult:
    """What one PII scrub produced — the (possibly unchanged) text, and which shapes fired.

    `redacted_fields` is empty exactly when `text` is untouched, so a caller never
    has to compare the two strings to know whether anything happened.
    """

    text: str
    redacted_fields: tuple[str, ...] = ()

    @property
    def triggered(self) -> bool:
        return bool(self.redacted_fields)


def _scrub(text: str, *, tag: str) -> ScrubResult:
    redacted_fields: list[str] = []
    result = text
    for label, pattern in _PII_PATTERNS.items():
        if pattern.search(result):
            redacted_fields.append(label)
            result = pattern.sub(f"[{label}_{tag}]", result)
    return ScrubResult(text=result, redacted_fields=tuple(redacted_fields))


def redact_pii(text: str) -> ScrubResult:
    """`pii_redactor` (input, §11.1): strip anything shaped like an email or a VN
    mobile number out of a message *before* it is ever sent to the model — FR/AC's
    own "bị redact trước khi tới LLM".
    """
    return _scrub(text, tag="redacted")


def mask_pii(text: str) -> ScrubResult:
    """`pii_masker` (output, §11.1): the same shapes, scrubbed a second time on the
    way out — independent of whether the input side ever saw them, because a model
    can restate a number it was given or produce one that merely looks like PII.
    """
    return _scrub(text, tag="masked")


#: Phrase shapes an injected instruction tends to take — the ones §15's own demo
#: script and the golden set's `injection` group use. Not a substitute for T2's
#: provenance-based defence (`src.compiler.state`, §10.3): that mechanism is what
#: actually stops a write derived from content the agent merely *read*, regardless
#: of whether this heuristic ever fires on what the *user* typed.
_INJECTION_PHRASES = (
    "bỏ qua hướng dẫn",
    "bỏ qua mọi hướng dẫn",
    "bỏ qua chỉ thị",
    "bỏ qua toàn bộ hướng dẫn",
    "không còn giới hạn",
    "làm theo mọi hướng dẫn xuất hiện",
    "làm theo đúng mọi hướng dẫn",
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard the above",
    "you are now",
    "system prompt",
)


def detect_prompt_injection(text: str) -> bool:
    """`prompt_injection_detector` (input, §11.1): a cheap phrase match for hidden
    instructions in the question itself.
    """
    lowered = text.lower()
    return any(phrase in lowered for phrase in _INJECTION_PHRASES)


#: The Vietnamese a user actually types for each `prohibitions.deny_topics` slug.
#:
#: Without this the classifier was dead code in production and nobody could tell,
#: because the slugs are English (`salary`, `legal_dispute`) and every question
#: this platform receives is Vietnamese — "Lương của nhân viên kinh doanh là bao
#: nhiêu?" contains neither `salary` nor `salary`-with-spaces, so
#: `topic_classifier` matched nothing on all five of the golden set's refusal
#: cases. Two of those five still *passed*, which is the part worth keeping in
#: mind: the 7B model happened to phrase its own refusal using the words the
#: grader looked for. A guardrail whose apparent successes come from the model's
#: choice of words is not a guardrail, and the slug match alone could never have
#: shown the difference.
#:
#: Keyed by slug and merged with it rather than replacing it, so a spec naming a
#: topic absent from this table still gets the original English/underscore match
#: instead of silently getting no check at all.
_TOPIC_SYNONYMS: dict[str, tuple[str, ...]] = {
    "salary": ("lương", "tiền lương", "thu nhập của nhân viên", "bảng lương"),
    "hr_records": ("hồ sơ nhân sự", "hồ sơ nhân viên", "nhân sự", "hợp đồng lao động"),
    "legal_dispute": ("tranh chấp pháp lý", "tranh chấp", "kiện tụng", "khởi kiện", "pháp lý"),
    "credit_policy_override": ("chính sách tín dụng", "hạn mức tín dụng", "nới hạn mức", "vượt hạn mức"),
}


def classify_topic(text: str, deny_topics: Sequence[str]) -> str | None:
    """`topic_classifier` (input, §11.1): the first `prohibitions.deny_topics` entry
    `text` mentions, or `None`. Matches a topic's slug (`credit_policy_override`),
    its words with underscores turned to spaces, and the Vietnamese a user is
    actually likely to write (`_TOPIC_SYNONYMS`) — a question is prose, in the
    reader's own language, and a topic name is an English slug.
    """
    lowered = text.lower()
    for topic in deny_topics:
        topic_lower = topic.lower()
        phrases = (topic_lower, topic_lower.replace("_", " "), *_TOPIC_SYNONYMS.get(topic_lower, ()))
        if any(phrase in lowered for phrase in phrases):
            return topic
    return None


_NUMBER_PATTERN = re.compile(r"\d")
_CITATION_MARKERS = ("nguồn:", "nguồn ", "(nguồn", "theo nguồn", "dựa trên")


def citation_present(text: str) -> bool:
    """`citation_required` (output, §11.1): whether a reply that states a figure
    also names where it came from. A reply with no digit in it at all is never
    held to this rule — there is no figure to source.
    """
    if not _NUMBER_PATTERN.search(text):
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in _CITATION_MARKERS)


def route_low_confidence(confidence: float | None, *, threshold: float) -> bool:
    """`confidence_router` (output, §11.1): whether this turn's reported confidence
    falls under the spec's own `guardrails.confidence_threshold` and must be
    escalated rather than shown as the model's own answer. `None` — a model that
    reported no confidence at all — never triggers escalation: silence is not
    evidence of low confidence.
    """
    return confidence is not None and confidence < threshold


#: Each fallback says, in its own way, that the agent *cannot* answer — and says
#: it in the words a reader would use ("không thể", "ngoài phạm vi"), not only by
#: describing what happens next.
#:
#: `ESCALATE_TO_HUMAN` used to say only "Câu hỏi này cần con người xử lý — đã
#: chuyển yêu cầu tới bộ phận phụ trách", which announces the handoff without
#: ever stating the refusal. That reads to a user as "an answer is coming", and
#: it is why a turn this guardrail correctly diverted still counted as an
#: unrefused one: the golden set's `refusal` cases look for exactly the words the
#: message omitted. Naming the limit is the message's job; naming who picks it up
#: is the second half, not the first.
_FALLBACK_MESSAGES: dict[Fallback, str] = {
    Fallback.ESCALATE_TO_HUMAN: (
        "Câu hỏi này ngoài phạm vi tôi có thể trả lời, nên tôi không thể tự xử lý — "
        "đã chuyển yêu cầu tới bộ phận phụ trách."
    ),
    Fallback.REFUSE: "Xin lỗi, tôi không thể trả lời câu hỏi này vì nằm ngoài phạm vi được phép.",
    Fallback.ANSWER_WITH_DISCLAIMER: "Chưa đủ chắc chắn để trả lời — vui lòng xác minh lại với người phụ trách trước khi dùng số liệu này.",
}


def fallback_message(fallback: Fallback) -> str:
    """The sentence `spec.guardrails.fallback` says to show whenever a guardrail diverts a turn."""
    return _FALLBACK_MESSAGES[fallback]
