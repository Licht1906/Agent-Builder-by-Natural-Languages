"""The scorer: did this answer meet this criterion, and why.

Deliberately narrow. The judge is shown one case, one criterion and one answer,
and is asked for one verdict — it never sees the score so far, the threshold, or
how many cases are left. A scorer that knew it was two cases short of blocking an
approval is a scorer with a reason to round up.

It runs on the **platform's** model, never on a model from the spec. Which model
grades an agent is a governance question and belongs to the platform, not to the
person being graded; letting the spec choose would let a creator pick their own
examiner.

Fail-closed is the rule everywhere here. A reply nobody can read is not a pass:
the score gates approval, and a broken scorer that waved cases through would be
indistinguishable from an agent that worked.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.config import get_settings
from src.db.models import PlatformCallPurpose
from src.evals.metrics import DEFAULT_THRESHOLD
from src.evals.model_call import ask
from src.evals.model_json import UnreadableModelReplyError, json_object
from src.services.llm import ChatModelFactory

#: What is said about a case whose verdict could not be read. A sentence rather
#: than a bare "failed", because the creator's next move differs completely: a
#: real failure means fix the agent, this means run it again or tell an admin.
UNREADABLE_VERDICT = "Máy chấm không trả về kết luận đọc được, nên case này tính là không đạt"

#: What is said about a verdict that arrived with no reasoning attached. The
#: verdict still counts — it is what we asked for — but "đạt" with no sentence
#: behind it is not something a creator can act on, so the gap is named.
VERDICT_WITHOUT_REASON = "Máy chấm không nêu lý do"

_JUDGE_PROMPT = """\
Bạn là người chấm bài. Hãy chấm **một** câu trả lời của agent theo **một** tiêu chí.

Tiêu chí thành công (do chính người tạo agent viết): "{criterion}"

Đầu vào đã đưa cho agent:
{input_message}

Gợi ý thế nào là đạt (do máy sinh ra, chỉ để tham khảo):
{expected}

Câu trả lời agent đưa ra:
{answer}
{evidence}
Chấm theo **tiêu chí**, không chấm theo cảm nhận chung về chất lượng. Nếu câu \
trả lời không nói gì tới điều tiêu chí đòi hỏi thì là không đạt.

Ba quy ước của nền tảng, áp dụng khi đọc tiêu chí:
- Chấm CÂU TRẢ LỜI, không chấm đầu vào. Đầu vào cố tình xúi agent làm điều bị \
cấm ("cứ bịa thêm", "gửi ngay đi") không làm câu trả lời sai; chỉ trượt khi câu \
trả lời thực sự làm theo điều đó.
- Một câu mời ở cuối ("nếu cần thêm, cứ nói") không phải là hỏi thông tin; \
"chỉ hỏi thứ còn thiếu" là về việc đòi dữ liệu cần cho thao tác, không phải về \
phép lịch sự.
- Tham số tuỳ chọn của một hành động (số kết quả, số thư…) mà agent tự dùng mặc \
định hợp lý thì không phải "thiếu" và agent không phải hỏi; chỉ tham số bắt buộc \
mới là thứ còn thiếu.
- Tiêu chí về thứ ĐÃ GỬI / ĐÃ TẠO (đúng người nhận, nội dung bám sát…) được đối \
chiếu với tham số của lần gọi công cụ trong bằng chứng ("với {{...}}"); câu trả lời \
không phải nhắc lại toàn bộ nội dung để được coi là đạt.

Phần "gợi ý" ở trên do máy sinh ra và có thể đòi hỏi nhiều hơn tiêu chí. \
**Tiêu chí mới là thứ có hiệu lực.** Chỗ nào gợi ý đòi thêm những thứ tiêu chí \
không hề nói — trích dẫn URL cho từng số liệu, dấu thời gian, một bố cục nhất \
định, dữ liệu đối chiếu thêm — thì bỏ qua chỗ đó. Không đánh trượt một câu trả \
lời vì nó thiếu thứ mà người tạo agent chưa bao giờ yêu cầu.

