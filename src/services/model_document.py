"""Ask the meta-agent for a document until it writes a valid one — the loop, once.

Lifted out of `src.interview.generation` when #18's editing path got a V2 sibling
(chat editing a workflow graph). The argument for hoisting it is the one
`src.interview.refinement` already makes about itself: a second module growing
"its own prompt scaffolding, its own retry policy and its own idea of what the
model may decide" is the second edit path that reasoning exists to avoid, wearing
a different name. Two documents now get written this way — an `AgentSpec` and a
`WorkflowJSON` — and they differ in the prompt and the parser, in nothing else.

What stays *out* of here is everything that is a fact about a particular
document: which allowlist narrows the reply, which fields the caller overwrites
after the model has spoken, what the creator is told when the attempts run out.
Those live with their document, because each is an answer to a question this
module cannot ask.

It sits under `src.services` rather than beside either caller: `src.workflow`
already depends on `src.services.llm`, and `src.workflow -> src.interview` would
be a new direction between two feature packages that have no business knowing
about each other.
"""

import logging
from collections.abc import Callable
from typing import TypeVar

from pydantic import ValidationError

from src.db.models import PlatformCallPurpose
from src.evals.model_call import ask
from src.evals.model_json import UnreadableModelReplyError
from src.services.llm import ChatModelFactory, document_model

_log = logging.getLogger(__name__)


class DocumentRejectedError(ValueError):
    """A reply that parsed and validated, and is still not a document we can accept.

    The third way a document can be wrong, and the one neither of the other two
    can express. `UnreadableModelReplyError` says *that was not JSON* and
    `ValidationError` says *that JSON was the wrong shape*; a graph whose every
    field is the right type but whose branches never reach an `End` is neither —
    it is a well-formed document failing a rule that lives above the schema
    (`src.workflow.validation`).

    A `ValueError` rather than a `ValidationError` subclass, and listed in
    `RETRYABLE` beside it, because what matters to this loop is only whether the
    reason can be handed back to the model. It can: the sentence a caller raises
    this with is written for the model to act on, exactly like a pydantic error
    dump, and `_CORRECTION` quotes it the same way.
    """


#: What `parse` is allowed to fail with, and therefore what earns a retry. All
#: three describe a reply that can be *handed back* to the model with the reason.
#: Anything else escapes, because an error the model cannot act on is not worth
#: paying for two more calls to repeat.
RETRYABLE = (UnreadableModelReplyError, ValidationError, DocumentRejectedError)

#: How many times the meta-agent may be asked before the creator is told instead.
#:
#: Three rather than one, because the common failure is a rule the model forgot
#: and being told which rule usually fixes it. Three rather than ten, because
#: every attempt is a paid call on the platform's strongest model and a creator
#: waiting at a spinner: a model that has failed three times with the validation
#: error in front of it is not one attempt away from succeeding.
MAX_ATTEMPTS = 3

#: Appended to the prompt on every attempt after the first. The whole prompt is
#: re-sent rather than only the complaint: `ask` is single-shot and holds no
#: conversation, so a bare "that was invalid" would arrive with nothing to fix.
_CORRECTION = """\

Lần trước bạn đã trả lời, nhưng JSON đó KHÔNG hợp lệ. Đây là lỗi trả về:
\"\"\"
{reason}
\"\"\"

Hãy viết lại toàn bộ JSON cho hợp lệ, sửa đúng chỗ bị báo lỗi và giữ nguyên những phần đã đúng.
"""

T = TypeVar("T")


async def write_document(
    *,
    build_prompt: Callable[[], str],
    parse: Callable[[str], T],
    model_factory: ChatModelFactory,
    purpose: PlatformCallPurpose,
    exhausted: str,
    failure: Callable[[str, str], Exception],
) -> T:
    """Ask the meta-agent for a document until `parse` accepts one, or the attempts run out.

    `parse` is where the document actually gets decided — it reads the reply,
    overwrites whatever the caller does not let the model choose, and validates.
    Raising one of `RETRYABLE` from it is how a caller says "ask again, and tell
    it this"; the exception's own text is what the model is shown, so a parser
    whose errors read as instructions is a parser that gets fixed on attempt two.

    `exhausted` and `failure` are both the caller's, and for the same reason the
    sentence was the caller's before this module existed: a creator waiting on a
    first draft is told to go and sharpen their interview, one who asked for a
    step to change is told to say it differently, and one whose graph could not
    be edited is told their graph is untouched. One sentence for all three would
    be wrong on two screens.

    The model is always `document_model()` — never an admin allowlist entry
    (ADR-0010 §1). Which model *writes* documents for this platform is not a
    setting.

    `purpose` is the caller's too, and it is what puts this loop on the bill.
    Every attempt is filed separately with its own attempt number, because the
    retry loop is where the money actually goes: a third attempt re-sends the
    whole prompt (`_CORRECTION` is appended, not substituted) and the document it
    finally produces is the one attempt one was asked for. A ledger that charged
    a document once could not show that.
    """
    prompt = build_prompt()
    model = model_factory(document_model())

    last_error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        reply = await ask(
            model,
            prompt if last_error is None else prompt + _CORRECTION.format(reason=last_error),
            purpose=purpose,
            attempt=attempt,
        )
        try:
            return parse(reply)
        except RETRYABLE as exc:
            last_error = str(exc)
    _log.warning("Viết tài liệu thất bại sau %s lần thử. Lỗi lần cuối: %s", MAX_ATTEMPTS, last_error)
    raise failure(exhausted.format(attempts=MAX_ATTEMPTS), str(last_error))
