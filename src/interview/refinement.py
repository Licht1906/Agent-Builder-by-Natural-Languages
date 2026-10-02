"""The meta-agent's third job: a saved spec and one sentence in, a new draft out — see #18.

Deliberately not a third job at all. It is `generation`'s call with a different
input — *"spec hiện tại + yêu cầu sửa → spec mới"* — sent to the same model,
validated by the same schema, retried through the same loop
(`generation.write_spec`). That is what makes an editing path this cheap
defensible: one chat box replaces the add/remove/reorder widgets a nested
structure would otherwise need, and it costs no second set of rules to keep in
step with `AgentSpec`.

It lives beside `generation` for the same reason. A refinement module that grew
its own prompt scaffolding, its own retry policy and its own idea of what the
model may decide would be the second edit path this ticket exists to *avoid*,
wearing a different name.

Two things stay out of the model's reach here, and they are narrower than
generation's three:

- **the slots** are copied from the spec being refined. They are what eval grades
  against (ADR-0011) and what the guardrails are built from; a sentence about
  step 3 is not where a creator re-opens the standard their agent is judged by.
- **the prohibitions** therefore survive every refinement, first and verbatim, as
  guardrails. What the model adds is kept beside them.

What it *does* decide is everything a creator asks about in chat: the steps and
their order, each prompt, each tool list, each model, the ceilings, the budget.
Whether those changes stay **local** — whether a step nobody mentioned comes back
word for word — is asked for in the prompt and checked in the tests, not enforced
by a validator. That boundary is stated plainly in ADR-0016 §2 rather than
implied, because it is the one promise here that a bad reply can still break.
"""

import json
from collections.abc import Sequence

from sqlalchemy.orm import Session

from src.admin.config import read_config
from src.agents.tools import TOOL_REGISTRY
from src.agents.versions import create_draft
from src.db.models import Agent, AgentVersion
from src.interview.generation import SCHEMA_RULES, limits_block, models_block, tools_block, write_spec
from src.models.agent_spec import AgentSpec
from src.services.llm import ChatModelFactory

#: What a creator is told when the meta-agent could not write a valid spec for
#: their request. Their next move, not generation's: the interview is not the
#: thing to go and fix here — the sentence they just typed is.
_EXHAUSTED = (
    "Chưa sửa được agent theo yêu cầu này sau {attempts} lần thử. "
    "Bạn thử nói lại cụ thể hơn giúp — nêu rõ bước nào và sửa gì. "
    "Bản hiện tại của agent vẫn giữ nguyên, không có gì bị mất."
)

#: Said when the request is empty. Refused before the first call: there is no
#: change to make, so no number of attempts produces one.
_BLANK = (
    'Chưa rõ bạn muốn sửa gì. Nói cụ thể giúp — ví dụ "bỏ bước 3", '
    '"bước 2 thêm quyền tra cứu đơn hàng", hay "prompt bước 1 nói rõ chỉ trả lời tiếng Việt".'
)

_REFINEMENT_PROMPT = """\
Bạn là meta-agent của một nền tảng dựng agent AI. Người tạo đã có sẵn một agent \
và muốn sửa nó bằng một câu nói.

Spec hiện tại của agent, dạng JSON:
\"\"\"
{current}
\"\"\"

Người tạo yêu cầu sửa:
\"\"\"
{instruction}
\"\"\"

Model được phép gán cho từng bước:
{models}

Tool được phép, chọn theo đúng việc mà bước đó phải làm:
{tools}

Trần cứng của hệ thống — vượt là spec bị từ chối:
{limits}

Quy tắc bắt buộc:
- Chỉ sửa ĐÚNG phần người tạo vừa yêu cầu. Mọi bước không được nhắc tới phải giữ \
nguyên từng chữ — cùng "id", cùng "title", cùng "prompt", cùng "tools", cùng "model", \
cùng "max_iterations" — và giữ nguyên thứ tự các bước.
- Trả về TOÀN BỘ spec sau khi sửa, không phải phần khác biệt.
- Bỏ một bước là xoá nó khỏi "steps"; bước nào đang trỏ "next" vào bước vừa bỏ thì sửa "next" cho đúng.
- "slots" do nền tảng giữ, bạn viết gì vào đó cũng bị bỏ qua — cứ chép lại nguyên văn.
{schema_rules}
- Nếu yêu cầu không thể làm mà không phạm các luật trên, hãy làm phần gần nhất còn hợp lệ.

Chỉ trả lời bằng một JSON object đúng khuôn của spec hiện tại, không kèm giải thích.
"""


