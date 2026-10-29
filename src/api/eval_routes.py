"""Eval endpoints — the creator's side of the quality bar. See #13 / ADR-0011.

Its own module rather than more of `agent_routes` for the reason `admin_routes`
is its own: the shape of the thing is different. These endpoints are about a test
set and its scores, not about an agent, and eight more handlers in a file that is
already the platform's busiest is how a reader stops being able to find anything.

Everything here sits behind `OwnedVersion`, so a test case, a run and a score are
only ever reachable through an agent the caller owns — declared once as a type
rather than checked once per handler.

The order the endpoints are declared in is the order a creator meets them:
generate a set from the spec's own criteria, correct it, run it, read the score.
That middle step is not a convenience. It is the reason this ticket exists in the
form it does — a creator blocked by cases they consider wrong, with no way to fix
them, is worse off than one with no eval at all.

**Two doors generate and two doors run** since #48. The V1 pair reads
`AgentVersion.spec`; the V2 pair reads `solution_spec` *and* the confirmed
`workflow`, because ADR-0028's rule-based checks are grounded against the graph
that will actually execute. A single endpoint choosing between them by looking at
which column happened to be filled is exactly what
`src.api.workflow_run_routes` refused to do for the same reason: a version
holding both documents would make the guess wrong silently.

The three doors in the middle — list, correct, delete — are **shared**, and that
is not an oversight. A V1 and a V2 case are rows in one table with one meaning,
and a creator correcting a case is doing the same thing to both. What a creator
cannot edit through them is `expected_check`: a rule is a machine-checkable fact
resolved against the graph, and hand-editing one is how a check ends up naming an
action nothing can call. Deleting such a case works as it always has.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from src.api.agent_routes import Checkpointer, OwnedAgent, OwnedVersion, validated_spec
from src.api.workflow_run_routes import Databases, Knowledge, runnable_workflow
from src.db.base import get_db
from src.db.models import EvalCase, EvalRun
from src.evals.cases import (
    EvalCaseError,
    add_case,
    delete_case,
    generate_cases,
    get_suite,
    list_cases,
    save_generated,
    update_case,
)
from src.evals.gate import score_for, version_blocker
from src.evals.results import newest_run, results_of, skipped_of
from src.evals.runner import EvalRunError, EvalServices, run_eval
from src.evals.sample_suites import case_count, sample_suite_of_agent
from src.evals.suite_io import SuiteImportError, export_suite, import_suite, template_csv
from src.evals.workflow_cases import generate_workflow_cases
from src.evals.workflow_runner import execute_workflow_eval, plan_workflow_eval
from src.models.schemas import (
    EvalCaseCreateRequest,
    EvalCaseResponse,
    EvalCaseUpdateRequest,
    EvalGenerationResponse,
    EvalRunRequest,
    EvalRunResponse,
    SampleSuiteResponse,
    SuiteImportResponse,
    SuiteImportRowResponse,
)
from src.services.llm import ChatModelFactory, get_chat_model_factory
from src.services.tracing import Tracer, get_tracer
from src.solution.specs import SpecStageError, solution_spec_of

_MEDIA_TYPES = {"csv": "text/csv", "jsonl": "application/x-ndjson"}

router = APIRouter(prefix="/agents", tags=["eval"])

#: Where a version's eval lives. Spelled once so the eight handlers below cannot
#: drift into two shapes of the same URL.
_VERSION = "/{agent_id}/versions/{version_id}"


def get_eval_services(
    model_factory: Annotated[ChatModelFactory, Depends(get_chat_model_factory)],
    tracer: Annotated[Tracer, Depends(get_tracer)],
    checkpointer: Checkpointer,
) -> EvalServices:
    """FastAPI dependency: the three seams an eval run talks to the world through.

    The same `get_chat_model_factory` the runtime uses, which is what puts the
    judge behind a seam a test can replace — "kể cả model chấm điểm" is a
    property of this wiring, not a promise in a docstring.
    """
    return EvalServices(model_factory=model_factory, tracer=tracer, checkpointer=checkpointer)


EvalRunServices = Annotated[EvalServices, Depends(get_eval_services)]


# --------------------------------------------------------------------------- #
# ADR-0054 §2 — a creator's own file in, a file back out. Agent-scoped rather
# than version-scoped like the rest of this router: the suite belongs to the
# agent (ADR-0054 §1), and grounding a rule-based row is done against the
# agent's latest version rather than asking the creator to pick one.
# --------------------------------------------------------------------------- #


@router.post("/{agent_id}/eval-suite/import", response_model=SuiteImportResponse)
async def import_my_agents_eval_suite(
    agent: OwnedAgent,
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="CSV, JSONL hoặc XLSX — cùng một schema")],
    dry_run: Annotated[bool, Query(description="true = chỉ kiểm, không ghi gì xuống DB")] = False,
) -> SuiteImportResponse:
    """Nạp một file bộ đề vào suite của agent — báo cáo từng dòng, ADR-0054 §2.

    422 chỉ cho lỗi của **cả file** (định dạng không đọc được, quá 500 dòng).
    Mọi lỗi ở mức dòng nằm trong báo cáo trả về với status 200 — dòng lỗi không
    được phép làm hỏng cách đọc các dòng khác trong cùng file.
    """
    content = await file.read()
    try:
        report = import_suite(db, agent, content=content, filename=file.filename or "", dry_run=dry_run)
    except SuiteImportError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return SuiteImportResponse(
        rows=[SuiteImportRowResponse(row=r.row, status=r.status, reason=r.reason) for r in report.rows],
        added_count=report.added_count,
        skipped_count=report.skipped_count,
        error_count=report.error_count,
        dry_run=report.dry_run,
    )


@router.get("/{agent_id}/eval-suite/sample", response_model=SampleSuiteResponse)
def my_agents_sample_suite(agent: OwnedAgent, db: Annotated[Session, Depends(get_db)]) -> SampleSuiteResponse:
    """Whether the agent's template ships a sample suite (DS-22) — what the Evaluation tab offers to load."""
    sample = sample_suite_of_agent(db, agent)
    if sample is None:
        return SampleSuiteResponse(available=False)
    return SampleSuiteResponse(available=True, name=sample.name, case_count=case_count(sample))


