"""When a run's failure belongs to the platform rather than to the agent — ADR-0061.

A run can fail two very different ways, and until ADR-0061 the record kept no
field that told them apart:

- **The world was unhelpful.** A search matched nothing, Gmail said the message
  id does not exist, an action refused arguments the conversation never
  supplied. That is material the agent has to cope with, and how it copes is
  exactly what an eval grades.
- **The platform never presented the world at all.** The owner's credential had
  expired, the connection was switched off, the node resolved to no action, the
  provider refused the model call. Nothing the agent did or could have done
  changes the transcript that came out, so a verdict read off it is a statement
  about the plumbing wearing the agent's name.

The second kind is a `PlatformFault`. It is written **at the place that knows**
— the exception carries it, the runtime copies it onto the `tool_result` line —
because a reader who had to recognise it from a Vietnamese sentence is a second
opinion about a fact the first opinion already had (ADR-0058 §1's rule, applied
to failures instead of to action names).

`fault_of_reason` is the one concession to records written before that field
existed, and it is deliberately narrow: it matches only sentences **this
platform composes itself**, whose templates live below beside the enum they map
to, and it is never consulted for a line that carries a real `fault`.
"""

import re
from enum import StrEnum


class PlatformFault(StrEnum):
    """Why a run could not exercise the agent — the closed list.

    Closed on purpose. Every value here removes a trial from the evidence, and
    a vocabulary anybody may extend by analogy is a vocabulary that eventually
    excuses a real failure. A failure that is not one of these four is the
    agent's, and is graded.
    """

    #: The owner's credential for a connection is missing, expired, unreadable
    #: or refused. The run carried no authority, so nothing it "found" is real.
    CREDENTIAL = "credential"
    #: The node could not resolve to any action: the creator pinned a name the
    #: registry does not have, or `allowed_actions` narrowed a `full`-mode node
    #: down to nothing. **Not** the case where a `full`-mode node had real
    #: candidates and the model declined to pick one — see `runtime._run_mcp`.
    ACTION_UNRESOLVED = "action_unresolved"
    #: The connection itself is unknown to the registry, or an admin has it in
    #: a status that forbids calling it.
    TOOL_UNAVAILABLE = "tool_unavailable"
    #: The model provider refused or could not be reached, so the run stopped
    #: before the agent had a turn.
    MODEL = "model"


#: What a creator is told a fault was, in one phrase per value. Here rather
#: than in the API layer because the screen and any stream and any future
#: dossier must all name it the same. Its only reader was the scenario grid's
#: cell message, so nothing composes it today (ADR-0065) — kept because the
#: mapping is the vocabulary, not the screen that happened to print it.
FAULT_SENTENCE: dict[PlatformFault, str] = {
    PlatformFault.CREDENTIAL: "credential của kết nối đã hết hạn hoặc chưa được nối",
    PlatformFault.ACTION_UNRESOLVED: "node không nối được vào action nào trong registry",
    PlatformFault.TOOL_UNAVAILABLE: "kết nối không tồn tại hoặc đang bị tắt",
    PlatformFault.MODEL: "gọi model thất bại",
}

# --------------------------------------------------------------------------- #
# The sentences the platform composes for a credential that will not work.
#
# Constants rather than literals at the raise sites, for `runtime.wire_name`'s
# reason: `fault_of_reason` below has to recognise them on a record written
# before `SandboxTranscriptEntry.fault` existed, and two spellings of one
# sentence is how ADR-0058's defect happened. `src.workflow.user_connections`
# and `src.workflow.google_tools` raise with these; nothing else may.
# --------------------------------------------------------------------------- #

#: A pasted access token whose recorded lifetime has run out.
EXPIRED_PASTE_MESSAGE = (
    "Access token dán tay đã hết hạn — dán token mới, hoặc bấm 'Đăng nhập Google' để token tự làm mới"
)
#: The agent's owner never linked a Google account at all.
NOT_CONNECTED_MESSAGE = (
    "Chủ agent chưa kết nối tài khoản Google — vào màn hình Connections và bấm 'Kết nối Google'"
)
#: The paste is on the row but the secret store will not give it back.
UNREADABLE_PASTE_MESSAGE = "Không đọc được access token đã lưu — cần dán lại hoặc đăng nhập Google"
#: The refresh token the row points at is gone.
MISSING_REFRESH_MESSAGE = "Không tìm thấy credential Google đã lưu — cần kết nối lại tài khoản Google"
#: The refresh token is there and does not decrypt.
UNDECRYPTABLE_REFRESH_MESSAGE = (
    "Credential Google đã lưu không giải mã được — SECRET_STORE_KEY có thể đã đổi; báo người vận hành"
)
#: Google answered 401 mid-run.
TOKEN_REFUSED_MESSAGE = (
    "Google không nhận token này nữa — access token dán tay đã hết hạn hoặc tài khoản bị thu hồi; "
    "vào Connections dán token mới hoặc bấm 'Đăng nhập Google'"
)

