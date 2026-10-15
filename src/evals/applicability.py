"""Whether a suite case still has something to say about *this* version — ADR-0054 §1.

A case's home is the agent's suite (`EvalSuite`); a version's spec and — for V2
— its confirmed graph are what say whether the case still makes sense *here*.
`run_eval`/`run_workflow_eval` call `applicability_of` on every case before
running it, once per run, so the classification a result is stamped with is
the one that actually decided whether the sandbox ran.

Three states, not two, because a case that cannot be graded is a different
problem from a case whose criterion the creator has since reworded:

- `ungrounded` — a case this version's graph cannot serve, in either of two
  ways. A rule-based case (`expected_check` set) whose action or node is not in
  the graph: running it would grade a string this graph can never produce. Or a
  case whose `category` is a `METRIC_CATALOG` row that names required node types
  (`applies_when`) and the graph has none of them — `context_precision` on a
  graph that retrieves nothing, which would put a question to the judge that the
  run has no material to answer. Neither is run; the caller reports both in
  `eval_runs.skipped[]` instead of `results[]`.
- `orphan` — a judge-graded case (no `expected_check`) whose `criterion` is no
  longer one of this version's `success_criteria`. Still run and still judged,
  against the case's own `expected` — a creator who reworded a criterion did
  not ask for the old case to stop testing anything, only for it to be marked
  as testing something this version no longer states.
- `live` — everything else: a rule-based case grounded in the graph, or a
  judge-graded case whose criterion the version still states.

**"Not applicable" is not "failed", and the shape is what keeps them apart.** An
ungrounded case produces a `SkippedCase`, never an `EvalCaseResult`, so it never
reaches `store_run`'s `passed_count`/`total_count` and therefore never reaches
`Score`, `src.evals.gate` or the approval bar. A metric the graph cannot serve
lowers neither the numerator nor the denominator by pretending to be a zero.
"""

from enum import StrEnum

from src.db.models import EvalCase
from src.evals.metrics import applies, required_nodes, spec_for
from src.models.eval_check import (
    ApprovalCompliance,
    AsksBack,
    CallsAction,
    ExpectedCheck,
    ForbidsAction,
    ReachesEnd,
    TakesNoAction,
    check_of,
)
from src.models.node_registry import NodeType
from src.models.schemas import CaseApplicability
from src.models.solution_spec import SuccessCriterionCategory
from src.models.workflow import WorkflowJSON
from src.workflow.runtime import mcp_action_names, side_effecting_actions


class Applicability(StrEnum):
    """`CaseApplicability` widened with the one state a run never executes.

    A separate type from the wire-facing `CaseApplicability` rather than that
    enum with a third member, because the two are read by different code: a
    result's `applicability` is always one a case actually ran under, and a
    type that could also hold `ungrounded` there would let a future caller
    write a result for a case that never ran.
    """

    LIVE = "live"
    ORPHAN = "orphan"
    UNGROUNDED = "ungrounded"


def applicability_of(case: EvalCase, *, success_criteria: frozenset[str], workflow: WorkflowJSON | None) -> Applicability:
    """Classify `case` against `success_criteria` and `workflow` — the whole of ADR-0054 §1's table.

    `workflow` is `None` for a V1 run: V1 cases carry neither an `expected_check`
    nor a `category` (`EvalCase`'s own docstring — both are V2 columns), so
    neither graph-reading branch below is ever taken there, and no graph is ever
    needed to decide it.

    The rule wins over the catalog when a case somehow has both. An
    `expected_check` is a statement somebody made about *this* case; the
    catalog's `applies_when` is a default about the category. Generation never
    puts a check on a `context_precision` case (`RULE_BASED_CATEGORIES` does not
    hold it), so today the order settles only a hand-written or imported case —
    and for that case, the rule its author wrote is the more specific claim.
    """
    check = check_of(case.expected_check)
    if check is not None:
        return Applicability.LIVE if _grounded(check, workflow) else Applicability.UNGROUNDED
    if not _servable(case.category, workflow):
        return Applicability.UNGROUNDED
    return Applicability.LIVE if case.criterion in success_criteria else Applicability.ORPHAN


def _servable(category: SuccessCriterionCategory | None, workflow: WorkflowJSON | None) -> bool:
    """Whether `workflow` has a node that can feed `category`'s catalog metric — ADR-0054 §4's `applies_when`.

    `True` for a category the catalog gives no required node (most of the
    table), and `True` for no category at all: a case that names no metric is
    not subject to a metric's precondition.

    `True` for `workflow is None`, too, and that one is a decision rather than a
    fallthrough. This rule is *read off a graph*, and a V1 run has no graph to
    read; answering "not applicable" from having nothing to look at would skip a
    case on ignorance rather than on a fact, which is the failure mode "not
    applicable ≠ scored zero" exists to avoid in the other direction. V1 cases
    carry no `category` anyway, so today this branch decides nothing — it is
    here so that the day a V2 case is run against a V1 version of the same
    agent's suite, the case is graded as it always was rather than silently
    disappearing.
    """
    if category is None or workflow is None:
        return True
    spec = spec_for(category)
    return spec is None or applies(spec, frozenset(node.type for node in workflow.nodes))


