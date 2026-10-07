"""Which template a confirmed spec should be built from — ADR-0025 §3–4.

Hybrid, and the split is the point. Matching *"tôi cần trợ lý trả lời khiếu nại
rồi gửi thư cho khách"* to a use-case description is judgement, and a fixed rule
set does it worse than a model. But two things about a proposal are structural,
and a model is not asked to be right about them:

- a template whose graph searches attached documents is useless to a spec that
  attached none;
- a spec whose actions really send or delete something cannot be built on a
  shape with nobody standing in front of the side effect (ADR-0023 §5).

Both would be caught later by the graph validator at confirm time. Catching them
here means the creator never receives a draft that could not have been
confirmed, which is a different thing from catching it: a rejected draft is work
they have to redo.

**Nothing here ever refuses to produce a workflow.** Requirement §2 (user story
#10) is explicit that an imperfect match is still a draft the creator fixes on
the canvas, so a model answering prose, an invented id, or a catalog where
nothing fits all end the same way — at `_closest`, with the best entry the rules
can find and, failing that, simply the first one. ADR-0022 §5 already promised
that a mid-edit graph is expected at this stage; an imperfect template match is
the same situation and needs no failure mode of its own.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from src.db.models import PlatformCallPurpose
from src.evals.model_call import ask
from src.evals.model_json import UnreadableModelReplyError, json_object
from src.models.solution_spec import AgentSolutionSpecification
from src.services.llm import ChatModelFactory, document_model
from src.workflow.effects import has_high_risk_action
from src.workflow.templates import TemplateSpec, approved_templates, template_of
from src.workflow.validation import approval_guards_every_mcp_path

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

#: How many times the model is asked before the choice falls to `_closest`. Two,
#: because the second ask exists to let it react to a rejection — a third would
#: only re-ask a model that has already had the one piece of information it was
#: missing, at the cost of another round trip in front of a waiting creator.
_ATTEMPTS = 2

_SELECTION_PROMPT = """\
Bạn là Workflow Generator của một nền tảng dựng agent AI. Bạn đang chọn một bộ khung workflow \
có sẵn cho bài toán nghiệp vụ dưới đây. Bạn **không** được tự vẽ workflow — chỉ chọn một trong \
các mẫu được liệt kê.

Bài toán nghiệp vụ:
{problem}

Các mẫu workflow được phép chọn:

{candidates}
{rejected}
Chọn đúng một mẫu hợp nhất với bài toán trên. Không mẫu nào hoàn hảo thì chọn mẫu gần nhất — \
người tạo sẽ tự sửa tiếp trên canvas.