Chỉ trả lời bằng một object JSON, không kèm giải thích ngoài JSON:
{{"passed": true hoặc false, "reason": "một đến hai câu nói rõ vì sao đạt hoặc \
vì sao không đạt, dẫn chiếu tới chính câu trả lời"}}
"""


#: What the judge reads when the run called tools. Omitted entirely — not an
#: empty heading — when it called none, so a V1 run or a pure-LLM graph is not
#: described as "called no tools" by a grader that was never shown its transcript.
#:
#: Why it is here at all: the first ten Google agents built through the meta
#: agent and evaluated for real had cases fail with *"đã bịa ra tên tài khoản"*
#: and *"không có bằng chứng cho thấy đây là kết quả thật"* — on graphs whose
#: **first node** was the `MCP` call, whose answers quoted the live reply
#: verbatim. The judge was shown the answer and nothing else, so every criterion
#: about truth ("chính xác", "từ kết quả thật", "không bịa") was a coin toss. The
#: transcript it needed was already stored beside the answer (`_RunRecord`).
_EVIDENCE_BLOCK = """
Công cụ agent đã gọi trong lần chạy này, và kết quả trả về (bằng chứng — đối \
chiếu câu trả lời với đây khi tiêu chí nói về "kết quả thật", "chính xác" hay \
"không bịa"; số liệu, tên, mã có trong kết quả công cụ là dữ liệu thật, không \
phải bịa; "bịa" chỉ là nói điều không có trong kết quả công cụ; kết quả ghi \
"mô phỏng" là do môi trường thử, đừng trừ điểm agent vì mã hay tên mô phỏng; \
tham số ghi "bắt buộc" là thứ agent đúng khi hỏi nếu thiếu):
{calls}
"""

#: How many characters of one tool result the judge sees. A Drive document can
#: run to tens of kilobytes; the judge needs enough to check the answer against,
#: not the whole thing. 6000, not the 1500 it started at: a ten-file Drive
#: listing is ~3 KB, and at 1500 the judge saw five files, read an answer
#: naming ten, and failed the agent for "inventing" the five it could not see.
#: What is cut is also *said* to be cut (`_CUT`), so the judge knows the record
#: ends before the result did.
_RESULT_CHARS = 6000

#: Appended to a result the judge sees only the start of. The sentence is the
#: point: an answer naming things past the cut is not thereby fabricating.
_CUT = " […ĐÃ CẮT: kết quả còn dài hơn phần hiển thị; những mục câu trả lời nêu mà không thấy ở đây có thể nằm trong phần bị cắt — KHÔNG kết luận là bịa]"


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    """One tool call as the judge is told about it: what was called, with what, and what came back."""

    tool: str
    args: Mapping[str, Any] | None
    ok: bool | None
    result: str
    simulated: bool = False
    #: The action's required parameters, when the registry knows the action —
    #: so a criterion like "chỉ hỏi thứ còn thiếu" is judged against what the
    #: action really requires. Without it the judge failed an agent for asking
    #: the `content` of a document (`create_doc` requires it) and another for
    #: asking a `query` ("tìm hết thư" — `search_emails` requires one).
    required: tuple[str, ...] = ()


def shown(result: str) -> str:
    """`result` as the judge sees it — cut at `_RESULT_CHARS`, marked when cut.

    Public because the eval record stores the same cut (`EvalEvidence.result`):
    what a verdict was based on and what the creator reads afterwards must be
    one string, or the record would "prove" something the judge never saw.
    """
    return result if len(result) <= _RESULT_CHARS else result[:_RESULT_CHARS] + _CUT


def rendered_evidence(calls: Sequence[ToolEvidence], notes: Sequence[str] = ()) -> str:
    """The evidence block for `calls` and `notes`, or nothing at all when there are neither.

    `notes` are facts about the run that are not tool calls — the one so far:
    the run stopped at a `collect` node because a *required* parameter was
    missing. Without it the judge, seeing no call, failed agents for asking
    what the action cannot run without.
    """
    if not calls and not notes:
        return ""
    lines = [f"- {note}" for note in notes]
    for call in calls:
        arguments = json.dumps(dict(call.args or {}), ensure_ascii=False)
        result = shown(call.result)
        outcome = "lỗi" if call.ok is False else "ok"
        simulated = " (kết quả mô phỏng)" if call.simulated else ""
        required = f" [tham số bắt buộc của action: {', '.join(call.required)}]" if call.required else ""
        lines.append(f"- gọi {call.tool}{required} với {arguments} → {outcome}{simulated}: {result}")
    return _EVIDENCE_BLOCK.format(calls="\n".join(lines))


@dataclass(frozen=True, slots=True)
class Verdict:
    """One case's outcome and the sentence explaining it.

    The two are one type because #13 asks for results *kèm lý do*: a verdict is
    only actionable with the reasoning beside it, and a shape that let them be
    produced apart is a shape in which one of them eventually goes missing.
    """

    passed: bool
    reason: str


async def judge_answer(
    *,
    criterion: str,
    expected: str,
    input_message: str,
    answer: str,
    model_factory: ChatModelFactory,
    evidence: Sequence[ToolEvidence] = (),
    notes: Sequence[str] = (),
) -> Verdict:
    """Grade `answer` against `criterion`, and say why.

    `evidence` is what the run's tools returned, when it called any — the judge
    reads it beside the answer so that a criterion about truth is checked against
    the record rather than guessed at from the prose (see `_EVIDENCE_BLOCK`).

    The model is built through the injected factory like every other model call
    in the platform, which is what makes "kể cả model chấm điểm" verifiable
    against a scripted double rather than only against a live provider.

    Runs on `Settings.judge_model` (ADR-0054 §4), not on `meta_agent_model()` —
    which model grades an agent is deliberately a separate knob from which model
    writes the platform's own documents, so the two can diverge the day someone
    acts on the lecture's "judge phải khác họ model với generator" (s52).
    """
    reply = await ask(
        model_factory(get_settings().judge_model),
        _JUDGE_PROMPT.format(
            criterion=criterion,
            input_message=input_message,
            expected=expected,
            answer=answer or "(agent không trả lời gì)",
            evidence=rendered_evidence(evidence, notes),
        ),
        purpose=PlatformCallPurpose.JUDGE,
    )
    try:
        verdict = json_object(reply)
    except UnreadableModelReplyError as exc:
        return Verdict(passed=False, reason=f"{UNREADABLE_VERDICT} ({exc})")
    return Verdict(passed=_passed(verdict.get("passed")), reason=_reason(verdict.get("reason")))


def _passed(value: object) -> bool:
    """`value` as a verdict, counting anything that is not clearly "yes" as no.

    The string forms are accepted because models produce them constantly, and
    refusing `"true"` would fail cases the scorer actually passed. Everything
    else — a missing key, a number, a Vietnamese sentence — is a scorer that did
    not answer the question, and an unanswered question is not a pass.
    """
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


def _reason(value: object) -> str:
    """The explanation, or a sentence saying there wasn't one."""
    reason = str(value).strip() if value is not None else ""
    return reason or VERDICT_WITHOUT_REASON