def _grounded(check: ExpectedCheck, workflow: WorkflowJSON | None) -> bool:
    """Whether `check` names an action or node `workflow` really has.

    Same grounding facts `src.evals.workflow_cases._GraphFacts` builds at
    generation time, read directly here rather than through that type: this is
    a single membership test per case per run, not a value worth building and
    threading through five helper methods for.

    The **superset** (`mcp_action_names`), deliberately, where generation uses
    the certain set — ADR-0058 §1 split the two. The question here is whether a
    stored check still means anything against *this* version's graph, and a
    `full`-mode node can reach every action its connection offers: a check
    grounded to one of them is applicable, and whether the run chose it is the
    verdict's business, not applicability's. Narrowing this to the certain set
    would skip real cases as "not in the graph" on a graph that calls them.

    ADR-0060's two behavioural rules name no action and no node, so nothing
    about a particular graph can fail to hold their *vocabulary* — which is what
    makes them the only rules a `full`-mode graph can be told anything by
    (`src.models.eval_check.CheckKind`). ADR-0060 read that as "grounded in
    every graph" for both of them, and for one of them that was wrong.

    **`TakesNoAction` is grounded only where the graph offers an action to
    decline** — ADR-0062 §3. Grounding asks whether a stored rule still *means*
    anything here, and a rule that cannot come out false means nothing: the
    whole of `checks._took_no_action` is "no line is flagged `simulated`", and
    on a graph whose `MCP` nodes offer nothing above `low` risk (or that has no
    `MCP` node at all) no run can ever write such a line. Skipping it is the
    honest answer and the shape is already right — an ungrounded case is
    reported in `skipped[]`, never as a zero, so a rule nobody could fail also
    never becomes a pass somebody quotes.

    `AsksBack` stays grounded everywhere, and the asymmetry is the point rather
    than an oversight. Its "nothing side-effecting happened" conjunct goes free
    on the same graphs — but its other half, *did the run actually stop and
    ask*, is a decision of the model readable off any transcript, so the rule
    still has a way to come out false. One vacuous conjunct is a weaker rule;
    a vacuous rule is not one.
    """
    if workflow is None:
        return False
    if isinstance(check, ReachesEnd | AsksBack):
        return True
    if isinstance(check, TakesNoAction):
        return bool(side_effecting_actions(workflow))
    if isinstance(check, CallsAction | ForbidsAction):
        return check.action in mcp_action_names(workflow)
    if isinstance(check, ApprovalCompliance):
        return check.node_id in {node.id for node in workflow.nodes_of_type(NodeType.HUMAN_APPROVAL)}
    return False


def ungrounded_reason(case: EvalCase) -> str:
    """Why `case` was skipped — the sentence `eval_runs.skipped[]` carries.

    Only meaningful after `applicability_of` has already returned `UNGROUNDED`
    for this case; re-deriving the check here rather than threading it through
    keeps the caller from having to carry two derived values for one field.

    The sentence has to say *not applicable*, not *failed*. A creator reading
    "graph này không có node tra cứu" knows the case is waiting for a graph that
    retrieves something; a creator reading a zero would go and rewrite an agent
    that was never asked the question.
    """
    check = check_of(case.expected_check)
    if isinstance(check, CallsAction | ForbidsAction):
        return f"Hành động '{check.action}' không có trong graph của version này"
    if isinstance(check, TakesNoAction):
        return (
            "Graph của version này không có hành động nào có tác dụng phụ để mà từ chối, "
            "nên luật 'không được làm gì' luôn đúng và không đo được điều gì về agent (không phải chấm trượt)"
        )
    if isinstance(check, ApprovalCompliance):
        return f"Node duyệt '{check.node_id}' không có trong graph của version này"
    if check is None and case.category is not None:
        needed = required_nodes(case.category)
        if needed:
            names = " hoặc ".join(node_type.value for node_type in needed)
            return (
                f"Metric '{case.category.value}' cần một node {names} trong graph mới có gì để chấm; "
                f"graph của version này không có node nào như vậy nên case không chạy (không phải chấm trượt)"
            )
    return "Luật của case này không áp dụng được cho graph của version này"


def as_case_applicability(applicability: Applicability) -> CaseApplicability:
    """`applicability` as the value a result may actually carry.

    Raises on `UNGROUNDED` rather than mapping it to something plausible: an
    ungrounded case never reaches `_result_for`, and a caller trying to stamp a
    result with it has a bug worth failing loudly on rather than a result
    worth serving.
    """
    if applicability is Applicability.LIVE:
        return CaseApplicability.LIVE
    if applicability is Applicability.ORPHAN:
        return CaseApplicability.ORPHAN
    raise ValueError(f"Case ungrounded không có kết quả để gắn applicability: {applicability}")