Chỉ trả lời bằng một JSON object, không kèm giải thích:
{{"template_id": "..."}}
"""

_REJECTED_PREFIX = """
Những mẫu sau đã bị loại, **không được chọn lại**:
{rejected}
"""


async def select_template(
    *,
    spec: AgentSolutionSpecification,
    model_factory: ChatModelFactory,
    db: "Session | None" = None,
    attempts: int = _ATTEMPTS,
) -> TemplateSpec:
    """The template to build `spec` on. Always returns one.

    The model proposes; `rule_problems` may turn the proposal down, and the next
    round is told which ids are out of play. When every round is spent, the
    choice falls to `_closest` — which is the whole of ADR-0025 §4's "no
    template fits well → pick the closest anyway."

    A template and nothing else. How the choice was reached — which ids the
    rules turned down, whether the model named this one or the platform fell
    back to it — is deliberately not returned: nothing downstream reads it, and
    `WorkflowJSON.metadata.template_id` is the only record ADR-0022 §4 asked
    for. A screen that one day wants to say *"no template matched well"* is the
    ticket that should add the field it needs.

    Runs on `document_model()` for the reason ADR-0010 §1 gives: the models an
    admin configures are the ones a creator's *agent* runs on, and choosing the
    shape of somebody's business process is the platform's own call.

    `db` is optional (#53): the Generator always has a session by the time it
    calls this and passes it so `status: approved` creator submissions join the
    candidate pool alongside the built-in seed (ADR-0025 §6); a caller with no
    session — as every test before #53 was written — still gets the seed alone.
    """
    candidates = approved_templates(db)
    rejected: list[str] = []
    for _ in range(attempts):
        proposal = await _propose(
            spec=spec, candidates=candidates, rejected=rejected, model_factory=model_factory, db=db
        )
        if proposal is None:
            continue
        if not rule_problems(proposal, spec):
            return proposal
        rejected.append(proposal.id)
    return _closest(spec, candidates)


def rule_problems(template: TemplateSpec, spec: AgentSolutionSpecification) -> tuple[str, ...]:
    """Why `template` cannot be built for `spec` — empty when it can.

    The two structural invariants of ADR-0025 §3, and deliberately no more. This
    is not a scoring function: everything else about whether a template *suits*
    a problem is the model's judgement, and a rule pretending to that judgement
    would quietly overrule it.
    """
    problems: list[str] = []
    if template.required_data_kinds and not any(source.kind in template.required_data_kinds for source in spec.data):
        kinds = ", ".join(kind.value for kind in template.required_data_kinds)
        problems.append(f"mẫu {template.id!r} cần nguồn dữ liệu loại {kinds}, đặc tả không khai nguồn nào như vậy")
    if has_high_risk_action(spec.actions) and not approval_guards_every_mcp_path(template.skeleton()):
        problems.append(
            f"đặc tả có hành động gây hậu quả không thể hoàn tác, nhưng mẫu {template.id!r} "
            f"không bắt buộc qua bước phê duyệt trước khi hành động"
        )
    return tuple(problems)


async def _propose(
    *,
    spec: AgentSolutionSpecification,
    candidates: Sequence[TemplateSpec],
    rejected: Sequence[str],
    model_factory: ChatModelFactory,
    db: "Session | None" = None,
) -> TemplateSpec | None:
    """One round of asking the model, or `None` if it did not name a real template.

    Never raises, the same discipline `src.solution.designer` keeps: prose
    instead of JSON, or an id nobody defined, costs this round and nothing else.
    A parse error surfacing here would turn the platform's flakiness into a
    creator who cannot get a workflow at all.
    """
    offered = [template for template in candidates if template.id not in rejected]
    if not offered:
        return None
    reply = await ask(
        model_factory(document_model()),
        _SELECTION_PROMPT.format(
            problem=_problem_block(spec),
            candidates=_candidates_block(offered),
            rejected=_rejected_block(rejected),
        ),
        purpose=PlatformCallPurpose.SELECT_TEMPLATE,
    )
    try:
        document = json_object(reply)
    except UnreadableModelReplyError:
        return None
    proposed = document.get("template_id")
    if not isinstance(proposed, str):
        return None
    template = template_of(proposed.strip(), db)
    return template if template is not None and template.id not in rejected else None


def _closest(spec: AgentSolutionSpecification, candidates: Sequence[TemplateSpec]) -> TemplateSpec:
    """The best template to hand over when the model never named a usable one.

    Two rules, in this order.

    **Acceptable first**, because a "closest match" that cannot pass confirmation
    is not close to anything.

    **Then the least presumptuous of those**, which is the part that used to be
    wrong. This function ran when the platform has just failed to work out what
    the creator is building, and it answered by taking the first entry in
    registry order — which is a document-search shape. So every unresolved
    selection proposed the same opinionated graph, and the creator's first sight
    of their workflow was a step nobody asked for. Falling back to a template
    that demands no particular data source says the true thing instead: we do not
    know what this is yet, so here is a shape that assumes as little as possible
    and that you can build out.

    Registry order still decides between equals, and that is fine — it is a tie
    among templates that are equally undemanding, not a preference smuggled in as
    an ordering.

    `candidates` is never empty, and that is a property of the registry rather
    than something checked here: the built-in seed is code, and it ships at
    `approved` (ADR-0025 §6). A guard for the empty case would be a branch no
    state of the system can reach.
    """
    acceptable = [template for template in candidates if not rule_problems(template, spec)]
    if not acceptable:
        return candidates[0]
    return next(
        (template for template in acceptable if not template.required_data_kinds),
        acceptable[0],
    )


def _problem_block(spec: AgentSolutionSpecification) -> str:
    """The spec as the model reads it when choosing a shape.

    Four of the seven fields. `goal`, `actions` and `data` are what a shape has
    to fit; `prohibitions` is here because a thing the creator forbade is often
    the clearest signal that a workflow needs a person in it. `users`,
    `expected_outcome` and `success_criteria` are left out — they describe how
    well the agent did, which no choice of graph shape follows from.
    """
    data = "; ".join(f"{source.name} ({source.kind.value})" for source in spec.data)
    return "\n".join(
        [
            f"- Mục tiêu: {spec.goal}",
            f"- Dữ liệu agent đọc: {data}",
            f"- Hành động agent phải làm: {'; '.join(spec.actions)}",
            f"- Tuyệt đối không được: {'; '.join(spec.prohibitions)}",
        ]
    )


def _candidates_block(candidates: Sequence[TemplateSpec]) -> str:
    """Every template on offer, as its id, name and what it is for.

    The `description` carries the match, and `required_data_kinds` is shown
    alongside it so the model can see the constraint the rule layer will check
    rather than being turned down by a rule it was never told about.
    """
    return "\n".join(
        f"- {template.id} — {template.name}: {template.description}"
        + (
            f" (cần nguồn dữ liệu loại: {', '.join(kind.value for kind in template.required_data_kinds)})"
            if template.required_data_kinds
            else ""
        )
        for template in candidates
    )


def _rejected_block(rejected: Sequence[str]) -> str:
    """The ids already turned down, or nothing at all on the first round."""
    if not rejected:
        return ""
    return _REJECTED_PREFIX.format(rejected="\n".join(f"- {template_id}" for template_id in rejected))