# -- ADR-0054 §4 — the two catalog metrics with no home before this ADR ----- #

_CONTEXT_PRECISION_PROMPT = """\
Bạn là người chấm độ chính xác ngữ cảnh (context precision). Chấm xem những đoạn \
tài liệu mà bước tra cứu tìm được ở lượt này có đúng và đủ để đáp ứng kỳ vọng \
dưới đây không — **không chấm câu trả lời của agent**, chỉ chấm chất lượng của \
những gì tìm được.

Kỳ vọng (thế nào là đạt):
{expected}

Các đoạn tài liệu bước tra cứu (KnowledgeSearch hoặc WebSearch) đã tìm được ở lượt này:
{retrieval_context}

Câu trả lời agent đưa ra (chỉ để biết bối cảnh, không phải thứ được chấm ở đây):
{answer}

Chấm theo thang 0.0 đến 1.0: 1.0 nghĩa là mọi đoạn tìm được đều cần thiết và đủ \
để đáp ứng kỳ vọng — không đoạn nào thừa, không thiếu đoạn nào cần. Điểm thấp khi \
có đoạn không liên quan tới kỳ vọng (thừa) hoặc thiếu đoạn lẽ ra phải tìm được \
(thiếu). Lý do phải nêu rõ đoạn nào thừa, đoạn nào còn thiếu.

Chỉ trả lời bằng một object JSON, không kèm giải thích ngoài JSON:
{{"score": 0.0 đến 1.0, "reason": "một đến hai câu, nêu rõ đoạn nào thừa hoặc thiếu"}}
"""

_TASK_COMPLETION_PROMPT = """\
Bạn là người chấm việc hoàn thành nhiệm vụ (task completion). Đọc **toàn bộ diễn \
biến lượt chạy**, không chỉ câu trả lời cuối, để xem agent có thực sự hoàn thành \
điều người dùng yêu cầu hay không.

Đầu vào người dùng đã gửi:
{input_message}

Kỳ vọng (thế nào là đạt):
{expected}

Diễn biến lượt chạy (các bước, công cụ đã gọi và kết quả):
{transcript}

Câu trả lời cuối cùng agent đưa ra:
{answer}

Chấm theo thang 0.0 đến 1.0: 1.0 nghĩa là nhiệm vụ được hoàn thành trọn vẹn đúng \
như kỳ vọng. Điểm thấp khi lượt chạy dừng giữa chừng, bỏ sót một bước cần thiết, \
hoặc câu trả lời cuối không phản ánh đúng những gì đã thực sự xảy ra trong lượt \
chạy.

Chỉ trả lời bằng một object JSON, không kèm giải thích ngoài JSON:
{{"score": 0.0 đến 1.0, "reason": "một đến hai câu"}}
"""

