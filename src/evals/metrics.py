"""The catalog: RAGAS and DeepEval names, mapped onto the platform's six-then-eight categories — ADR-0054 §4.

Mentors keep naming "RAGAS, DeepEval" as though they were a different grading
machine. They are not a machine this platform runs — faithfulness, answer
relevancy, context precision/recall, tool correctness, task completion are a
*vocabulary*, and for every one of them this platform already asks a question of
its own somewhere, on its own seam (`src.evals.judge`, `src.evals.checks`). What
was missing was the table saying which of ours answers which of theirs, and the
two measures that had no name here at all (`context_precision`,
`task_completion`).

**A mapping, never an equivalence — and the difference is the algorithm.** Every
`judge` row below is *one* model call that returns a 0..1 score with a reason.
The libraries these names come from do something else to reach their numbers:
RAGAS `faithfulness` breaks the answer into individual claims and verifies each
one against the context; DeepEval `AnswerRelevancy` generates questions back out
of the answer and compares them to the one that was asked; RAGAS
`context_precision` scores retrieved chunks for relevance *and* their ranking.
Different algorithms have different variance and different failure modes, so a
number produced here is **not comparable** to a number published under the same
name by those libraries. It is comparable to itself across versions of this
platform, which is what a threshold and a calibration set need it to be. Read
`ragas`/`deepeval` on a row as "this is the question they call X", never as
"this is X".

Nothing here calls the `ragas`/`deepeval` packages, and nothing here ever will —
UC-1's deepeval runner (removed with the UC-1 pipeline, DS-21) gave the reason
— a second network path, air-gap, a second seam nobody can keep in step with
the first — and this ADR keeps it: nothing is a `ragas`/`deepeval` import away from a
metric that already exists.
"""

from dataclasses import dataclass

from src.models.node_registry import NodeType
from src.models.solution_spec import SuccessCriterionCategory

