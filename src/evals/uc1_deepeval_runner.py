"""UC1-29 (#130), spec.md §11.2, FR-33 — the LLM-judge grader for the golden
set's eight `correct_lookup` cases: two **DeepEval-style** metrics named
`faithfulness`/`answer_relevancy`, scored by the platform's judge model through
the same seam `src.evals.judge` already uses (§20's "Seam thứ hai: model
factory" — "kể cả model chấm điểm").

Deliberately not the `deepeval` PyPI package. That package ships its own model
client and its own network path — exactly the second "ask a model a question"
mechanism this platform's one-seam testing discipline exists to avoid (§20),
and NFR-05's air-gapped requirement rules out a metrics library that reaches
out by default.

**Say what this actually is, once, here.** It is *one* model call that is handed
the question, the retrieval context, the expected answer and the agent's answer,
and asked to return two numbers with a sentence each. That is not what the
libraries do to produce the metrics these two names come from: RAGAS
`faithfulness` decomposes the answer into individual claims and verifies each one
against the context, and DeepEval's `AnswerRelevancy` generates questions back
out of the answer and compares them with the one that was asked. Different
algorithms, different variance, different failure modes — so **the numbers below
are not comparable to any number published under those names**. They are
comparable across versions of this platform, which is the only comparison
`METRIC_PASS_THRESHOLD` and the FR-33 gate ever make of them. The names are kept
because they name the *question* well and because ADR-0054 §4's vocabulary bridge
is worth having; what is dropped is the claim of being the library's metric.

The module keeps its `deepeval` filename, and so do `deepeval_pass_rate`
(`src.agentspec.lifecycle`) and the `deepeval_below_threshold` problem code that
travels to the frontend: those are identifiers on a wire contract and in stored
records, and renaming them would rewrite history to fix a sentence. The sentence
is fixed here instead.

This module only *scores*. It takes each case's `input`/`expected_output`/
`retrieval_context` and the answer the agent actually gave, produced by
whoever ran the case — this module has no dependency on UC1-38's sandbox chat,
unlike UC1-30 (#131), precisely because scoring an answer does not require
this module to be the one that produced it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.config import get_settings
from src.db.models import PlatformCallPurpose
from src.evals.metrics import DEFAULT_THRESHOLD
from src.evals.model_call import ask
from src.evals.model_json import UnreadableModelReplyError, json_object
from src.evals.uc1_golden_set import CorrectLookupCase
from src.services.llm import ChatModelFactory

#: Below this, a case is not "câu trả lời có đúng ý không" — it is wrong, on
#: either axis. Distinct from `AgentSpec.evaluation.min_pass_rate` (the
#: *aggregate* bar across the whole 23-case golden set — UC1-17's territory,
#: not built by this batch of tickets): this is the per-case, per-metric bar,
#: and lowering that gate never moves this one. A case scored 0.0 here is still
#: a failed case in the rate the gate then reads.
#:
#: Re-exported from `src.evals.metrics.DEFAULT_THRESHOLD` (ADR-0054 §4) rather
#: than a second literal `0.7` — this module's `faithfulness`/`answer_relevancy`
#: ask the same two questions `METRIC_CATALOG` asks under `source_groundedness`/
#: `relevance`, and one question has exactly one pass bar. Not the 0.7 anybody
#: publishes for the library metrics of those names: this bar was registered for
#: *this* judge, and a number from a different algorithm does not inherit it.
METRIC_PASS_THRESHOLD = DEFAULT_THRESHOLD

_UNREADABLE_REASON = "Máy chấm không trả về điểm đọc được, nên case này tính điểm 0"

# **`faithfulness` asks for *support*, and the wording has to fight for it** —
# ADR-0059 §4, the one defect its calibration found.
#
# The old line read *"câu trả lời có bịa thêm điều gì KHÔNG có trong ngữ cảnh
# không?"*, which a model answers as **non-contradiction**: it looks for a
# statement the context refutes, finds none, and scores high. Its own reasons
# said so out loud — *"Câu trả lời không chứa thông tin sai lệch hoặc bịa
# đặt"*. Measured on `tools/eval-calibration`'s 48-answer ladder, that cost
# the FR-33 gate exactly the failures it exists to catch: an answer about the
# weather scored **0.875** and passed 88% of runs, and an otherwise-correct
# answer with one fabricated sentence spliced in scored **0.756** and also
# passed 88%.
#
# The rewrite makes the unit of judgement the *claim* rather than the answer,
# and spells out that "not found" counts as unsupported however plausible the
# claim is — the part a model will not infer on its own. Re-measured on the
# same ladder, same model, same 96 rows: off-topic **0.875 → 0.000** (88% → 0%
# pass), spliced fabrication **0.756 → 0.511** (88% → 0%), contradiction
# **0.275 → 0.156** (25% → 0%), fully-grounded answers unchanged at **1.000 /
# 100%**. Rank correlation against the ladder's intended order rose **0.565 →
# 0.809**, above both libraries' own scores on the same rows.
#
# Known cost, recorded rather than hidden: a *grounded but irrelevant* answer
# now scores 0.656 instead of 1.000, so the two axes correlate +0.23 where
# they used to correlate −0.39. The case still fails on `answer_relevancy`
# where it should, so no verdict changes — only the attribution blurs a
# little. Widening that back out belongs to a change that can measure it.

_JUDGE_PROMPT = """\
Bạn là người chấm bài theo lối DeepEval. Chấm MỘT câu trả lời của agent theo hai \
chỉ số, mỗi chỉ số từ 0.0 đến 1.0.

