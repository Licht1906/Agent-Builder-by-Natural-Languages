"""The test set: generated from the creator's own criteria, then owned by them.

Two halves of one idea, which is why they share a module. Generation turns each
sentence in `slots.success_criteria` into concrete cases; the editing half
exists because that turning is a **guess**, and #13 is explicit that a closed
loop in which a model invents the tests, grades them and blocks the creator with
them is not acceptable. A creator who cannot delete a case they consider wrong is
a creator held up by a machine's misreading of their own words.

Two rules keep generation honest:

- `criterion` is copied from the spec, never taken from the model's reply. It is
  the sentence a result gets explained in terms of, and a model that paraphrased
  it would be grading against wording the creator never wrote.
- a criterion whose reply cannot be read produces **nothing**, and is reported by
  name. Silence there would leave a criterion untested while the score implied
  everything had been covered — the one failure mode a pass rate cannot show.
"""

from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.datasources.access import asker_of
from src.db.models import EvalCase, EvalCaseOrigin, EvalSuite, PlatformCallPurpose
from src.evals.model_call import ask
from src.evals.model_json import UnreadableModelReplyError, json_array
from src.models.agent_spec import AgentSpec
from src.models.eval_check import ExpectedCheck, check_payload
from src.models.solution_spec import SuccessCriterionCategory
from src.services.llm import ChatModelFactory, document_model

#: MVP default name for the one suite an agent gets — ADR-0054 §1. The same
#: text the backfill migration (`b7e4a92f5c31`) gave every pre-existing agent,
#: so a suite reads the same whether it was created by that migration or by
#: the first case a creator ever adds.
DEFAULT_SUITE_NAME = "Bộ đề mặc định"

#: How many cases one criterion is asked for. Two rather than one because a
#: criterion is usually met in the easy case and broken in the awkward one, and
#: a set that only ever asks the easy question grades nothing worth grading.
CASES_PER_CRITERION = 2

#: The most that will be kept from one criterion however many the model returns.
#: Every case is a full sandboxed run of the agent plus a judging call, so a
#: model in a generous mood is a bill the creator never asked for.
MAX_CASES_PER_CRITERION = 3

_GENERATION_PROMPT = """\
Bạn đang viết test case để kiểm tra một agent AI.

Agent: {name}
Mô tả: {description}
Người dùng: {audience}
Dữ liệu đầu vào thường gặp: {inputs}

Tiêu chí thành công cần kiểm: "{criterion}"

Hãy viết {count} test case cho **riêng** tiêu chí này. Mỗi test case gồm:
- "input_message": một đầu vào cụ thể, viết như người dùng thật sẽ gửi;
- "expected": câu trả lời thế nào thì coi là đạt **đúng tiêu chí trên**, viết đủ \
rõ để người khác đọc và chấm được.

QUAN TRỌNG — "expected" chỉ được diễn đạt lại chính tiêu chí sao cho chấm được, \
**không được thêm yêu cầu mới**. Không tự thêm các đòi hỏi mà tiêu chí không nói, \
ví dụ: bắt trích dẫn URL từng số liệu, bắt nêu dấu thời gian, bắt trình bày theo \
một bố cục nhất định, hay bắt phải có thêm dữ liệu đối chiếu. Một tiêu chí một vế \
thì "expected" cũng một vế. Người tạo agent sẽ bị chấm trượt theo đúng những gì \
bạn viết ở đây, nên mỗi vế bạn thêm vào là một cách trượt mà họ chưa từng yêu cầu.

Ít nhất một case nên là tình huống khó hoặc thiếu thông tin, vì đó là chỗ tiêu \
chí dễ bị vi phạm nhất — nhưng vẫn chấm bằng đúng tiêu chí đó, không khắt khe hơn.

Chỉ trả lời bằng một mảng JSON, không kèm giải thích:
[{{"input_message": "...", "expected": "..."}}]
"""


class EvalCaseError(ValueError):
    """A test case that cannot be stored, carrying the reason. Message is user-facing."""


@dataclass(frozen=True, slots=True)
class GeneratedCase:
    """One case the generator proposed, before anybody has agreed to it.

    The last two fields are V2's (#48 / ADR-0028) and both default, because a V1
    case has neither: a criterion in `slots.success_criteria` is a bare sentence
    with no category, and the only way to grade one is to ask the judge about
    `expected`. `src.evals.workflow_cases` is what fills them in.
    """

    criterion: str
    input_message: str
    expected: str
    category: SuccessCriterionCategory | None = None
    #: The rule that grades this case instead of the judge, when there is one.
    #: `expected` is still filled on such a case — derived from this check
    #: (`describe_check`), so the sentence a creator reads is the rule that runs.
    check: ExpectedCheck | None = None


