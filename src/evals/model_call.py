"""One question to a model, one answer as text — and one row on the bill.

Every single-shot call the platform makes on its own behalf comes through here:
no tools, no loop, no memory. That makes them a different shape from everything
in `src.agents.runtime`, which exists to run an agent, and giving them one
two-line entry point keeps the runtime from growing a "just ask it a question"
path that no `AgentSpec` is behind.

It goes through the model whose factory the caller was handed, so this path has
exactly the same test seam as every other model call in the platform.

**`purpose` is required, and that is the point.** Twelve callers pass through
this function and between them they were the platform's entire unmetered spend —
found when an OpenAI project hit its enforced limit while the cost board reported
$1.57. A keyword with no default is the only version of this that cannot be
forgotten: a thirteenth caller either says which job it is doing or does not
compile. `src.services.platform_spend` does the rest.
"""

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

from src.agents.runtime import text_of
from src.db.models import PlatformCallPurpose
from src.services.platform_spend import metered


async def ask(
    model: BaseChatModel,
    prompt: str,
    *,
    purpose: PlatformCallPurpose,
    attempt: int = 1,
) -> str:
    """Put `prompt` to `model`, file what it cost, and return what it said.

    `attempt` is 1 unless a retry loop is counting: `write_document` re-asks with
    the validator's complaint appended, and the third attempt is both the dearest
    and the one whose existence a cost board most needs to be able to see. Left
    at 1 by every caller that asks once.
    """
    reply = await metered(model, purpose, attempt=attempt).ainvoke([HumanMessage(content=prompt)])
    return text_of(reply.content)