_CREDENTIAL_MESSAGES: tuple[str, ...] = (
    EXPIRED_PASTE_MESSAGE,
    NOT_CONNECTED_MESSAGE,
    UNREADABLE_PASTE_MESSAGE,
    MISSING_REFRESH_MESSAGE,
    UNDECRYPTABLE_REFRESH_MESSAGE,
    TOKEN_REFUSED_MESSAGE,
)

#: `runtime._mcp_refusal`'s two sentences, as patterns rather than prefixes so a
#: sentence that merely starts the same way cannot pass for one.
#:
#: The unresolved pattern insists the action name be **non-empty**, and that is
#: the legacy half of `runtime._unresolved_fault`'s rule. An empty name is what
#: a `full`-mode node falls back to when the model declined to pick any of the
#: real actions it was offered — a decision, not a fault — and it is exactly the
#: string `'gmail.'` that agent 103's transcripts carry.
UNRESOLVED_REASON_PATTERN = re.compile(r"Không có tool '[^']*' với action '[^']+' trong registry")
DISABLED_REASON_PATTERN = re.compile(r"đang ở trạng thái '[^']*' nên không được gọi")


def fault_of_exception(exc: BaseException | None) -> PlatformFault | None:
    """The fault `exc` declares, following the chain it was raised from.

    The chain matters and is not defensiveness: `google_tools` deliberately
    re-raises a `UserConnectionError` as a `GoogleToolError` so the run reports
    one kind of sentence, and the typed fact would be lost at that boundary if
    only the outermost exception were read. `raise … from …` is already how
    those sites are written, so nothing had to change for this to work.

    An exception declares itself by carrying a `platform_fault` attribute. An
    attribute rather than a base class because the two classes that need it
    (`ToolUnavailableError`, `UserConnectionError`) live in modules with
    different import weights, and a shared base would drag one into the other.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        fault = getattr(exc, "platform_fault", None)
        if isinstance(fault, PlatformFault):
            return fault
        exc = exc.__cause__
    return None


def fault_of_reason(reason: str) -> PlatformFault | None:
    """The fault behind a failure sentence **this platform wrote** — the reader for old records.

    Only for a `tool_result` stored before `SandboxTranscriptEntry.fault`
    existed. A line that carries the field is read from the field and never
    reaches here, so this can only ever get narrower over time, never
    load-bearing.

    Narrow by construction: every sentence it recognises is a constant in this
    module that the raising site imports, so there is no independent spelling of
    anything. What it therefore does **not** recognise is a credential failure
    whose wording came from Google (`GoogleOAuthError`'s refresh refusals) — see
    ADR-0061 §"Vẫn nợ". Under-recognising leaves a trial counted as a real
    failure, which is the direction a rule like this has to err in.
    """
    text = reason or ""
    if any(message in text for message in _CREDENTIAL_MESSAGES):
        return PlatformFault.CREDENTIAL
    if UNRESOLVED_REASON_PATTERN.search(text):
        return PlatformFault.ACTION_UNRESOLVED
    if DISABLED_REASON_PATTERN.search(text):
        return PlatformFault.TOOL_UNAVAILABLE
    return None


__all__ = [
    "DISABLED_REASON_PATTERN",
    "EXPIRED_PASTE_MESSAGE",
    "FAULT_SENTENCE",
    "MISSING_REFRESH_MESSAGE",
    "NOT_CONNECTED_MESSAGE",
    "PlatformFault",
    "TOKEN_REFUSED_MESSAGE",
    "UNDECRYPTABLE_REFRESH_MESSAGE",
    "UNREADABLE_PASTE_MESSAGE",
    "UNRESOLVED_REASON_PATTERN",
    "fault_of_exception",
    "fault_of_reason",
]
