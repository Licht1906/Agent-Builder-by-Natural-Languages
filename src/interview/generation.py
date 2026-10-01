"""The meta-agent's second job: a finished sheet in, a valid `AgentSpec` out — see #17.

Where the declarative-spec architecture earns its keep. The model here writes
*data*, and the data has a schema with the platform's safety rules already in it
(ADR-0001), so the whole space of things this call can get wrong is the space of
things `AgentSpec` refuses. That is the trade the architecture was chosen for: an
LLM that fails in a way we **catch and report**, rather than one that emits code
that runs and is subtly wrong.

Three things are therefore not asked of the model, because they are not its to
decide:

- **the slots**. `InterviewSlots` is copied from the interview, never taken from
  the reply. It is what eval grades against (ADR-0011), and a paraphrase would
  move the standard away from the sentence the creator actually wrote.
- **the guardrails**. Every prohibition becomes a guardrail here, by construction.
  *"Điều-không-được-làm xuất hiện thành guardrails"* is then a property of the
  code rather than a hope about a prompt that was obeyed.
- **the model allowlist**. Validation runs against the allowlist *in force*
  (`src.admin.config`), so a draft cannot be saved naming a model the platform
  would refuse the moment anybody ran it.

Everything else — the steps, their prompts, which tool each one gets, which model
each one runs on, the loop ceilings — is the model's work, checked on the way in
and handed back to it with the reason when it fails.

`write_spec` is that whole arrangement — the call, the schema, the retry loop —
with the prompt left open. `src.interview.refinement` (#18) hands it a different
prompt and gets the same guarantees; nothing else in the platform asks a model
for a spec, and nothing else should.
"""

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy.orm import Session

from src.admin.config import read_config
from src.agents.tools import TOOL_REGISTRY
from src.agents.tools.registry import ToolRisk, ToolSpec
from src.agents.versions import create_draft
from src.db.models import Agent, AgentVersion, InterviewSession, PlatformCallPurpose
from src.evals.model_json import json_object
from src.interview.sessions import sheet_of
from src.models.agent_spec import SYSTEM_LIMITS, AgentSpec, InterviewSlots
from src.models.model_catalog import ASSIGNABLE_MODELS, ModelInfo, ModelTier
from src.services.llm import ChatModelFactory
from src.services.model_document import MAX_ATTEMPTS as MAX_ATTEMPTS
from src.services.model_document import write_document

#: `MAX_ATTEMPTS` is re-exported from `src.services.model_document` at the import
#: above rather than redefined here. The number lives with the loop it bounds now
#: that a workflow graph is written by the same loop, and callers that already
#: read it from this module — the refinement suite asserts the attempt budget
#: against it — go on seeing the one value.

#: What a creator is told when the attempts run out. Their next move, in their
#: words — the ticket asks for *"thông báo dễ hiểu"* and *"được thử lại"*, and a
#: pydantic error dump is neither. The technical reason travels on
#: `SpecGenerationError.reason` for the logs, not on the screen.
_EXHAUSTED = (
    "Chưa dựng được agent từ bản phỏng vấn này sau {attempts} lần thử. "
    "Bạn thử lại giúp — nếu vẫn vậy, xem lại các slot ở màn hình phỏng vấn "
    "và viết rõ hơn phần agent phải làm gì, rồi thử lại."
)

#: Said when there is nothing to build an agent out of. Not a failed generation —
#: no number of attempts could succeed — so it is refused before the first call.
_NO_MODELS = (
    "Nền tảng hiện không cho phép model nào để gán cho các bước, nên chưa dựng được agent. "
    "Nhờ quản trị viên mở lại allowlist model rồi thử lại."
)

