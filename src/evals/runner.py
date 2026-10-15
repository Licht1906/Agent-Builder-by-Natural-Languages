"""Running the test set: one sandboxed run per case, then one verdict per run.

The executing half of #13. Every case is a full, ordinary run of the ordinary
runtime with `sandbox=True` — the same flag, through the same
`stream_agent_reply`, that a trial run uses (#9 / ADR-0007). That is what makes
*"tool có tác dụng phụ không hề được gọi"* true here without eval knowing
anything about tools: there is no second execution path to keep in step, and a
side-effecting tool has no way to reach its real implementation from a run whose
flag is set.

Each case gets its **own** conversation. Cases are independent questions, and a
set that ran them down one thread would grade the third case on an agent that had
already been told the answer to the first — a test set that flatters the agent
the longer it gets.

A case whose run does not finish — a crash, or the run's own budget ceiling (#10)
— is a failure with the reason attached, and is never sent to the judge. There is
no answer to grade, and grading the fragment would score the agent on what it
managed to say before it was stopped.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.orm import Session

from src.admin.config import read_config
from src.agents.runtime import BUDGET_EXCEEDED_EVENT, Conversation, RunServices, stream_agent_reply
from src.agents.threads import RunIdentity, ThreadNamespace, ThreadRef
from src.config import get_settings
from src.db.models import EvalCase, EvalRun
from src.evals.applicability import Applicability, applicability_of, as_case_applicability, ungrounded_reason
from src.evals.judge import judge_answer
from src.evals.results import store_run
from src.models.agent_spec import AgentSpec
from src.models.schemas import EvalCaseResult, SkippedCase
from src.services.llm import ChatModelFactory
from src.services.tracing import Tracer, open_trace

#: Said about a case whose run stopped before it produced an answer. The reason
#: the runtime gave is appended — a run stopped by its token ceiling and one
#: stopped by a broken tool provider need very different fixes.
RUN_DID_NOT_FINISH = "Agent không chạy xong nên không có câu trả lời để chấm"


class EvalRunError(ValueError):
    """An eval run that cannot start, carrying the reason. Message is user-facing."""


def stopped_reason(exc: Exception) -> str:
    """Why a case's run died, as a sentence a creator can read.

    Shared with `src.evals.workflow_runner` because it is a rule about *eval* —
    one case that crashed must not cost the creator the verdicts on the rest of
    the set, so the exception becomes that case's reason — rather than about
    either runtime.

    The fallback exists because an exception raised with no message would leave
    an empty reason, and an empty reason reads as no failure at all.
    """
    return str(exc) or f"Lần chạy dừng vì {type(exc).__name__}"


@dataclass(frozen=True, slots=True)
class EvalServices:
    """Everything eval needs from outside itself, as one argument.

    The same three seams the chat and sandbox endpoints already inject, gathered
    here because an eval run is many runs and each needs all three. Passed in
    rather than reached for, so a test replaces the model factory — the judge's
    included — the way it replaces it everywhere else.
    """

    model_factory: ChatModelFactory
    tracer: Tracer
    checkpointer: BaseCheckpointSaver


@dataclass(frozen=True, slots=True)
class _Answer:
    """What one sandboxed run left behind: what the agent said, and why it stopped.

    `error` is `None` for a run that finished. Both halves travel together
    because the decision they feed is one decision — grade this, or fail it with
    a reason.
    """

    text: str
    error: str | None


async def run_eval(
    db: Session,
    *,
    agent_id: int,
    version_id: int,
    spec: AgentSpec,
    cases: Sequence[EvalCase],
    services: EvalServices,
) -> EvalRun:
    """Run every case against `spec` in sandbox mode, score the set, and store the run.

    Refuses an empty set rather than recording a perfect score against it. A run
    with no cases would compute `0/0`, and whatever that is defined to be it is
    not evidence — a version must not be able to clear the bar by having deleted
    every test.

    The threshold is read once, here, and stored with the run. Reading it per
    case would let an admin's edit land halfway through and produce a score that
    was never measured against any single bar.
    """
    if not cases:
        raise EvalRunError(
            "Chưa có test case nào để chạy — hãy sinh bộ test case từ tiêu chí thành công "
            "của spec, hoặc tự thêm case, rồi chạy lại"
        )
    threshold = read_config(db).eval_pass_threshold
    # A V1 case never carries `expected_check` (`EvalCase`'s own docstring), so
    # `workflow=None` never sends a case down `applicability_of`'s ungrounded
    # branch here — every case is either `live` or `orphan`, both of which run.
    success_criteria = frozenset(spec.slots.success_criteria)
    results: list[EvalCaseResult] = []
    skipped: list[SkippedCase] = []
    for case in cases:
        applicability = applicability_of(case, success_criteria=success_criteria, workflow=None)
        if applicability is Applicability.UNGROUNDED:
            skipped.append(SkippedCase(suite_case_id=case.id, reason=ungrounded_reason(case)))
            continue
        results.append(
            await _result_for(
                case,
                spec=spec,
                agent_id=agent_id,
                version_id=version_id,
                services=services,
                applicability=applicability,
            )
        )
    return store_run(
        db, version_id, threshold=threshold, results=results, skipped=skipped, judge_model=get_settings().judge_model
    )


async def _result_for(
    case: EvalCase,
    *,
    spec: AgentSpec,
    agent_id: int,
    version_id: int,
    services: EvalServices,
    applicability: Applicability,
) -> EvalCaseResult:
    """Run one case and grade it — or fail it with the reason its run stopped."""
    answer = await _sandboxed_answer(
        case.input_message, spec=spec, agent_id=agent_id, version_id=version_id, services=services
    )
    if answer.error is not None:
        return _result(
            case,
            answer=answer.text,
            passed=False,
            reason=f"{RUN_DID_NOT_FINISH}: {answer.error}",
            applicability=applicability,
        )
    verdict = await judge_answer(
        criterion=case.criterion,
        expected=case.expected,
        input_message=case.input_message,
        answer=answer.text,
        model_factory=services.model_factory,
    )
    return _result(case, answer=answer.text, passed=verdict.passed, reason=verdict.reason, applicability=applicability)


def _result(case: EvalCase, *, answer: str, passed: bool, reason: str, applicability: Applicability) -> EvalCaseResult:
    """One case and its outcome, copied into the shape the run stores."""
    return EvalCaseResult(
        case_id=case.id,
        suite_case_id=case.id,
        applicability=as_case_applicability(applicability),
        criterion=case.criterion,
        input_message=case.input_message,
        expected=case.expected,
        answer=answer,
        passed=passed,
        reason=reason,
    )


async def _sandboxed_answer(
    message: str,
    *,
    spec: AgentSpec,
    agent_id: int,
    version_id: int,
    services: EvalServices,
) -> _Answer:
    """One case's run, start to finish, and what the agent ended up saying.

    The answer is the **last** step's output, because that is the answer the
    agent gives: earlier steps are working notes that the pipeline itself
    consumes (ADR-0003), and grading their contents would score the agent on
    scratch work no user ever sees.

    The run is traced like any other (#11) — `open_trace` under an identity of
    its own — but its spend is deliberately **not** written to `run_costs`. An
    eval candidate is a genuine `sandbox=True` run of the version, so a row here
    would be picked up by `latest_trial_cost` and served as "the measured cost of
    this version", displacing the trial run the creator actually watched. #13 did
    not ask for eval spend, and buying it at the price of a wrong number on #11's
    cost screen is not a trade this ticket gets to make. See ADR-0011 §8.
    """
    thread = ThreadRef.new(agent_id, namespace=ThreadNamespace.TRIAL)
    identity = RunIdentity.for_run(thread, version_id)
    run_services = RunServices(model_factory=services.model_factory, trace=open_trace(services.tracer, identity))
    conversation = Conversation(thread=thread, checkpointer=services.checkpointer)

    answer = ""
    error: str | None = None
    try:
        async for event in stream_agent_reply(
            spec, message, services=run_services, conversation=conversation, sandbox=True
        ):
            if event["event"] == "step":
                answer = event["data"]["content"]
            elif event["event"] == BUDGET_EXCEEDED_EVENT:
                error = event["data"]["message"]
    except Exception as exc:  # noqa: BLE001 — a broken candidate is a failed case, not a failed eval
        # One case that crashed must not lose the creator the verdicts on the
        # other seven: the exception becomes this case's reason and the set
        # carries on.
        error = stopped_reason(exc)

    return _Answer(text=answer, error=error)