@router.post("/{agent_id}/eval-suite/sample", response_model=SuiteImportResponse)
def import_my_agents_sample_suite(
    agent: OwnedAgent,
    db: Annotated[Session, Depends(get_db)],
    dry_run: Annotated[bool, Query(description="true = chỉ kiểm, không ghi gì xuống DB")] = False,
) -> SuiteImportResponse:
    """Load the template's sample suite through the ordinary importer — the same row report, the same dedup.

    404 when the agent's template ships none. Loading twice adds nothing the
    second time: each row is a duplicate `(input, criterion)` and is skipped.
    """
    sample = sample_suite_of_agent(db, agent)
    if sample is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template của agent này không có bộ đề mẫu")
    report = import_suite(db, agent, content=sample.content(), filename=sample.path.name, dry_run=dry_run)
    return SuiteImportResponse(
        rows=[SuiteImportRowResponse(row=r.row, status=r.status, reason=r.reason) for r in report.rows],
        added_count=report.added_count,
        skipped_count=report.skipped_count,
        error_count=report.error_count,
        dry_run=report.dry_run,
    )


@router.get("/{agent_id}/eval-suite/export")
def export_my_agents_eval_suite(
    agent: OwnedAgent,
    db: Annotated[Session, Depends(get_db)],
    format: Annotated[Literal["jsonl", "csv"], Query(description="Định dạng file xuất ra")] = "jsonl",
) -> Response:
    """Toàn bộ suite của agent, ở đúng schema mà `import` đọc lại được — round-trip, ADR-0054 §2."""
    cases = list_cases(db, agent.id)
    content = export_suite(cases, format)
    return Response(
        content=content,
        media_type=_MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="eval_suite.{format}"'},
    )


@router.get("/{agent_id}/eval-suite/template.csv")
def eval_suite_template(agent: OwnedAgent) -> Response:
    """Một file CSV mẫu — header + ba dòng ví dụ — điểm khởi đầu cho người tạo tự viết bộ đề."""
    return Response(
        content=template_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="eval_suite_template.csv"'},
    )