_GENERATION_PROMPT = """\
Bạn là meta-agent của một nền tảng dựng agent AI. Việc của bạn: biến bản phỏng vấn \
dưới đây thành một agent nhiều bước, viết ra dưới dạng JSON.

Người tạo mô tả nghiệp vụ của họ như sau:
\"\"\"
{description}
\"\"\"

Bản phỏng vấn đã điền đủ:
- Ai dùng agent: {audience}
- Dữ liệu đầu vào: {inputs}
- Agent phải làm được:
{capabilities}
- Thế nào là trả lời đúng:
{success_criteria}
- Tuyệt đối không được làm:
{prohibitions}

Model được phép gán cho từng bước:
{models}

Tool được phép, chọn theo đúng việc mà bước đó phải làm:
{tools}

Trần cứng của hệ thống — vượt là spec bị từ chối:
{limits}

Quy tắc bắt buộc:
- Chia nghiệp vụ thành từng bước, mỗi bước làm một việc, và viết prompt riêng cho từng bước.
- Gán model theo độ khó của bước: bước trích xuất hoặc phân loại thì dùng model rẻ, \
bước suy luận hoặc soạn văn bản thì dùng model mạnh.
- "max_iterations" là trần vòng lặp của bước: bước không có tool để 1, bước có tool để vừa đủ.
{schema_rules}

Chỉ trả lời bằng một JSON object đúng khuôn dưới đây, không kèm giải thích:
{skeleton}
"""

#: The rules that are `AgentSpec` validators, said in the model's language.
#:
#: One copy, quoted by every prompt that asks for a spec. These are the rules the
#: validator will actually enforce, so a prompt carrying its own paraphrase of
#: them is a prompt that can fall behind ADR-0001 without anything going red —
#: the same argument `limits_block` makes about the system ceilings, and the
#: reason both live here rather than inside each prompt template.
SCHEMA_RULES = """\
- Bước nào có tool thì KHÔNG được dùng model rẻ — model rẻ gọi tool không đáng tin.
- Chỉ dùng tên tool có trong danh sách trên; bước không cần tool thì để "tools": [].
- Tool nào đòi khai domain thì phải điền vào mảng "allowed_domains" Ở CẤP NGOÀI CÙNG của JSON (tuyệt đối không đặt bên trong bước). Domain phải là hostname trần \
(ví dụ "api.erp.internal"), không kèm scheme hay đường dẫn.
- "id" của bước không được trùng nhau."""

_SKELETON = """\
{
  "name": "...",
  "description": "...",
  "steps": [
    {"id": "...", "title": "...", "prompt": "...", "model": "...", "tools": [], "max_iterations": 1, "next": null}
  ],
  "guardrails": ["..."],
  "budget": {"max_total_tokens": 60000, "max_wall_seconds": 120},
  "allowed_domains": []
}"""