class BlankInstructionError(ValueError):
    """The creator asked for a refinement without saying what to change. Message is user-facing.

    Its own type rather than a generation failure, because nothing failed: no
    call was made and no attempt was spent. A caller that could not tell the two
    apart would answer "the meta-agent is having trouble, try again" to somebody
    who simply pressed send on an empty box.
    """


async def refine_spec(
    *,
    current: AgentSpec,
    instruction: str,
    model_factory: ChatModelFactory,
    model_allowlist: Sequence[str],
) -> AgentSpec:
    """`current`, changed the way `instruction` asks, as a freshly validated `AgentSpec`.

    Returns a **new** spec and never touches the one it was given. That is what
    lets the caller keep serving the version this came from while the refinement
    is attempted, and what makes a refusal cost nothing: there is no half-edited
    state to roll back, because nothing was edited.

    The allowlist is the one *in force*, exactly as at generation. A version
    saved when a model was allowed keeps running if an admin withdraws it later
    (ADR-0003 §7) — but the next version written cannot name it, so a refinement
    of that version has to move the step onto a model the platform still allows.
    The validator says so on the first attempt and the model is told; that is the
    loop working, not an error.
    """
    asked = instruction.strip()
    if not asked:
        raise BlankInstructionError(_BLANK)
    return await write_spec(
        build_prompt=lambda allowed: _refinement_prompt(current=current, instruction=asked, allowed=allowed),
        slots=current.slots,
        model_factory=model_factory,
        model_allowlist=model_allowlist,
        exhausted=_EXHAUSTED,
    )


async def draft_from_refinement(
    db: Session,
    agent: Agent,
    current: AgentSpec,
    instruction: str,
    *,
    model_factory: ChatModelFactory,
) -> AgentVersion:
    """One turn of chat-driven refinement, saved as a new **draft** version of `agent`.

    Every turn is a version, and that is where the ticket's version history comes
    from — for free, and without a single write to what already exists. The
    version being refined keeps its spec and its status; `create_draft` never
    moves `agent.deployed_version_id` (ADR-0005 §2), so a creator can refine the
    version that is serving real users without the running agent changing under
    them.

    Nothing is written when the refinement fails: `create_draft` is reached only
    by a spec that validated, so a refused turn leaves the agent exactly as many
    versions as it had.
    """
    spec = await refine_spec(
        current=current,
        instruction=instruction,
        model_factory=model_factory,
        model_allowlist=read_config(db).model_allowlist,
    )
    return create_draft(db, agent, spec)


def _refinement_prompt(*, current: AgentSpec, instruction: str, allowed: Sequence[str]) -> str:
    """The whole brief: what the agent is now, what to change, and what may be chosen from.

    The current spec goes in whole and verbatim, `slots` included. A model asked
    to leave the untouched steps alone has to be able to see them, and a summary
    would be a second description of the agent for the reply to drift from.
    """
    return _REFINEMENT_PROMPT.format(
        current=json.dumps(current.model_dump(mode="json"), ensure_ascii=False, indent=2),
        instruction=instruction,
        models=models_block(allowed),
        tools=tools_block(TOOL_REGISTRY.all()),
        limits=limits_block(),
        schema_rules=SCHEMA_RULES,
    )