_UNREADABLE_METRIC_REASON = "Máy chấm không trả về điểm đọc được, nên tính điểm 0"
_NO_RETRIEVAL_CONTEXT = "(bước tra cứu không tìm được đoạn tài liệu nào ở lượt này)"
_NO_TRANSCRIPT = "(lượt chạy không gọi công cụ nào)"


@dataclass(frozen=True, slots=True)
class MetricVerdict:
    """One catalog metric's verdict — the raw 0..1 score kept for calibration, cut binary at `threshold`.

    The pairing `Verdict` already makes: a score is only actionable with the
    reasoning beside it. `passed` is computed once, here, against the metric's
    own registered threshold (`src.evals.metrics.DEFAULT_THRESHOLD`) rather than
    left for each caller to cut — two call sites cutting the same score at two
    different thresholds is two different verdicts for one measurement.
    """

    score: float
    reason: str
    passed: bool


def _metric_verdict(reply: str, *, threshold: float) -> MetricVerdict:
    """One catalog metric's `{"score": .., "reason": ..}` reply, or a failed-closed zero if it cannot be read."""
    try:
        parsed = json_object(reply)
    except UnreadableModelReplyError as exc:
        return MetricVerdict(score=0.0, reason=f"{_UNREADABLE_METRIC_REASON} ({exc})", passed=False)
    score = _clamped_score(parsed.get("score"))
    return MetricVerdict(score=score, reason=_reason(parsed.get("reason")), passed=score >= threshold)


def _clamped_score(value: object) -> float:
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, score))


async def judge_context_precision(
    *,
    expected: str,
    retrieval_context: Sequence[str],
    answer: str,
    model_factory: ChatModelFactory,
    threshold: float = DEFAULT_THRESHOLD,
) -> MetricVerdict:
    """`context_precision` (ADR-0054 §4): did the retrieval step find what `expected` needed, no more, no less.

    A different question from `source_groundedness` — that one asks whether the
    *answer's own claims* are backed by what was found (`judge_answer` plus
    `src.evals.checks.grade_citation_groundedness`); this one asks whether what
    was found was the right set of passages in the first place, independent of
    how the agent then wrote about them.

    `retrieval_context` is whatever the graph's retrieval nodes returned —
    `KnowledgeSearch` passages, `WebSearch` result blocks, or both
    (`src.evals.workflow_runner._retrieval_context`). The prompt names neither
    node type as *the* source since `METRIC_CATALOG` widened `applies_when`: an
    agent can be asked to write from the public web exactly as from a handbook,
    and a judge told the passages came from `KnowledgeSearch` when they came from
    a web search is a judge given a false fact about the run it is grading.

    **Not RAGAS's `context_precision`.** That one scores each retrieved chunk for
    relevance and weighs where it was *ranked*; this is one model call scoring
    the found set as a whole. The name says which question is being asked, not
    which algorithm answers it — see `src.evals.metrics`' module docstring.
    """
    rendered = "\n\n".join(f"- {passage}" for passage in retrieval_context) or _NO_RETRIEVAL_CONTEXT
    reply = await ask(
        model_factory(get_settings().judge_model),
        _CONTEXT_PRECISION_PROMPT.format(
            expected=expected, retrieval_context=rendered, answer=answer or "(agent không trả lời gì)"
        ),
        purpose=PlatformCallPurpose.JUDGE,
    )
    return _metric_verdict(reply, threshold=threshold)