class SpecGenerationError(ValueError):
    """The meta-agent could not produce a valid spec. `args[0]` is user-facing.

    Carries `reason` beside the message on purpose. The sentence the creator
    reads has to be about what *they* can do next, and the validation error that
    actually happened is about a JSON document they have never seen — useful to
    whoever is debugging the generator, useless on a screen. One exception with
    both keeps them from being merged into a message that serves neither reader.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class NoAssignableModelError(SpecGenerationError):
    """There is no model a step could be assigned, so no spec is possible.

    Its own type because the creator's next move is different in kind. Every
    other generation failure is *"thử lại"* — the platform is fine and the model
    had a bad turn. This one is a platform setting, no retry can change it, and a
    caller that could not tell the two apart would offer a retry button for a
    state no amount of pressing it will fix.
    """


async def generate_spec(
    *,
    description: str,
    slots: InterviewSlots,
    model_factory: ChatModelFactory,
    model_allowlist: Sequence[str],
) -> AgentSpec:
    """Write an `AgentSpec` for `slots`, retrying with the validation error in hand.

    Runs on `meta_agent_model()` and nothing else — the models a creator's admin
    configures are the ones their *agent's steps* run on (ADR-0010 §1), and this
    is the platform writing the agent rather than the agent working.

    The loop is the point. An invalid reply is not an error to surface; it is a
    message to send back, because the validator's complaint names the step and the
    rule and is the most useful thing anyone could tell the model. Only when the
    attempts run out does the creator hear about it, and then in a sentence they
    can act on — see `SpecGenerationError`.
    """
    return await write_spec(
        build_prompt=lambda allowed: _generation_prompt(description=description, slots=slots, allowed=allowed),
        slots=slots,
        model_factory=model_factory,
        model_allowlist=model_allowlist,
        exhausted=_EXHAUSTED,
    )


async def write_spec(
    *,
    build_prompt: Callable[[Sequence[str]], str],
    slots: InterviewSlots,
    model_factory: ChatModelFactory,
    model_allowlist: Sequence[str],
    exhausted: str,
) -> AgentSpec:
    """Ask the meta-agent for a spec until it writes a valid one, or the attempts run out.

    **The** call, in the singular. Generating from an interview and refining by
    chat (#18) differ only in the prompt they build — same model, same schema,
    same retry loop, same refusal — and that is not tidiness but the reason
    refining by conversation is cheap enough to be the *only* edit path: a second
    way to change a spec would be a second set of rules to keep in step with
    `AgentSpec`.

    The prompt arrives as a **builder** rather than a string, and that is the one
    piece of ceremony here worth paying for. The list of models a caller shows the
    model and the list the reply is validated against have to be the same list;
    handed in separately they are two arguments a third caller can get subtly out
    of step, and the symptom would be a model diligently choosing from a menu the
    validator then refuses. Narrowing the allowlist happens here, once, and the
    result is what both the prompt and the validation context are made of.

    `slots` is what the returned spec will carry whatever the reply says, so
    every caller must hand over the slots that already govern this agent — the
    interview's on the way in, the current spec's on every turn after.

    `exhausted` is the caller's, and only that, because it is the one part of
    this failure that is not shared: a creator waiting on a first draft is told
    to go and sharpen their interview, and a creator who asked for one step to
    change is told to say it differently. One sentence for both would be wrong
    on one of the two screens.
    """
    allowed = allowed_models(model_allowlist)
    context: dict[str, Any] = {"model_allowlist": list(allowed)}
    return await write_document(
        build_prompt=lambda: build_prompt(allowed),
        parse=lambda reply: _spec_from(reply, slots=slots, context=context),
        model_factory=model_factory,
        exhausted=exhausted,
        failure=lambda message, reason: SpecGenerationError(message, reason=reason),
        purpose=PlatformCallPurpose.SOLUTION_SPEC,
    )


async def draft_from_interview(
    db: Session,
    session: InterviewSession,
    *,
    model_factory: ChatModelFactory,
) -> AgentVersion:
    """Turn a finished interview into a saved **draft** version of an agent.

    The three halves of the ticket, in the only order they can happen: the sheet
    refuses to hand over its slots unless every one is filled
    (`SlotSheet.to_interview_slots`), the spec is generated against the allowlist
    in force, and what comes out enters the ordinary version lifecycle through
    `create_draft` — no second door, no status this module invented.

    Generating twice adds a *new draft to the same agent* rather than a second
    agent. An interview is one creator describing one agent, and two agents from
    one description would leave them with a workspace of near-duplicates and no
    way to tell which was which. It is also what makes the refine loop (#18) a
    continuation of this one: every attempt is a version, and the version history
    is the record of how the agent got its shape.
    """
    slots = sheet_of(session).to_interview_slots()
    spec = await generate_spec(
        description=session.description,
        slots=slots,
        model_factory=model_factory,
        model_allowlist=read_config(db).model_allowlist,
    )
    return create_draft(db, _ensure_agent(db, session, spec), spec)


def _ensure_agent(db: Session, session: InterviewSession, spec: AgentSpec) -> Agent:
    """The agent this interview is building, **creating and attaching it** if it has none.

    Named for the write it performs rather than for the read: on the first
    generation it adds an `agents` row and points the interview at it, which a
    plain `_agent_of` would have hidden behind a lookup.

    Named from the spec because the creator never typed a name: they described a
    business, and what the meta-agent called the result is the closest thing to a
    name anybody has written. A later generation leaves the name alone; renaming
    an agent under its owner because a second attempt phrased it differently is a
    change they did not ask for, and `PATCH /agents/{id}` is where renaming lives.
    """
    if session.agent is not None:
        return session.agent
    agent = Agent(owner_id=session.owner_id, name=spec.name)
    db.add(agent)
    db.flush()
    session.agent_id = agent.id
    return agent


def _spec_from(reply: str, *, slots: InterviewSlots, context: dict[str, Any]) -> AgentSpec:
    """One reply as a validated spec, with the two fields the model does not own.

    `slots` is overwritten rather than merged, and `guardrails` is rebuilt around
    the prohibitions. Both are why two of the ticket's criteria cannot fail
    intermittently: they do not depend on the reply at all.
    """
    document = json_object(reply)
    document["slots"] = slots.model_dump()
    document["guardrails"] = _guardrails(document.get("guardrails"), slots.prohibitions)
    return AgentSpec.model_validate(document, context=context)


def _guardrails(written: object, prohibitions: list[str]) -> object:
    """The prohibitions, then whatever else the model thought worth forbidding.

    Prohibitions first and verbatim: they are the creator's own sentences and the
    reason the slot was collected. What the model added is kept beside them rather
    than instead of them — it reads the business too, and a rule it inferred from
    the description is worth having.

    Anything that is not a list is handed to the validator untouched, so a model
    that answered with a string hears about it in pydantic's own words and gets
    another attempt, rather than having its mistake quietly papered over here.
    """
    if not isinstance(written, list):
        return written
    return [*prohibitions, *(rule for rule in written if rule not in prohibitions)]


def allowed_models(configured: Sequence[str]) -> tuple[str, ...]:
    """The configured allowlist, narrowed to models that are actually assignable.

    Refused when it is empty, before a single call is paid for. `AgentSpec` would
    reject every step of every attempt for the same reason, so the loop would burn
    three calls on the platform's strongest model to arrive at a message about
    retrying — when what is wrong is a platform setting no retry can change.
    """
    allowed = tuple(name for name in configured if name in ASSIGNABLE_MODELS)
    if not allowed:
        raise NoAssignableModelError(_NO_MODELS, reason=f"allowlist model hiện tại không dùng được: {list(configured)}")
    return allowed


def _generation_prompt(*, description: str, slots: InterviewSlots, allowed: Sequence[str]) -> str:
    """The whole brief: the interview, the menu it may choose from, and the ceilings."""
    return _GENERATION_PROMPT.format(
        description=description,
        audience=slots.audience,
        inputs=slots.inputs,
        capabilities=_bullets(slots.capabilities),
        success_criteria=_bullets(slots.success_criteria),
        prohibitions=_bullets(slots.prohibitions),
        models=models_block(allowed),
        tools=tools_block(TOOL_REGISTRY.all()),
        limits=limits_block(),
        schema_rules=SCHEMA_RULES,
        skeleton=_SKELETON,
    )


def limits_block() -> str:
    """The hard ceilings as lines a prompt can carry.

    A function of `SYSTEM_LIMITS` rather than four numbers typed into each
    prompt: a ceiling the platform raises and a prompt still quoting the old one
    is a model told to obey a rule the validator no longer has.
    """
    return "\n".join(
        (
            f"- nhiều nhất {SYSTEM_LIMITS.max_steps} bước",
            f"- mỗi bước nhiều nhất {SYSTEM_LIMITS.max_iterations_per_step} vòng lặp",
            f"- ngân sách nhiều nhất {SYSTEM_LIMITS.max_total_tokens} token "
            f"và {SYSTEM_LIMITS.max_wall_seconds} giây một lần chạy",
        )
    )


def _bullets(items: Sequence[str]) -> str:
    """A slot's list value as lines the prompt can carry, or a line saying it holds none."""
    return "\n".join(f"  - {item}" for item in items) if items else "  (không có)"


def models_block(allowed: Sequence[str]) -> str:
    """The models this spec may name, with the two facts that decide where each belongs.

    Price is quoted because per-step assignment is the whole cost argument (#11):
    a model told only which models exist has no basis for putting the cheap one on
    the extraction step. Tier is quoted because it is what makes a step with tools
    off-limits, and a rule stated without the fact it turns on is a rule the model
    cannot apply.
    """
    return "\n".join(f'- "{name}" — {_model_note(ASSIGNABLE_MODELS[name])}' for name in allowed)


def _model_note(info: ModelInfo) -> str:
    tier = "model rẻ, KHÔNG được gán cho bước có tool" if info.tier is ModelTier.CHEAP else "model mạnh"
    return (
        f"{tier}; {info.usd_per_million_input_tokens}$ / 1M token vào, "
        f"{info.usd_per_million_output_tokens}$ / 1M token ra"
    )


def tools_block(tools: Sequence[ToolSpec]) -> str:
    """The registry as a menu. Nothing outside it is a name a spec may use."""
    return "\n".join(f'- "{tool.name}" — {tool.description}{_tool_note(tool)}' for tool in tools)


def _tool_note(tool: ToolSpec) -> str:
    """What using this tool obliges the spec to do, said where the choice is made."""
    notes = []
    if tool.risk is ToolRisk.HIGH:
        notes.append("rủi ro cao, chỉ dùng khi nghiệp vụ thực sự cần")
    if tool.requires_declared_domains:
        notes.append('bắt buộc khai "allowed_domains"')
    return f" ({'; '.join(notes)})" if notes else ""