#: The nickname-cut ADR-0054 §4 registers for every judge metric: a raw 0..1
#: score is kept for calibration (`results[].metrics[]`), but the verdict that
#: gates anything is binary at this line — the lecture's own rule (s52: "phán
#: quyết mang đi quyết định phải là nhị phân").
DEFAULT_THRESHOLD = 0.7


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One row of the mapping table — a platform category, labelled with the RAGAS/DeepEval name for the same question.

    `ragas`/`deepeval` are display strings, read by a tooltip or a doc table
    (§5), never by code that branches on them — the only name that ever routes
    anything is `category`. They say *which question of theirs this row answers*,
    not that this row is their metric; the module docstring says why that
    distinction is load-bearing. `required_inputs` says what a run has to have
    produced before this metric can be asked about it, so a caller can tell
    "not applicable" from "scored zero" before spending a judge call.
    """

    category: SuccessCriterionCategory
    ragas: str | None
    deepeval: str | None
    required_inputs: tuple[str, ...]
    grader: str  # "rule" | "judge"
    #: The node types that can feed this metric — **any one** of them somewhere
    #: in the graph is enough — or `()` when it needs no particular node.
    #: `context_precision` has nothing to read on a graph that retrieves nothing,
    #: and asking it about such a graph would put a question to it that the graph
    #: cannot even pose.
    #:
    #: A tuple rather than one `NodeType` because retrieval has two doors: a
    #: `KnowledgeSearch` node returns passages from the creator's own documents
    #: and a `WebSearch` node returns per-result blocks from the public web
    #: (`src.agents.tools.builtin._format_search_results`), both land in the same
    #: `findings` channel, and an answer can be ungrounded against either. "Any
    #: one of" rather than "all of" for the same reason: they are alternative
    #: sources of one input (`retrieval_context`), not two inputs both required.
    applies_when: tuple[NodeType, ...] = ()
    threshold: float = DEFAULT_THRESHOLD


#: The whole table, in the order ADR-0054 §4 lists it. Eight rows: the six
#: `SuccessCriterionCategory` already had, both told in RAGAS/DeepEval terms,
#: plus the two the ADR adds. `hallucination` is deliberately not a ninth row —
#: it is `source_groundedness`'s own number, inverted, and a second row for one
#: measurement is the "hai số cho một phép đo" the ADR names and refuses.
METRIC_CATALOG: tuple[MetricSpec, ...] = (
    MetricSpec(
        category=SuccessCriterionCategory.WORKFLOW_SUCCESS,
        ragas=None,
        deepeval="TaskCompletion (một phần)",
        required_inputs=("transcript",),
        grader="rule",
    ),
    MetricSpec(
        category=SuccessCriterionCategory.TOOL_CORRECTNESS,
        ragas=None,
        deepeval="ToolCorrectness",
        required_inputs=("tool_calls",),
        grader="rule",
    ),
    MetricSpec(
        category=SuccessCriterionCategory.HUMAN_APPROVAL_COMPLIANCE,
        ragas=None,
        deepeval=None,
        required_inputs=("approvals",),
        grader="rule",
    ),
    MetricSpec(
        category=SuccessCriterionCategory.SOURCE_GROUNDEDNESS,
        ragas="faithfulness",
        deepeval="Faithfulness · Hallucination (nghịch)",
        required_inputs=("answer", "retrieval_context"),
        grader="judge",
    ),
    MetricSpec(
        category=SuccessCriterionCategory.CORRECTNESS,
        ragas=None,
        deepeval="G-Eval",
        required_inputs=("answer", "expected"),
        grader="judge",
    ),
    MetricSpec(
        category=SuccessCriterionCategory.RELEVANCE,
        ragas="answer_relevancy",
        deepeval="AnswerRelevancy",
        required_inputs=("input", "answer"),
        grader="judge",
    ),
    MetricSpec(
        category=SuccessCriterionCategory.CONTEXT_PRECISION,
        ragas="context_precision · context_recall",
        deepeval="ContextualPrecision/Recall",
        required_inputs=("retrieval_context", "expected"),
        grader="judge",
        applies_when=(NodeType.KNOWLEDGE_SEARCH, NodeType.WEB_SEARCH),
    ),
    MetricSpec(
        category=SuccessCriterionCategory.TASK_COMPLETION,
        ragas=None,
        deepeval="TaskCompletion",
        required_inputs=("input", "transcript", "answer"),
        grader="judge",
    ),
)

_BY_CATEGORY: dict[SuccessCriterionCategory, MetricSpec] = {spec.category: spec for spec in METRIC_CATALOG}


def spec_for(category: SuccessCriterionCategory) -> MetricSpec | None:
    """The catalog row for `category`, or `None` for a category the table does not carry (there is none, today)."""
    return _BY_CATEGORY.get(category)


def applies(spec: MetricSpec, node_types: frozenset[NodeType]) -> bool:
    """Whether `spec` has anything to read on a graph shaped like `node_types`.

    `True` whenever the metric names no required node (most of the table) — a
    graph is never disqualified by a rule it was never subject to. Only
    `context_precision` currently names any, and one of the two is enough: they
    are two doors onto the same input, not two requirements.

    Called for real on the judging path (`src.evals.applicability`), which is
    what makes "don't ask a metric about a graph that cannot serve it" a rule
    the platform enforces rather than one it merely writes down — a metric this
    returns `False` for is reported *not applicable* in `eval_runs.skipped[]`
    and never reaches a judge or a denominator.
    """
    return not spec.applies_when or any(node_type in node_types for node_type in spec.applies_when)


def required_nodes(category: SuccessCriterionCategory) -> tuple[NodeType, ...]:
    """Which node types `category` can be fed by, or `()` when it needs none — the catalog's own answer.

    A function rather than two lines at each call site, because the "no row for
    this category" case has to answer the same as "a row that names no node":
    both mean *nothing about the graph disqualifies this*, and a caller that
    spelled the `None` check itself would eventually spell it as "skip it".
    """
    spec = spec_for(category)
    return () if spec is None else spec.applies_when