@router.post(f"{_VERSION}/eval-cases/generate", response_model=EvalGenerationResponse)
async def generate_my_versions_eval_cases(
    version: OwnedVersion,
    db: Annotated[Session, Depends(get_db)],
    model_factory: Annotated[ChatModelFactory, Depends(get_chat_model_factory)],
) -> EvalGenerationResponse:
    """Write test cases from the version's own success criteria, and store them as drafts.

    Appends to whatever is already there, so pressing this twice never destroys
    a case the creator has corrected by hand.

    A spec with no `success_criteria` produces nothing and says so through an
    empty `cases`, rather than failing: the interview is where criteria come
    from, and an agent built before that slot was filled is a gap to report to
    the creator, not an error to throw at them.
    """
    spec = validated_spec(version, subject="Version")
    generation = await generate_cases(spec, model_factory=model_factory)
    saved = save_generated(db, version.agent_id, version.id, generation)
    return EvalGenerationResponse(
        cases=[EvalCaseResponse.model_validate(case) for case in saved.cases],
        skipped_criteria=list(generation.skipped_criteria),
        duplicate_count=saved.duplicate_count,
    )


@router.post(f"{_VERSION}/workflow-eval-cases/generate", response_model=EvalGenerationResponse)
async def generate_my_versions_workflow_eval_cases(
    version: OwnedVersion,
    db: Annotated[Session, Depends(get_db)],
    model_factory: Annotated[ChatModelFactory, Depends(get_chat_model_factory)],
) -> EvalGenerationResponse:
    """Write test cases from a confirmed workflow's spec **and** its graph — #48 / ADR-0028.

    Two documents rather than one, and that is the whole reason this is a second
    door. `success_criteria` says what "đúng" means and carries the `category`
    that routes each criterion to a judge or to a rule; the confirmed
    `WorkflowJSON` is what a rule gets grounded against, so a prohibition becomes
    the name of an action this graph can really invoke rather than a sentence
    nobody can check.

    The graph is fetched through `runnable_workflow`, the same three refusals the
    chat and trial doors use: no workflow, a stage that has not confirmed one, or
    a graph the runtime cannot execute. Generating against a document still being
    edited on the canvas would stamp cases with node ids that may not survive the
    next save.

    Appends, like V1's door: pressing it twice never destroys a case the creator
    corrected by hand. A criterion the model could not ground produces nothing and
    comes back named in `skipped_criteria` — the hole a pass rate cannot show.
    """
    workflow = runnable_workflow(version)
    try:
        spec = solution_spec_of(version)
    except SpecStageError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    generation = await generate_workflow_cases(spec, workflow, model_factory=model_factory)
    saved = save_generated(db, version.agent_id, version.id, generation)
    return EvalGenerationResponse(
        cases=[EvalCaseResponse.model_validate(case) for case in saved.cases],
        skipped_criteria=list(generation.skipped_criteria),
        duplicate_count=saved.duplicate_count,
    )


@router.get(f"{_VERSION}/eval-cases", response_model=list[EvalCaseResponse])
def list_my_versions_eval_cases(
    version: OwnedVersion, db: Annotated[Session, Depends(get_db)]
) -> list[EvalCaseResponse]:
    """Every test case of this version's agent suite, oldest first — exactly the set the next run will use."""
    return [EvalCaseResponse.model_validate(case) for case in list_cases(db, version.agent_id)]


@router.post(f"{_VERSION}/eval-cases", response_model=EvalCaseResponse, status_code=status.HTTP_201_CREATED)
def add_my_versions_eval_case(
    version: OwnedVersion,
    payload: EvalCaseCreateRequest,
    db: Annotated[Session, Depends(get_db)],
) -> EvalCaseResponse:
    """A case the creator writes themselves — for what the generator missed or got wrong."""
    try:
        case = add_case(
            db,
            version.agent_id,
            version.id,
            criterion=payload.criterion,
            input_message=payload.input_message,
            expected=payload.expected,
            run_as=payload.run_as,
        )
    except EvalCaseError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return EvalCaseResponse.model_validate(case)


def _get_owned_case(case_id: int, version: OwnedVersion, db: Annotated[Session, Depends(get_db)]) -> EvalCase:
    """The case at `case_id` if it belongs to this version's **agent** suite, otherwise 404.

    Scoped through `OwnedVersion` rather than fetched by id alone: a case id is
    guessable, and "this case exists but is not yours" is the same leak
    `get_owned_agent` refuses to make. Since ADR-0054 §1 a case is scoped to the
    agent's suite rather than to one version, so the check reads the suite
    rather than comparing `version.id` directly — any version of this agent
    reaches the same case.
    """
    case = db.get(EvalCase, case_id)
    suite = get_suite(db, version.agent_id)
    if case is None or suite is None or case.suite_id != suite.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy test case")
    return case