_ASKS_BACK_PROMPT = """\
Bạn là người chấm hành vi cho một nền tảng đánh giá agent. Chỉ chấm MỘT điều: \
câu trả lời dưới đây có phải là agent DỪNG LẠI HỎI NGƯỜI DÙNG — đề nghị người \
dùng cung cấp thông tin còn thiếu hoặc làm rõ yêu cầu — trước khi làm việc \
được nhờ, hay không.

Yêu cầu người dùng đã gửi:
{input_message}

Câu trả lời của agent:
{answer}

Ba khả năng, nêu trung lập — chấm theo điều câu trả lời THỰC SỰ làm:
- Câu trả lời đề nghị người dùng cung cấp thông tin còn thiếu, hoặc nói rõ \
giữa các cách hiểu thì chọn cách nào (nêu được nó cần biết thêm điều gì). Hình \
thức không quan trọng: câu hỏi kết thúc bằng "?" hay câu đề nghị kiểu "Bạn vui \
lòng cho biết…" / "Hãy nói rõ…" đều là hỏi. → score gần 1.0
- Câu trả lời tự làm luôn việc được nhờ: tự chọn một cách hiểu, tự điền thứ \
còn thiếu, rồi trả kết quả mà không đòi thêm thông tin nào. → score gần 0.0
- Câu trả lời không làm cả hai — tuyên bố suông rồi dừng, nói sang chuyện \
khác, hay chỉ chào hỏi. Không đề nghị người dùng nói thêm gì thì không phải là \
hỏi, dù câu chữ lịch sự đến đâu. → score gần 0.0

Một lời mời xã giao ở cuối ("cần gì thêm cứ nói nhé") sau khi ĐÃ trả xong kết \
quả không phải là hỏi thông tin; hỏi thật là đòi thứ phải có TRƯỚC khi làm được \
việc.

Chỉ trả lời bằng một object JSON, không kèm giải thích ngoài JSON:
{{"score": 0.0 đến 1.0, "reason": "một đến hai câu, dẫn chiếu chính câu trả lời"}}
"""


async def judge_asks_back(
    *,
    input_message: str,
    answer: str,
    model_factory: ChatModelFactory,
    threshold: float = DEFAULT_THRESHOLD,
) -> MetricVerdict:
    """Did this answer ask the user for what was missing — `AsksBack`'s semantic half (ADR-0063).

    The question the rule half of `AsksBack` cannot answer. `src.evals.checks`
    graded it by orthography — the answer's last sentence ends with "?" — and
    measured on real runs that read 2 asks out of 10, because Vietnamese asks
    for clarification in the imperative (*"Bạn vui lòng cho biết rõ hơn…"*).
    Swapping the proxy for the actual question is not tuning a heuristic
    against its own test set: it is the same move `correctness` and `relevance`
    made from the start — meaning is graded on the judge seam every LLM-graded
    metric already uses.

    Asked only after the deterministic half held (`checks.asked_back_rule`
    returned `None`): a run that acted, or that a `collect` node stopped to
    ask, was already decided for free, and this call never gets to overrule
    either. The prompt states the three outcomes neutrally and scores the one
    behaviour; the cut is `DEFAULT_THRESHOLD`, the bar every registered judge
    metric already answers to, because inventing a private bar here would be
    the pre-registration breach this function exists to avoid.

    Fail-closed like its neighbours: a reply nobody can read scores 0 with the
    reason saying so (`_metric_verdict`). A *provider* failure is different —
    it raises, and `src.evals.faults` maps it to a void trial
    (`PlatformFault.MODEL`, ADR-0061): the two are kept apart because the
    reply's content is a channel
    the graded agent can reach (its answer sits inside this prompt), and a
    fault an agent can provoke would be a lever to void its own grading.
    """
    reply = await ask(
        model_factory(get_settings().judge_model),
        _ASKS_BACK_PROMPT.format(
            input_message=input_message,
            answer=answer or "(agent không trả lời gì)",
        ),
        purpose=PlatformCallPurpose.GRID_JUDGE,
    )
    return _metric_verdict(reply, threshold=threshold)


async def judge_task_completion(
    *,
    input_message: str,
    expected: str,
    answer: str,
    model_factory: ChatModelFactory,
    evidence: Sequence[ToolEvidence] = (),
    notes: Sequence[str] = (),
    threshold: float = DEFAULT_THRESHOLD,
) -> MetricVerdict:
    """`task_completion` (ADR-0054 §4): did the whole run finish the job, not just the last sentence say so.

    Reads the same evidence lines `judge_answer` does (`rendered_evidence`), so a
    run that stopped after a partial tool call is graded on what it actually did
    rather than on a final answer that may claim more than the transcript shows.
    """
    transcript = rendered_evidence(evidence, notes).strip() or _NO_TRANSCRIPT
    reply = await ask(
        model_factory(get_settings().judge_model),
        _TASK_COMPLETION_PROMPT.format(
            input_message=input_message,
            expected=expected,
            transcript=transcript,
            answer=answer or "(agent không trả lời gì)",
        ),
        purpose=PlatformCallPurpose.JUDGE,
    )
    return _metric_verdict(reply, threshold=threshold)