@dataclass(frozen=True, slots=True)
class Generation:
    """What one pass of the generator produced — and what it failed to produce.

    `skipped_criteria` is not an error list to be logged and forgotten. It names
    the part of the creator's own definition of "correct" that now has no test
    behind it, and the only person who can fix that is the creator — so it
    travels all the way out to them.
    """

    cases: tuple[GeneratedCase, ...]
    skipped_criteria: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SavedGeneration:
    """What `save_generated` actually wrote, and how much of `Generation.cases` it declined to.

    `duplicate_count` is ADR-0054 §1's other half of "sinh case không sinh lại":
    a criterion that already has a `generated` case in the suite is not
    reproposed, and the count travels out the same way `skipped_criteria`
    does — a number the creator can read as "nothing new here", not a silent
    no-op that looks identical to "the model wrote zero cases".
    """

    cases: tuple[EvalCase, ...]
    duplicate_count: int


async def generate_cases(spec: AgentSpec, *, model_factory: ChatModelFactory) -> Generation:
    """Propose test cases for every success criterion in `spec`.

    Runs on the meta-agent's model, not on any model named in the spec: writing
    the tests is the platform's job, while a step's model is the creator's choice
    — including, legitimately, a cheap one. A test set is worth no more than the
    model that wrote it.

    One call per criterion rather than one call for the whole spec. A single call
    would have to be trusted to say which criterion each case belongs to, and a
    mislabelled case tests nothing while appearing to test something. Here the
    label is ours by construction.

    Sequential, deliberately: the calls are few, and a deterministic order is
    what lets the whole path be verified against scripted models (#13's last
    criterion).
    """
    model = model_factory(document_model())
    cases: list[GeneratedCase] = []
    skipped: list[str] = []
    for criterion in spec.slots.success_criteria:
        proposed = await _cases_for_criterion(model, spec, criterion)
        if proposed:
            cases.extend(proposed)
        else:
            skipped.append(criterion)
    return Generation(cases=tuple(cases), skipped_criteria=tuple(skipped))


async def _cases_for_criterion(model: BaseChatModel, spec: AgentSpec, criterion: str) -> list[GeneratedCase]:
    """The usable cases for one criterion — empty if the model's reply was not usable.

    Empty rather than an exception, because one criterion the model fumbled must
    not cost the creator the cases it wrote for the other five. The caller turns
    an empty result into a named, reported gap.
    """
    reply = await ask(
        model,
        _GENERATION_PROMPT.format(
            name=spec.name,
            description=spec.description,
            audience=spec.slots.audience,
            inputs=spec.slots.inputs,
            criterion=criterion,
            count=CASES_PER_CRITERION,
        ),
        purpose=PlatformCallPurpose.EVAL_CASES,
    )
    try:
        proposed = json_array(reply)
    except UnreadableModelReplyError:
        return []
    usable = (_case_from(entry, criterion) for entry in proposed)
    return [case for case in usable if case is not None][:MAX_CASES_PER_CRITERION]


def _case_from(entry: object, criterion: str) -> GeneratedCase | None:
    """One entry of the model's array as a case, or `None` if it is not one.

    A case missing either half is dropped rather than stored blank. An empty
    input is not a test, and an empty expectation is a case the judge would have
    to invent a standard for — which is the closed loop this ticket refuses.
    """
    if not isinstance(entry, dict):
        return None
    input_message = str(entry.get("input_message", "")).strip()
    expected = str(entry.get("expected", "")).strip()
    if not input_message or not expected:
        return None
    return GeneratedCase(criterion=criterion, input_message=input_message, expected=expected)


def get_suite(db: Session, agent_id: int) -> EvalSuite | None:
    """`agent_id`'s suite, or `None` if it has never had a case. A plain read — never creates one.

    Kept separate from `get_or_create_suite` so a GET (`list_cases`) never
    writes: an agent nobody has generated or added a case for has no suite and
    no cases, and the two facts should stay in lockstep without a request that
    only *reads* being the thing that makes them not.
    """
    return db.execute(select(EvalSuite).where(EvalSuite.agent_id == agent_id)).scalar_one_or_none()


def get_or_create_suite(db: Session, agent_id: int) -> EvalSuite:
    """`agent_id`'s suite, creating the MVP one-per-agent row the first time anything is written to it.

    ADR-0054 §1's "tạo lười lần đầu chạm": every write path (generate, add,
    promote, and the importer EVAL-2 adds) calls this rather than assuming the
    row exists, so an agent's first test case is also the moment its suite is
    born.
    """
    suite = get_suite(db, agent_id)
    if suite is not None:
        return suite
    suite = EvalSuite(agent_id=agent_id, name=DEFAULT_SUITE_NAME)
    db.add(suite)
    db.commit()
    db.refresh(suite)
    return suite