Câu hỏi đã hỏi agent:
{input}

Ngữ cảnh agent được phép dùng để trả lời (retrieval context):
{retrieval_context}

Câu trả lời kỳ vọng (tham khảo, không bắt agent lặp lại nguyên văn):
{expected_output}

Câu trả lời agent thực sự đưa ra:
{answer}

Chấm hai chỉ số:
- "faithfulness": ĐO ĐỘ CÓ CĂN CỨ, KHÔNG PHẢI ĐỘ KHÔNG-MÂU-THUẪN. Hãy tách câu \
trả lời thành từng khẳng định, rồi với MỖI khẳng định tự hỏi: có tìm được chỗ nào \
trong ngữ cảnh ở trên chống lưng cho nó không? Điểm = số khẳng định có chống lưng \
chia cho tổng số khẳng định. Một khẳng định KHÔNG tìm thấy căn cứ thì tính là \
KHÔNG có căn cứ — kể cả khi nó nghe hợp lý, đúng ngoài đời, hay chỉ đơn giản là \
không mâu thuẫn với ngữ cảnh. Câu trả lời nói về chuyện khác hẳn thì không khẳng \
định nào được ngữ cảnh chống lưng, nên điểm là 0.0, không phải điểm cao vì "không \
sai". 1.0 = mọi khẳng định đều truy được về ngữ cảnh; 0.0 = không khẳng định nào \
truy được.
- "answer_relevancy": câu trả lời có thực sự trả lời đúng câu hỏi đã hỏi không? \
1.0 = trả lời thẳng, đúng trọng tâm; 0.0 = lạc đề. Chấm ĐỘC LẬP với faithfulness: \
một câu bịa nhưng đúng trọng tâm vẫn được relevancy cao, và một câu chép đúng ngữ \
cảnh nhưng không trả lời câu hỏi vẫn phải bị relevancy thấp.

Chỉ trả lời bằng một object JSON, không kèm giải thích ngoài JSON:
{{"faithfulness": {{"score": 0.0 đến 1.0, "reason": "một câu"}}, \
"answer_relevancy": {{"score": 0.0 đến 1.0, "reason": "một câu"}}}}
"""


@dataclass(frozen=True, slots=True)
class MetricScore:
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class UC1JudgeResult:
    case_id: str
    faithfulness: MetricScore
    answer_relevancy: MetricScore

    @property
    def passed(self) -> bool:
        return self.faithfulness.score >= METRIC_PASS_THRESHOLD and self.answer_relevancy.score >= METRIC_PASS_THRESHOLD


async def judge_uc1_case(case: CorrectLookupCase, *, answer: str, model_factory: ChatModelFactory) -> UC1JudgeResult:
    """Score one `correct_lookup` case's `answer` on `faithfulness`/`answer_relevancy`.

    Runs on `Settings.judge_model` (ADR-0054 §4), never a model from the spec —
    the same reasoning `src.evals.judge.judge_answer` gives: which model grades
    an agent is a governance question, not the graded agent's own choice.
    """
    reply = await ask(
        model_factory(get_settings().judge_model),
        _JUDGE_PROMPT.format(
            input=case.input,
            retrieval_context="\n".join(f"- {passage}" for passage in case.retrieval_context),
            expected_output=case.expected_output,
            answer=answer or "(agent không trả lời gì)",
        ),
        purpose=PlatformCallPurpose.JUDGE,
    )
    try:
        verdict = json_object(reply)
    except UnreadableModelReplyError as exc:
        unreadable = MetricScore(score=0.0, reason=f"{_UNREADABLE_REASON} ({exc})")
        return UC1JudgeResult(case_id=case.id, faithfulness=unreadable, answer_relevancy=unreadable)
    return UC1JudgeResult(
        case_id=case.id,
        faithfulness=_metric_of(verdict.get("faithfulness")),
        answer_relevancy=_metric_of(verdict.get("answer_relevancy")),
    )


async def judge_uc1_cases(
    cases: Sequence[CorrectLookupCase], *, answers: Mapping[str, str], model_factory: ChatModelFactory
) -> list[UC1JudgeResult]:
    """Score every case in `cases` — the whole `judge: llm` group, in order.

    `answers` is keyed by `case.id`; a case with no entry is scored as an
    unanswered turn (`answer=""`) rather than skipped — a case whoever ran the
    golden set never actually produced an answer for is still a case the
    golden set names, and silently dropping it would let a version's reported
    pass rate look better than what actually ran.
    """
    return [await judge_uc1_case(case, answer=answers.get(case.id, ""), model_factory=model_factory) for case in cases]


def _metric_of(value: object) -> MetricScore:
    if not isinstance(value, dict):
        return MetricScore(score=0.0, reason=_UNREADABLE_REASON)
    return MetricScore(score=_score_of(value.get("score")), reason=_reason_of(value.get("reason")))


def _score_of(value: object) -> float:
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, score))


def _reason_of(value: object) -> str:
    reason = str(value).strip() if value is not None else ""
    return reason or "Máy chấm không nêu lý do"