OwnedCase = Annotated[EvalCase, Depends(_get_owned_case)]


@router.patch(f"{_VERSION}/eval-cases/{{case_id}}", response_model=EvalCaseResponse)
def change_my_versions_eval_case(
    case: OwnedCase,
    payload: EvalCaseUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
) -> EvalCaseResponse:
    """Correct a case before it is ever run against the agent.

    The whole point of the ticket in one endpoint: a generated case is a
    machine's reading of the creator's own sentence, and the creator gets the
    last word on it. Omitted fields are left alone, so fixing an input does not
    require restating the expectation.
    """
    try:
        edited = update_case(
            db,
            case,
            criterion=payload.criterion,
            input_message=payload.input_message,
            expected=payload.expected,
            run_as=payload.run_as,
        )
    except EvalCaseError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return EvalCaseResponse.model_validate(edited)


@router.delete(f"{_VERSION}/eval-cases/{{case_id}}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_versions_eval_case(case: OwnedCase, db: Annotated[Session, Depends(get_db)]) -> None:
    """Throw a case away.

    Runs already scored against it are untouched: they hold their own copy of
    what they ran (`eval_runs.results`), so deleting a case rewrites no history.
    """
    delete_case(db, case)


@router.post(f"{_VERSION}/eval-runs", response_model=EvalRunResponse)
async def run_my_versions_eval(
    version: OwnedVersion,
    db: Annotated[Session, Depends(get_db)],
    services: EvalRunServices,
    payload: EvalRunRequest | None = None,
) -> EvalRunResponse:
    """Run the whole test set in sandbox mode and score it.

    Not a stream, unlike a chat or a trial run. What a creator is waiting for
    here is the verdict on a *set*, and a per-case stream would invite a client
    to render a running tally that the last case can still overturn.

    An empty set is a 409 rather than a perfect score: a version must not clear
    the bar by having no tests. Any other failure inside a single case stays
    inside that case — it fails, with the reason, and the rest of the set runs.
    """
    spec = validated_spec(version, subject="Version")
    try:
        run = await run_eval(
            db,
            agent_id=version.agent_id,
            version_id=version.id,
            spec=spec,
            cases=_selected(list_cases(db, version.agent_id), payload),
            services=services,
        )
    except EvalRunError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return eval_run_response(db, run)


@router.post(f"{_VERSION}/workflow-eval-runs", response_model=EvalRunResponse)
async def run_my_versions_workflow_eval(
    version: OwnedVersion,
    db: Annotated[Session, Depends(get_db)],
    services: EvalRunServices,
    knowledge: Knowledge,
    databases: Databases,
    background: BackgroundTasks,
    payload: EvalRunRequest | None = None,
) -> EvalRunResponse:
    """Start the whole test set against this version's confirmed workflow — answered at once, run in the background.

    EV-01 / ADR-0069 §7: a hundred cases are a hundred agent runs, half an hour
    one at a time and minutes eight at a time — either way longer than a
    request should stay open. So this answers with the run `running`, 0 of N,
    and `GET …/eval-runs/{run_id}` reports `done_count` until it is
    `completed` (or `failed`, with `error`). The creator may leave the screen;
    the run does not depend on anybody waiting for it.

    Every case is a sandboxed run of the real graph, and a case carrying an
    `expected_check` is graded by reading that run's transcript rather than by
    asking a model anything (ADR-0028 §2). A `human_approval_compliance` case
    additionally supplies its own decision at the named node, through eval's own
    execution mode — the runtime never pauses here, because there is nobody to
    resume it (ADR-0028 §1).

    `knowledge` is the same dependency the workflow chat and trial doors take, so
    a `KnowledgeSearch` node in an eval run reads the creator's real documents
    rather than a stand-in. A test set grounded in a stand-in would be evidence
    about the stand-in. Like the streaming doors, the job keeps using it after
    the request's session has closed.

    The two refusals are the ones the V1 door already gives — a graph that cannot
    run, and an empty set — and neither is a 422: both are facts about the version
    rather than about this request. Both are answered before anything runs.
    """
    workflow = runnable_workflow(version)
    try:
        solution_spec = solution_spec_of(version)
    except SpecStageError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    try:
        plan = plan_workflow_eval(
            db,
            version_id=version.id,
            workflow=workflow,
            cases=_selected(list_cases(db, version.agent_id), payload),
            success_criteria=frozenset(criterion.text for criterion in solution_spec.success_criteria),
        )
    except EvalRunError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    background.add_task(
        execute_workflow_eval,
        plan,
        agent_id=version.agent_id,
        version_id=version.id,
        workflow=workflow,
        services=services,
        knowledge=knowledge,
        databases=databases,
    )
    run = db.get(EvalRun, plan.run_id)
    assert run is not None
    return eval_run_response(db, run)


def _selected(cases: list, payload: "EvalRunRequest | None") -> list:
    """The requested subset of the set, or all of it — see `EvalRunRequest`."""
    if payload is None or payload.case_ids is None:
        return cases
    wanted = set(payload.case_ids)
    return [case for case in cases if case.id in wanted]


@router.get(f"{_VERSION}/eval-runs/latest", response_model=EvalRunResponse)
def read_my_versions_latest_eval_run(version: OwnedVersion, db: Annotated[Session, Depends(get_db)]) -> EvalRunResponse:
    """The run the Evaluation tab opens on: one still going if there is one, otherwise the last that finished.

    Declared before the by-id route because `latest` is not an integer and would
    otherwise be matched — and rejected — as one.

    Not necessarily the run the gate reads (EV-01): a creator who left the
    screen mid-run comes back to its progress, while `blocked_reason` keeps
    describing the version by its last *completed* run, as the gate does. A run
    that stopped as a whole is shown only until the next one starts.

    404 for a version nobody has evaluated. Unlike an agent with no costs, there
    is no true zero to report here: "no run" and "a run that scored nothing" are
    different states, and the approval gate treats them differently too.
    """
    run = newest_run(db, version.id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version chưa chạy eval lần nào")
    return eval_run_response(db, run)


@router.get(f"{_VERSION}/eval-runs/{{run_id}}", response_model=EvalRunResponse)
def read_my_versions_eval_run(
    run_id: int, version: OwnedVersion, db: Annotated[Session, Depends(get_db)]
) -> EvalRunResponse:
    """One recorded eval run, read back in full — what an approval request attaches.

    Scored against the bar in force *now*, not the one it ran under, because the
    question anyone reading it is asking is whether this version may go forward
    today. What it faced on the day is `threshold_at_run`.
    """
    run = db.get(EvalRun, run_id)
    if run is None or run.version_id != version.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy lần chạy eval")
    return eval_run_response(db, run)


def eval_run_response(db: Session, run: EvalRun) -> EvalRunResponse:
    """One stored run, plus every number that comes from comparing it to the live bar.

    Two different questions, deliberately answered from two different scores.
    The numbers (`pass_rate`, `meets_threshold`, `gap`, `cases_short`) describe
    **this** run against the bar in force now. `blocked_reason` describes **the
    version**, because "may I ask for approval" is not a question about the run
    being displayed — the gate only ever consults the latest run.

    They coincide on the latest run and diverge on an older one, and the
    divergence is the whole point: without it, opening a superseded run that
    happened to pass would show a green light above a button that refuses.
    Answered by `src.evals.gate`, which is the same code the gate itself runs —
    including, since #48, the rule-based refusal no `eval_override` can waive, so
    a creator reads *that* sentence here rather than a score that is beside the
    point.

    Public because the approval dossier (#14) attaches the same run and must
    describe it in the same words. A second rendering there would be a second
    opinion about whether a version clears the bar, shown to the one person whose
    job is to decide.
    """
    score = score_for(db, run)
    return EvalRunResponse(
        id=run.id,
        version_id=run.version_id,
        passed_count=run.passed_count,
        total_count=run.total_count,
        pass_rate=score.pass_rate,
        threshold=score.threshold,
        threshold_at_run=run.threshold,
        meets_threshold=score.meets_threshold,
        gap=score.gap,
        cases_short=score.cases_short,
        blocked_reason=version_blocker(db, run.version_id),
        created_at=run.created_at,
        results=results_of(run),
        skipped=skipped_of(run),
        judge_model=run.judge_model,
        status=run.status,
        done_count=run.done_count,
        error=run.error,
    )
