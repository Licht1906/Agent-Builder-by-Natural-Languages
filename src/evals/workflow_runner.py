"""Running V2's test set: one sandboxed workflow run per case, then one verdict — #48.

The executing half of ADR-0028, and the V2 counterpart of `src.evals.runner`.
Everything that module says about *evidence* holds here unchanged and for the
same reasons: every case is a full run of the ordinary runtime with
`sandbox=True`, so a side-effecting action has no way to reach its real
implementation; every case gets its own conversation, so a set does not flatter
the agent the longer it gets; and a run that could not finish fails its case with
the reason attached rather than being graded on the fragment it managed.

Two things are genuinely new.

**A run here answers its own `HumanApproval` nodes.** ADR-0024 §7 makes a real
trial pause for a real person, and an auto-generated case has nobody standing by.
So an eval run selects the third execution mode (ADR-0028 §1): the decision comes
from the case's own `expected_check`, through a handler that never calls
`interrupt()` and never opens a `run_approvals` row. Which decision that is was
fixed at generation time, so a `human_approval_compliance` criterion gets one run
that approves and one that refuses.

**Half the cases are never shown to a judge.** A case carrying an
`expected_check` is graded by `src.evals.checks` reading the transcript this run
produced — the same transcript, through the same mapper, that a trial stores for
an admin to read. The remaining cases go to `judge_answer` exactly as ADR-0011
sends them.

Spend is deliberately not written to `run_costs`, for ADR-0011 §8's reason: an
eval candidate is a genuine `sandbox=True` run of the version, and a row here
would be picked up as "the measured cost of this version" and displace the trial
run the creator actually watched.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from src.admin.config import read_config
from src.agents.costs import CostFigure
from src.agents.runtime import Conversation, RunServices
from src.agents.threads import RunIdentity, ThreadNamespace, ThreadRef
from src.config import get_settings
from src.db.models import EvalCase, EvalRun, RunApprovalStatus
from src.evals.applicability import Applicability, applicability_of, as_case_applicability, ungrounded_reason
from src.evals.checks import (
    CheckVerdict,
    asked_back_rule,
    final_answer,
    grade_check,
    grade_citation_groundedness,
    grade_tool_groundedness,
)
from src.evals.faults import fault_of_run_error
from src.evals.judge import (
    ToolEvidence,
    judge_answer,
    judge_asks_back,
    judge_context_precision,
    judge_task_completion,
    shown,
)
from src.evals.metrics import DEFAULT_THRESHOLD
from src.evals.results import store_run
from src.evals.runner import RUN_DID_NOT_FINISH, EvalRunError, EvalServices, stopped_reason
from src.models.eval_check import ApprovalCompliance, AsksBack, ExpectedCheck, ForbidsAction, check_of
from src.models.node_registry import NodeType
from src.models.schemas import EvalCaseResult, EvalEvidence, MetricScoreOut, SandboxTranscriptEntry, SkippedCase
from src.models.solution_spec import SuccessCriterionCategory
from src.models.workflow import WorkflowJSON
from src.services.tracing import open_trace
from src.workflow.faults import PlatformFault
from src.workflow.knowledge import KnowledgeStore
from src.workflow.runtime import (
    BUDGET_EXCEEDED_EVENT,
    COST_EVENT,
    KNOWLEDGE_SEARCH_TOOL,
    WEB_SEARCH_TOOL,
    ScriptedApprovals,
    eval_decision,
    stream_workflow_reply,
)
from src.workflow.sandbox import transcript_entry
from src.workflow.tools import TOOL_DEFINITIONS

#: The two ADR-0054 §4 categories graded by a dedicated catalog metric instead
#: of the generic `judge_answer` — a different question each (did the search
#: find the right passages; did the whole run finish the job), not "did this
#: answer meet this criterion".
_METRIC_CATEGORIES = frozenset({SuccessCriterionCategory.CONTEXT_PRECISION, SuccessCriterionCategory.TASK_COMPLETION})


@dataclass(frozen=True, slots=True)
class _RunRecord:
    """What one eval run of a workflow left behind.

    Four fields because four different questions are asked of it: the
    transcript is what a rule reads, the answer is what a judge reads, the
    error is why neither may be trusted, and `fault` (ADR-0061) is whether the
    reason it died belongs to this platform rather than to the agent. They
    travel together because the decision they feed is one decision.

    `fault` is only ever the *run-level* one — the provider refusing the model
    call. A fault a tool hit is on the transcript line that hit it, and
    `src.evals.faults.trial_fault` reads both from one record.

    `cost` is the run's **measured** spend, read straight off the `cost` event
    the runtime emits on every leg (`CostFigure.from_payload`, the same figure a
    creator is shown). It was carried here so the retired grid could price each
    trial: a figure dropped at this seam is why `GRID_COST_CAP_USD` never braked
    (Phase C / ADR-0063) — `TrialObservation.cost_usd` stayed 0 and `spent`
    never grew. `None` only when the run died before the runtime could emit a
    cost at all.
    """

    transcript: list[SandboxTranscriptEntry] = field(default_factory=list)
    answer: str = ""
    error: str | None = None
    fault: PlatformFault | None = None
    cost: CostFigure | None = None


@dataclass(frozen=True, slots=True)
class _Subject:
    """The version under test, as one argument.

    Five values that are fixed for the whole set and meaningless apart — the
    graph to run, whose version it is, and the three seams the runs reach the
    world through. Passed as a bundle for `EvalServices`' own reason: five
    keywords repeated down three signatures is five chances to thread one of them
    wrongly, and the one that matters most is `knowledge`, whose absence would
    ground the whole test set in the wrong documents.
    """

    workflow: WorkflowJSON
    agent_id: int
    version_id: int
    services: EvalServices
    knowledge: KnowledgeStore


async def run_workflow_eval(
    db: Session,
    *,
    agent_id: int,
    version_id: int,
    workflow: WorkflowJSON,
    cases: Sequence[EvalCase],
    services: EvalServices,
    knowledge: KnowledgeStore,
    success_criteria: frozenset[str],
) -> EvalRun:
    """Run every case against `workflow` in sandbox mode, score the set, and store the run.

    Refuses an empty set rather than recording a perfect score against it, and
    reads the threshold once, here — both for `src.evals.runner`'s reasons, and
    both stated in one place because they are rules about *eval*, not about
    either runtime. `EvalRunError` is that module's type rather than a second one,
    so the HTTP layer has one refusal to translate.

    Every case is classified against `workflow`/`success_criteria` before it
    runs (ADR-0054 §1): a rule-based case whose action or node this graph does
    not have is `ungrounded` and never reaches the sandbox — it is reported in
    the returned run's `skipped[]` instead of costing a run nobody could grade.
    Since ADR-0054 §4's `applies_when` became enforceable, so is a case whose
    catalog metric names node types this graph has none of: `context_precision`
    on a graph that retrieves nothing is **not applicable**, which is a
    different fact from a score of zero and is kept apart from one by the shape
    — `skipped[]` never reaches `passed_count`/`total_count`, so a metric the
    graph cannot serve moves no gate in either direction.
    """
    if not cases:
        raise EvalRunError(
            "Chưa có test case nào để chạy — hãy sinh bộ test case từ tiêu chí thành công và điều cấm "
            "của spec, hoặc tự thêm case, rồi chạy lại"
        )
    threshold = read_config(db).eval_pass_threshold
    subject = _Subject(
        workflow=workflow, agent_id=agent_id, version_id=version_id, services=services, knowledge=knowledge
    )
    results: list[EvalCaseResult] = []
    skipped: list[SkippedCase] = []
    for case in cases:
        applicability = applicability_of(case, success_criteria=success_criteria, workflow=workflow)
        if applicability is Applicability.UNGROUNDED:
            skipped.append(SkippedCase(suite_case_id=case.id, reason=ungrounded_reason(case)))
            continue
        results.append(await _result_for(case, subject, applicability=applicability))
    return store_run(
        db, version_id, threshold=threshold, results=results, skipped=skipped, judge_model=get_settings().judge_model
    )


async def _result_for(case: EvalCase, subject: _Subject, *, applicability: Applicability) -> EvalCaseResult:
    """Run one case and grade it, by rule or by judge.

    **The rule is applied before the run's own health is considered**, and the
    order is deliberate. A run that crashed after calling a forbidden action still
    called it, and reporting only *"agent không chạy xong"* would file the one
    verdict nobody may override as an ordinary breakage. So a safety violation
    found on whatever transcript exists wins outright; only then does an
    unfinished run fail the case with the runtime's reason (ADR-0011 §3).
    """
    check = check_of(case.expected_check)
    record = await _eval_run(case.input_message, subject, scripted=_scripted_for(check))
    verdict = None if check is None else grade_check(check, record.transcript)
    if verdict is not None and verdict.safety_violation:
        return _result(case, check, record, verdict, applicability)
    if record.error is not None:
        stopped = CheckVerdict(passed=False, reason=f"{RUN_DID_NOT_FINISH}: {record.error}")
        return _result(case, check, record, stopped, applicability)
    if verdict is not None:
        # ADR-0063: on `AsksBack`, what the rule could not decide — is this
        # answer the agent asking? — goes to the judge, the same seam the
        # judge-graded categories below already stand on. After the error gate,
        # so a run with no answer is never graded on its fragment; a judge
        # provider failure propagates here exactly as it does for
        # `judge_answer` one branch down — the suite has no void to map it to.
        if isinstance(check, AsksBack) and asked_back_rule(record.transcript) is None:
            judged = await judge_asks_back(
                input_message=case.input_message,
                answer=final_answer(record.transcript),
                model_factory=subject.services.model_factory,
            )
            verdict = CheckVerdict(passed=judged.passed, reason=judged.reason)
        return _result(case, check, record, verdict, applicability)
    if case.category in _METRIC_CATEGORIES:
        return await _metric_result(case, check, record, applicability, subject)
    judged = await judge_answer(
        criterion=case.criterion,
        expected=case.expected,
        input_message=case.input_message,
        answer=record.answer,
        model_factory=subject.services.model_factory,
        evidence=tool_evidence(record.transcript),
        notes=collector_notes(record.transcript, subject.workflow),
    )
    judge_verdict = CheckVerdict(passed=judged.passed, reason=judged.reason)
    # ADR-0052: on every judged case, a link or id the answer names must be in
    # some tool result (or in the input). Deterministic, beside the judge —
    # the judge reads prose, this reads strings, and an invented
    # `…/your_document_id_here` is a string.
    grounded = grade_tool_groundedness(record.transcript, record.answer, input_message=case.input_message)
    if grounded is not None:
        judge_verdict = _both_hold(judge_verdict, grounded)
    # #102: `source_groundedness` supplements the judge with a deterministic
    # citation check, never in place of it. Reachable at all only because
    # `SOURCE_GROUNDEDNESS` is outside `RULE_BASED_CATEGORIES`
    # (`src.evals.workflow_cases`) and so never carries an `expected_check` —
    # `verdict` above is always `None` here, and `judge_answer` above always ran.
    if case.category is not SuccessCriterionCategory.SOURCE_GROUNDEDNESS:
        return _result(case, check, record, judge_verdict, applicability)
    citations = grade_citation_groundedness(record.transcript, record.answer)
    if citations is None:
        return _result(case, check, record, judge_verdict, applicability)
    return _result(case, check, record, _both_hold(judge_verdict, citations), applicability)


async def _metric_result(
    case: EvalCase, check: ExpectedCheck | None, record: _RunRecord, applicability: Applicability, subject: _Subject
) -> EvalCaseResult:
    """`context_precision`/`task_completion` — graded by a catalog metric, not by `judge_answer` (ADR-0054 §4).

    Each asks a different question than "did this answer meet this criterion",
    so each replaces the generic judge call rather than supplementing it the way
    `source_groundedness`'s citation check does. The raw score travels out in
    `EvalCaseResult.metrics` beside the binary verdict it was cut from.

    Reached only for a category this graph can actually serve: `run_workflow_eval`
    has already sent a case whose `applies_when` names nothing the graph has to
    `skipped[]`, so nothing here has to invent a score for a metric with no
    material. The score is this platform's own single-call judge, not the
    library metric whose name the catalog row carries — `src.evals.metrics`'
    module docstring says why the two are not comparable numbers.
    """
    if case.category is SuccessCriterionCategory.CONTEXT_PRECISION:
        verdict = await judge_context_precision(
            expected=case.expected,
            retrieval_context=_retrieval_context(record.transcript),
            answer=record.answer,
            model_factory=subject.services.model_factory,
        )
    else:
        verdict = await judge_task_completion(
            input_message=case.input_message,
            expected=case.expected,
            answer=record.answer,
            model_factory=subject.services.model_factory,
            evidence=tool_evidence(record.transcript),
            notes=collector_notes(record.transcript, subject.workflow),
        )
    metric = MetricScoreOut(
        metric=case.category.value,
        score=verdict.score,
        threshold=DEFAULT_THRESHOLD,
        passed=verdict.passed,
        reason=verdict.reason,
    )
    return _result(
        case,
        check,
        record,
        CheckVerdict(passed=verdict.passed, reason=verdict.reason),
        applicability,
        metrics=[metric],
    )


#: Which tool results count as *retrieval* for `context_precision` — the two
#: read-only search nodes, matching `METRIC_CATALOG`'s widened `applies_when`.
#: `WebSearch` belongs here for the same reason `KnowledgeSearch` does: it comes
#: back with per-result blocks the answering node then writes from
#: (`src.agents.tools.builtin._format_search_results` renders title, URL and the
#: snippet's own text, one bullet per result), and an answer can invent against
#: those exactly as it can against a handbook passage. A graph that searches the
#: web and is told its `retrieval_context` is empty gets a zero for a retrieval
#: it actually performed.
_RETRIEVAL_TOOLS = frozenset({KNOWLEDGE_SEARCH_TOOL, WEB_SEARCH_TOOL})


def _retrieval_context(transcript: Sequence[SandboxTranscriptEntry]) -> list[str]:
    """Every retrieval result this turn produced, as the judge-facing text — ADR-0054 §4.

    The same lines `src.evals.checks._citations_available` reads for its label
    set, kept as whole strings here instead: `context_precision` judges the
    *content* of what was found, not just which citation labels it offered.

    A failed search's result is kept rather than filtered out, and deliberately:
    *"Không tra cứu được tài liệu: …"* is what the run had to answer from, and a
    metric shown nothing at all would report the same empty context for a search
    that broke and one that never happened.
    """
    return [
        entry.content
        for entry in transcript
        if entry.kind == "tool_result" and entry.tool in _RETRIEVAL_TOOLS and entry.content
    ]


def tool_evidence(transcript: Sequence[SandboxTranscriptEntry]) -> list[ToolEvidence]:
    """The transcript's tool calls paired with their results, in the order they ran.

    The same lines `grade_check` reads for a rule-based verdict, handed to the
    judge for the judged ones: one record, read two ways, rather than a judge
    told a different story about the run than the rule checker was.
    """
    calls: list[ToolEvidence] = []
    pending: dict[str, tuple[str, Mapping[str, Any] | None, bool]] = {}
    for entry in transcript:
        key = f"{entry.node_id}:{entry.tool}"
        if entry.kind == "tool":
            pending[key] = (entry.tool or "", entry.args, bool(entry.simulated))
        elif entry.kind == "tool_result":
            tool, args, simulated = pending.pop(key, (entry.tool or "", None, bool(entry.simulated)))
            calls.append(
                ToolEvidence(
                    tool=tool,
                    args=args,
                    ok=entry.ok,
                    result=entry.content or "",
                    simulated=simulated,
                    required=_required_of(tool),
                )
            )
    for tool, args, simulated in pending.values():
        calls.append(
            ToolEvidence(
                tool=tool,
                args=args,
                ok=None,
                result="(không có kết quả ghi lại)",
                simulated=simulated,
                required=_required_of(tool),
            )
        )
    return calls


def collector_notes(transcript: Sequence[SandboxTranscriptEntry], workflow: WorkflowJSON) -> list[str]:
    """One sentence per `collect` node that stopped the run: which action, which required parameters.

    A run that ends on the collector's question made no tool call, so the
    judge has no evidence line to learn from that the parameter asked for is
    one the action cannot run without — and failed *"Tìm thư"* → "từ khoá?"
    for asking. The note says what the schema says.
    """
    notes = []
    for entry in transcript:
        if entry.kind != "node" or entry.node_type != NodeType.COLLECT.value or entry.port_taken != "incomplete":
            continue
        node = workflow.node(entry.node_id or "")
        target = workflow.node(str((node.config if node else {}).get("action_node") or ""))
        if target is None:
            continue
        qualified = f"{target.config.get('tool_id', '')}.{target.config.get('action', '')}"
        required = _required_of(qualified)
        if required:
            notes.append(
                f"Agent dừng ở bước thu thập để hỏi người dùng: action {qualified} BẮT BUỘC có "
                f"{', '.join(required)} — hỏi những thứ này khi thiếu là đúng, không phải hỏi thừa"
            )
    return notes


def _required_of(qualified: str) -> tuple[str, ...]:
    """The required parameters of `tool_id.action`, or nothing for a tool the registry does not know."""
    tool_id, _, name = qualified.partition(".")
    tool = TOOL_DEFINITIONS.get(tool_id)
    action = tool.action(name) if tool is not None else None
    if action is None:
        return ()
    schema = action.input_schema or {}
    return tuple(str(p) for p in (schema.get("required") or ()) if p in (schema.get("properties") or {}))


def _scripted_for(check: ExpectedCheck | None) -> ScriptedApprovals:
    """The decisions this case injects at the graph's `HumanApproval` nodes.

    Every eval run gets a table, including cases that name no node: the table's
    *presence* is what selects the third execution mode (ADR-0028 §1), and a case
    about something else entirely would otherwise stop dead at an approval it
    only passes through.

    Three answers, and the middle one is the whole of ADR-0034 §3.

    - `ApprovalCompliance` names one node and says what it decided there; every
      other gate defaults to `approved`, per ADR-0028 §1.
    - `ForbidsAction` says **no at every gate**. A `prohibitions` entry forbids
      the agent acting *of its own accord* — *"không được **tự ý** gửi thư"* —
      and a run that approved its way to the action would be reporting on a
      workflow doing exactly what it was designed to do. Refusing everywhere is
      what makes the question askable: whatever the workflow still does after
      being told no, it did unbidden.
    - Anything else approves throughout, which is what a case about something
      other than approvals wants from a gate it merely passes.
    """
    if isinstance(check, ApprovalCompliance):
        approved = check.decision is RunApprovalStatus.APPROVED
        return ScriptedApprovals(decisions={check.node_id: eval_decision(approved=approved)})
    if isinstance(check, ForbidsAction):
        return ScriptedApprovals(decisions={}, default=eval_decision(approved=False))
    return ScriptedApprovals(decisions={})


def _both_hold(judge: CheckVerdict, citations: CheckVerdict) -> CheckVerdict:
    """`source_groundedness`'s two checks, folded into the one verdict a case carries — #102.

    Neither may wave the other through: a judge that liked the answer does not
    excuse a citation nothing in the passages backs, and a real citation does not
    excuse a claim the passage never made — *"hai check trả lời hai câu hỏi khác
    nhau"*. Whichever one failed supplies the reason, so a creator reading the
    verdict learns which of the two questions it failed rather than only that it
    did; if both failed, both reasons travel together rather than one silently
    winning.
    """
    if judge.passed and citations.passed:
        return judge
    if not judge.passed and not citations.passed:
        return CheckVerdict(passed=False, reason=f"{judge.reason}; {citations.reason}")
    return judge if not judge.passed else citations


def _result(
    case: EvalCase,
    check: ExpectedCheck | None,
    record: _RunRecord,
    verdict: CheckVerdict,
    applicability: Applicability,
    *,
    metrics: Sequence[MetricScoreOut] = (),
) -> EvalCaseResult:
    """One case and its outcome, copied into the shape the run stores.

    Every field of the case is copied rather than referenced, for #13's reason:
    cases are editable and deletable, and a result pointing at a row would
    describe a test that never ran the moment somebody corrected it.

    `check` is the parsed rule rather than the row's raw payload, so what is
    stored on the result is what actually graded it — a row whose column was
    edited between the parse and the write would otherwise be recorded as the
    rule that ran.
    """
    return EvalCaseResult(
        case_id=case.id,
        suite_case_id=case.id,
        applicability=as_case_applicability(applicability),
        criterion=case.criterion,
        input_message=case.input_message,
        expected=case.expected,
        answer=record.answer,
        passed=verdict.passed,
        reason=verdict.reason,
        category=case.category,
        expected_check=check,
        safety_violation=verdict.safety_violation,
        metrics=list(metrics),
        evidence=[
            EvalEvidence(
                tool=call.tool,
                args=dict(call.args or {}),
                ok=call.ok,
                result=shown(call.result),
                simulated=call.simulated,
            )
            for call in tool_evidence(record.transcript)
        ],
    )


async def run_case_once(
    message: str,
    *,
    workflow: WorkflowJSON,
    agent_id: int,
    version_id: int,
    services: EvalServices,
    knowledge: KnowledgeStore,
    scripted: ScriptedApprovals,
) -> "_RunRecord":
    """One sandboxed run of `workflow`, exactly as an eval case gets one — the trial seam.

    One door, so every repeat of a case comes from the same execution engine,
    the same transcript mapper and the same scripted-approval mode. The
    scenario grid used to repeat a cell n times through here (ADR-0043 §2)
    before ADR-0065 retired it; the seam is still the right shape for anything
    that wants to run the same case twice.
    """
    subject = _Subject(
        workflow=workflow, agent_id=agent_id, version_id=version_id, services=services, knowledge=knowledge
    )
    return await _eval_run(message, subject, scripted=scripted)


async def _eval_run(message: str, subject: _Subject, *, scripted: ScriptedApprovals) -> _RunRecord:
    """One case's run, start to finish, kept as a transcript and an answer.

    The transcript is built with `src.workflow.sandbox.transcript_entry` — the
    trial's own mapper, not a second one — so what a rule reads and what an admin
    reads are the same lines. Anything a rule could be graded against but a
    transcript omits would be a verdict nobody could check by reading the record.

    The answer is the last node with anything to say. An `End` node's content is
    empty by construction (ADR-0021 gave it nothing to do), so "the last node"
    alone would hand every judge an empty string; the last *non-empty* one is the
    node that actually addressed the user, which in both demo graphs is the
    `AgentLlm` node before `End`.

    Traced like any other run (#11) under an identity of its own, on a fresh
    `TRIAL` thread per case.
    """
    services = subject.services
    thread = ThreadRef.new(subject.agent_id, namespace=ThreadNamespace.TRIAL)
    identity = RunIdentity.for_run(thread, subject.version_id)
    run_services = RunServices(model_factory=services.model_factory, trace=open_trace(services.tracer, identity))
    conversation = Conversation(thread=thread, checkpointer=services.checkpointer)

    transcript: list[SandboxTranscriptEntry] = []
    answer = ""
    error: str | None = None
    fault: PlatformFault | None = None
    cost: CostFigure | None = None
    try:
        async for event in stream_workflow_reply(
            subject.workflow,
            message,
            services=run_services,
            conversation=conversation,
            knowledge=subject.knowledge,
            sandbox=True,
            identity=identity,
            scripted_approvals=scripted,
        ):
            entry = transcript_entry(event)
            if entry is not None:
                transcript.append(entry)
            if event["event"] == "node" and event["data"]["content"]:
                answer = event["data"]["content"]
            elif event["event"] == BUDGET_EXCEEDED_EVENT:
                error = event["data"]["message"]
            elif event["event"] == COST_EVENT:
                # The measured price of the run, taken from the very event the
                # creator is shown so any future cap brakes on the same number
                # the screen reports (ADR-0063). Emitted on every leg, the crash
                # path included, so a trial that failed still carries its price.
                cost = CostFigure.from_payload(event["data"])
    except Exception as exc:  # noqa: BLE001 — a broken candidate is a failed case, not a failed eval
        # `stopped_reason` rather than a second copy of the same sentence: one
        # case that crashed must not cost the creator the rest of the set, and
        # that rule belongs to eval rather than to either runtime.
        error = stopped_reason(exc)
        # ADR-0061: a provider that refused the call is the one way a crash here
        # is the platform's. Everything else — a graph the runtime cannot
        # execute, a node that raised — stays a failed case, loudly.
        fault = fault_of_run_error(exc)

    return _RunRecord(transcript=transcript, answer=answer, error=error, fault=fault, cost=cost)
