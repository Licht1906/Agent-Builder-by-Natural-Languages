"""`METRIC_CATALOG` pins the table ADR-0054 §4 registers — RAGAS/DeepEval on the platform's own categories."""

from pathlib import Path

from src.evals.metrics import DEFAULT_THRESHOLD, METRIC_CATALOG, applies, required_nodes, spec_for
from src.models.node_registry import NodeType
from src.models.solution_spec import SuccessCriterionCategory


def test_the_catalog_has_exactly_the_eight_rows_adr_0054_registers() -> None:
    assert [spec.category for spec in METRIC_CATALOG] == [
        SuccessCriterionCategory.WORKFLOW_SUCCESS,
        SuccessCriterionCategory.TOOL_CORRECTNESS,
        SuccessCriterionCategory.HUMAN_APPROVAL_COMPLIANCE,
        SuccessCriterionCategory.SOURCE_GROUNDEDNESS,
        SuccessCriterionCategory.CORRECTNESS,
        SuccessCriterionCategory.RELEVANCE,
        SuccessCriterionCategory.CONTEXT_PRECISION,
        SuccessCriterionCategory.TASK_COMPLETION,
    ]


def test_every_row_carries_its_ragas_or_deepeval_name_and_a_threshold() -> None:
    by_category = {spec.category: spec for spec in METRIC_CATALOG}

    groundedness = by_category[SuccessCriterionCategory.SOURCE_GROUNDEDNESS]
    assert groundedness.ragas == "faithfulness"
    assert groundedness.grader == "judge"
    assert groundedness.threshold == DEFAULT_THRESHOLD == 0.7

    relevance = by_category[SuccessCriterionCategory.RELEVANCE]
    assert relevance.ragas == "answer_relevancy"
    assert relevance.deepeval == "AnswerRelevancy"

    workflow_success = by_category[SuccessCriterionCategory.WORKFLOW_SUCCESS]
    assert workflow_success.grader == "rule"
    assert workflow_success.ragas is None


def test_context_precision_is_the_only_row_that_requires_a_node_type() -> None:
    context_precision = spec_for(SuccessCriterionCategory.CONTEXT_PRECISION)
    assert context_precision is not None
    assert context_precision.applies_when == (NodeType.KNOWLEDGE_SEARCH, NodeType.WEB_SEARCH)
    assert all(
        spec.applies_when == ()
        for spec in METRIC_CATALOG
        if spec.category is not SuccessCriterionCategory.CONTEXT_PRECISION
    )


def test_applies_refuses_context_precision_on_a_graph_that_retrieves_nothing() -> None:
    context_precision = spec_for(SuccessCriterionCategory.CONTEXT_PRECISION)
    assert context_precision is not None

    assert applies(context_precision, frozenset()) is False
    assert applies(context_precision, frozenset({NodeType.AGENT_LLM})) is False
    assert applies(context_precision, frozenset({NodeType.MCP, NodeType.END})) is False


def test_context_precision_applies_to_either_retrieval_node_type() -> None:
    """`WebSearch` returns text an answer can be ungrounded against exactly as `KnowledgeSearch` does.

    Any *one* of the named types is enough: they are two doors onto one input
    (`retrieval_context`), not two separate requirements.
    """
    context_precision = spec_for(SuccessCriterionCategory.CONTEXT_PRECISION)
    assert context_precision is not None

    assert applies(context_precision, frozenset({NodeType.KNOWLEDGE_SEARCH})) is True
    assert applies(context_precision, frozenset({NodeType.WEB_SEARCH})) is True
    assert applies(context_precision, frozenset({NodeType.WEB_SEARCH, NodeType.KNOWLEDGE_SEARCH})) is True
    assert applies(context_precision, frozenset({NodeType.AGENT_LLM, NodeType.WEB_SEARCH})) is True


def test_a_row_with_no_applies_when_applies_to_every_graph() -> None:
    correctness = spec_for(SuccessCriterionCategory.CORRECTNESS)
    assert correctness is not None
    assert applies(correctness, frozenset()) is True


def test_required_nodes_answers_the_same_for_no_row_and_a_row_naming_nothing() -> None:
    """Both mean *nothing about the graph disqualifies this* — a caller must not be able to tell them apart."""
    assert required_nodes(SuccessCriterionCategory.CONTEXT_PRECISION) == (
        NodeType.KNOWLEDGE_SEARCH,
        NodeType.WEB_SEARCH,
    )
    assert required_nodes(SuccessCriterionCategory.CORRECTNESS) == ()
    assert required_nodes(SuccessCriterionCategory.WORKFLOW_SUCCESS) == ()


def test_hallucination_is_not_a_ninth_row() -> None:
    """ADR-0054 §4: it is `source_groundedness` inverted, not a second measurement."""
    assert all("hallucination" != spec.category.value for spec in METRIC_CATALOG)


def test_no_source_file_imports_the_ragas_or_deepeval_packages() -> None:
    """AC: `grep -r "import ragas\\|import deepeval" src/` is empty."""
    src = Path(__file__).resolve().parents[2] / "src"
    offenders = [
        path
        for path in src.rglob("*.py")
        if any(needle in path.read_text(encoding="utf-8") for needle in ("import ragas", "import deepeval"))
    ]
    assert offenders == []