def save_generated(db: Session, agent_id: int, version_id: int, generation: Generation) -> SavedGeneration:
    """Store `generation`'s cases in `agent_id`'s suite, marked as machine-written.

    Appends rather than replaces, and skips a proposed case whose criterion
    already has a `generated` case in the suite — ADR-0054 §1's "không sinh lại
    cho tiêu chí đã có case generated". Both are the same discipline #13 always
    had, restated for a suite that now survives across versions: a creator who
    has already corrected or kept a generated case must not lose that work, or
    see it quietly duplicated, by pressing generate again on a later version.
    """
    suite = get_or_create_suite(db, agent_id)
    already_generated = set(
        db.execute(
            select(EvalCase.criterion).where(
                EvalCase.suite_id == suite.id, EvalCase.origin == EvalCaseOrigin.GENERATED
            )
        ).scalars()
    )
    kept = [case for case in generation.cases if case.criterion not in already_generated]
    rows = [
        EvalCase(
            suite_id=suite.id,
            introduced_in_version_id=version_id,
            criterion=case.criterion,
            input_message=case.input_message,
            expected=case.expected,
            origin=EvalCaseOrigin.GENERATED,
            category=case.category,
            expected_check=None if case.check is None else check_payload(case.check),
        )
        for case in kept
    ]
    db.add_all(rows)
    db.commit()
    for row in rows:
        db.refresh(row)
    return SavedGeneration(cases=tuple(rows), duplicate_count=len(generation.cases) - len(kept))


def list_cases(db: Session, agent_id: int) -> list[EvalCase]:
    """Every test case of `agent_id`'s suite, oldest first — the set the next run will use.

    Keyed by agent since ADR-0054 §1, not by version: a version created after
    the suite already has cases sees all of them immediately, with nothing to
    regenerate.

    **One list, no exceptions**, since ADR-0065 retired the scenario grid. This
    function used to take `include_grid=False` so the ordinary run could hold
    the two measuring regimes apart (ADR-0054 §3, "hai con số, hai tập, không
    trộn"). There is only one regime now, so a row left behind with
    `origin=grid` is an ordinary row: it is listed, it is run against
    `eval_pass_threshold` like every other, and the creator can delete it. A
    filter that hid rows a creator can no longer produce would only make part
    of their own suite invisible.
    """
    suite = get_suite(db, agent_id)
    if suite is None:
        return []
    return list(
        db.execute(
            select(EvalCase)
            .where(EvalCase.suite_id == suite.id)
            .order_by(EvalCase.created_at, EvalCase.id)
        ).scalars()
    )


def add_case(
    db: Session,
    agent_id: int,
    version_id: int,
    *,
    criterion: str,
    input_message: str,
    expected: str,
    run_as: str | None = None,
) -> EvalCase:
    """A case the creator wrote themselves, into `agent_id`'s suite.

    Marked `MANUAL`, which is what stops a later generate from being read as
    having produced it — and what makes "the creator can add cases" a fact in the
    data rather than a claim in a docstring. `version_id` is kept only as
    `introduced_in_version_id` provenance (ADR-0054 §1) — which version the
    creator was looking at when they wrote it, not what the case is scoped to.

    `run_as` is who asks the question (DS-05) — blank is nobody.
    """
    suite = get_or_create_suite(db, agent_id)
    case = EvalCase(
        suite_id=suite.id,
        introduced_in_version_id=version_id,
        criterion=_required(criterion, "tiêu chí"),
        input_message=_required(input_message, "đầu vào"),
        expected=_required(expected, "kỳ vọng"),
        origin=EvalCaseOrigin.MANUAL,
        run_as=asker_of(run_as),
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


def update_case(
    db: Session,
    case: EvalCase,
    *,
    criterion: str | None = None,
    input_message: str | None = None,
    expected: str | None = None,
    run_as: str | None = None,
) -> EvalCase:
    """Change whichever fields were named. `None` means "leave this one alone".

    The origin is left as it was, on purpose. A corrected generated case is still
    a case the machine proposed and a person fixed, and that history is exactly
    what an admin reading the approval dossier is entitled to see.
    """
    if criterion is not None:
        case.criterion = _required(criterion, "tiêu chí")
    if input_message is not None:
        case.input_message = _required(input_message, "đầu vào")
    if expected is not None:
        case.expected = _required(expected, "kỳ vọng")
    if run_as is not None:
        # Blank clears it — `None` here means "leave alone", so blank is how a
        # creator says "nobody is asking" (DS-05).
        case.run_as = asker_of(run_as)
    db.commit()
    db.refresh(case)
    return case


def delete_case(db: Session, case: EvalCase) -> None:
    """Throw a case away. Runs already scored against it keep their own copy of it."""
    db.delete(case)
    db.commit()


def _required(value: str, field: str) -> str:
    """`value` stripped, or the reason it cannot be stored.

    Here rather than only on the request schema, for the same reason the config
    rules live in `src.admin.config`: "an empty input is not a test" is a fact
    about eval, and a second caller deserves the same refusal in the same words.
    """
    cleaned = value.strip()
    if not cleaned:
        raise EvalCaseError(f"Test case phải có {field}")
    return cleaned
