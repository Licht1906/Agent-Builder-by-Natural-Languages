"""The Tool Definition registry — what an `MCP` node's `tool_id` actually points at.

See issue #47 / ADR-0023. `McpConfig` has carried `{tool_id, action}` since
ADR-0021 with nothing on the other end of either name; this module is the other
end. It is `TOOL_REGISTRY`'s shape one level up: a closed, code-defined
catalogue read the same way by the graph validator, the runtime and (later) the
tool catalogue screen.

**A `ToolDefinition` is one connection, not one action** (ADR-0023 §2). One
`gmail` entry carries `search_emails`, `send_email` and the rest, because the
credential and the usage scope are properties of the OAuth grant, not of an
individual call — duplicating them per action would be N copies of one approval
state. One connection per *service*, though: `gmail` and `google_drive` are
separate entries, the way a connector registry lists `connector_gmail` and
`connector_googledrive` separately, because a node naming one of them tells the
reader which surface the run touches.

**`risk_level` is derived, never declared** (ADR-0023 §3). There is no
`risk_level` field to set: it is computed from the action's effect, and
`sandbox_behavior` (§4) and `requires_human_approval` (§5) are computed from it
in turn. A self-declared risk field has no teeth as a safety gate — every
submitter has a reason to write `low` — and everything downstream of this module
(what runs mocked in a sandbox, what must have a person standing in front of it)
treats risk as a fact about the action rather than a claim about it. `extra=
"forbid"` is what makes "you cannot set it" literal rather than conventional.

**One built-in connection, and the rest of the families are absent on purpose.**
ADR-0023 §1 lists Knowledge Search and Web search as built-in tools too, but in
this platform those are *node types* (ADR-0021 §2): the runtime resolves them
from `NODE_REGISTRY` and `TOOL_REGISTRY` without ever reading a `tool_id`. Entries
for them here would be a second description of two things nothing looks up, and
the way a second description goes wrong is that it drifts from the one that runs.
They join when something actually reads them — a catalogue screen, or an eval
check over families.

**The real implementation exists and has no credentials, and says so.** The
Gmail/Drive wrapper is `built_in` (ADR-0023 §8) but `UserConnection` — the
per-end-user OAuth tokens ADR-0023's Consequences leave to a later ticket — does
not exist yet, so `invoke` raises `ToolUnavailableError` naming what is missing.
That is `src.agents.tools.builtin`'s own pattern ("the code path is real, only
the credentials are missing") rather than a stub that pretends: a run that
reaches a real send and is refused for want of a connection is telling the truth
about this platform, where a stub returning "sent!" would not be.

Creator-registered tools (ADR-0023 §1, §7) are DB rows in this same shape at
`status: pending`. `tool_definition` is written to be the one place the two
sources would be joined, so adding that half later changes one function rather
than every caller — the same split `src.workflow.templates` draws.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.workflow.effects import is_high_risk
from src.workflow.faults import PlatformFault


class ToolFamily(StrEnum):
    """The three MVP tool families of `VERSION2-REQUIREMENT.md` §5."""

    KNOWLEDGE_SEARCH = "knowledge_search"
    MCP = "mcp"
    WEB_SEARCH = "web_search"


class ToolSource(StrEnum):
    """Who wrote a tool, which is what decides where it is stored — ADR-0023 §1.

    A storage detail and not a schema divergence: every consumer reads one
    `ToolDefinition` shape whichever of the two it came from.
    """

    BUILT_IN = "built_in"
    CREATOR_REGISTERED = "creator_registered"


class ToolStatus(StrEnum):
    """Whether a workflow may actually call this tool right now.

    Checked by the runtime immediately before invoking, never at save time
    (ADR-0023 §9): a tool an admin disables after the fact must not silently
    break drafts their creators have not touched.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISABLED = "disabled"


class UsageScope(StrEnum):
    """Who may wire this connection into a workflow."""

    PUBLIC = "public"
    ORG_RESTRICTED = "org_restricted"
    CREATOR_ONLY = "creator_only"


class RiskLevel(StrEnum):
    """How much scrutiny an action's effect deserves — computed, never declared."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SandboxBehavior(StrEnum):
    """What a trial run does with this action — ADR-0023 §4.

    `LIVE_READONLY` runs the real implementation, because a read changes nothing
    and a rehearsal grounded in a mock would be evidence about the mock.
    `MOCK` answers from `mock_response_example` and says what it *would* have
    done.
    """

    LIVE_READONLY = "live_readonly"
    MOCK = "mock"


class CredentialKind(StrEnum):
    """How this connection authenticates — the shape of the secret, not the secret.

    No token ever lives on a `ToolDefinition`: `OAUTH_PER_USER` means the tokens
    are per end-user and belong in `UserConnection` (ADR-0023's Consequences),
    and `SECRET_REFERENCE` names a key in the backend secret store.
    """

    NONE = "none"
    OAUTH_PER_USER = "oauth_per_user"
    SECRET_REFERENCE = "secret_reference"
    #: The third of Agent Builder's three auth choices ("No Auth" / "Access
    #: token" / "Custom headers"). The header *values* are secrets as much as a
    #: bearer token is, so they are stored the same way — the whole mapping
    #: JSON-encoded under one secret-store key, never on the row.
    CUSTOM_HEADERS = "custom_headers"


class ToolUnavailableError(RuntimeError):
    """A real implementation exists but the connection behind it is not set up.

    The V2 counterpart of `src.agents.tools.builtin.ToolUnavailableError`, and a
    distinct class rather than that one: this is a *connection* that was never
    authorised for this end-user, which is a thing a creator can go and fix,
    where the V1 error is an operator's missing environment variable.

    A platform fault (ADR-0061): the agent asked for a connection this
    deployment cannot make, so the run never met the world the check is about.
    """

    platform_fault = PlatformFault.TOOL_UNAVAILABLE


class ToolCredential(BaseModel):
    """How a connection proves who it is — never the proof itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CredentialKind = Field(..., description="Kiểu xác thực của kết nối này")
    provider: str = Field(default="", description="Nhà cung cấp OAuth, ví dụ 'google'")
    scopes: tuple[str, ...] = Field(default=(), description="Quyền OAuth kết nối này xin")
    secret_ref: str = Field(default="", description="Khoá trong secret store, không bao giờ là chính bí mật")


def risk_of(name: str, description: str, *, read_only: bool) -> RiskLevel:
    """What an action's effect makes it worth, by ADR-0023 §3's fixed rule.

    Read-only first and unconditionally: an action that only reads is `low`
    whatever words it is described in, and asking the word list first would make
    `read_inbox` high risk because "email" appears in its description. The rule
    is keyed to the *effect*, and the effect of a read is nothing.

    Everything else goes to `src.workflow.effects`, which is already the one
    place this platform decides whether a sentence names an unrecoverable effect
    — the same function template selection reads (ADR-0025's Consequences). Two
    word lists would eventually disagree about one action, and the one they
    disagreed about would be the one that sends the email.

    Public because creator registration (ADR-0023 §7) computes risk from an
    introspected `tools/list` entry rather than from a hand-written table, and
    that path must reach the same verdict this one does.
    """
    if read_only:
        return RiskLevel.LOW
    return RiskLevel.HIGH if is_high_risk(f"{name} {description}") else RiskLevel.MEDIUM


def sandbox_behavior_for(risk: RiskLevel) -> SandboxBehavior:
    """What a trial run does with an action of this risk — ADR-0023 §4.

    A module-level rule rather than only a property on `ToolAction`, because a
    creator-registered connection under review (#52) has to show an admin what
    a rehearsal would do with each introspected action *before* there is a
    `ToolAction` to ask — an action still missing its `mock_response_example`
    cannot be constructed at all.
    """
    return SandboxBehavior.LIVE_READONLY if risk is RiskLevel.LOW else SandboxBehavior.MOCK


def approval_for(risk: RiskLevel, *, when_medium: bool) -> bool:
    """Whether an action of this risk needs a person in front of it — ADR-0023 §5.

    `high` always, `low` never, `medium` as declared. Beside `sandbox_behavior_for`
    and public for the same reason: the review queue answers this question about
    an action that is not yet a `ToolAction`, and two readings of §5 would be two
    answers to "will this need approval once I approve it".
    """
    if risk is RiskLevel.HIGH:
        return True
    if risk is RiskLevel.LOW:
        return False
    return when_medium


class ToolAction(BaseModel):
    """One call on one connection: what it takes, what it gives back, what it costs to be wrong.

    `read_only` is the only thing said about the effect, and everything else
    about risk is read off it (see `risk_of`). It is a fact the *team* declares
    for a built-in tool and a fact `tools/list` introspection reports for a
    registered one — in neither case something a creator types into a form.

    There is no `risk_level`, no `sandbox_behavior` and no bare
    `requires_human_approval` field to set. `approval_when_medium` is the one
    dial ADR-0023 §5 leaves open, and it is deliberately named for the only
    case it applies to: it cannot switch a `high` action's gate off, and
    switching a `low` one's on would be an approval step in front of a read.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    name: str = Field(..., min_length=1, description="Tên action, là thứ `McpConfig.action` trỏ tới")
    description: str = Field(..., min_length=1, description="Action này làm gì, viết cho người tạo đọc")
    input_schema: dict[str, Any] = Field(default_factory=dict, description="JSON Schema của tham số")
    output_schema: dict[str, Any] = Field(default_factory=dict, description="JSON Schema của kết quả")
    read_only: bool = Field(..., description="True = chỉ đọc, không đổi gì ở ngoài — cơ sở của mọi mức rủi ro")
    mock_response_example: dict[str, Any] | None = Field(
        default=None, description="Bắt buộc khi sandbox chạy bản mô phỏng; phải khớp `output_schema`"
    )
    approval_when_medium: bool = Field(
        default=False, description="Chỉ có tác dụng với action mức `medium` — ADR-0023 §5"
    )
    risk_override: RiskLevel | None = Field(
        default=None, description="Mức rủi ro một admin đã xem xét rồi tự đặt — chỉ admin, xem docstring"
    )
    #: Which parameters a `collect` node (ADR-0052) may only accept **as the
    #: user wrote them**. `verbatim`: the value must appear in the conversation
    #: character for character — a model asked to fill `file_id` will otherwise
    #: oblige with a plausible one. `email`: verbatim, and it must contain `@`
    #: — *"anh Minh"* is in the conversation and is not an address. Kept here,
    #: beside the schema, rather than as `format`/`x-` keywords inside it: the
    #: schema goes to the provider as a function signature, and providers differ
    #: on which keywords they reject.
    verbatim: dict[str, Literal["verbatim", "email"]] = Field(
        default_factory=dict, description="Tham số chỉ nhận đúng như người dùng viết: 'verbatim' hoặc 'email'"
    )

    @property
    def risk_level(self) -> RiskLevel:
        """This action's risk: an admin's reviewed verdict if there is one, else its effect.

        The override is the single exception ADR-0023 §3 writes into the rule,
        and it exists because the computed answer is a *word list* over a
        sentence somebody else wrote (`src.workflow.effects`): a real
        `list_campaigns` whose description happens to say "chiến dịch email" is
        exactly the row an admin has to be able to correct. It is still not a
        field a submitter fills in — nothing on the creator registration path
        writes it, and the only writer is
        `src.workflow.registrations.override_risk`, behind `AdminUser`.

        Everything downstream keeps reading *this* property rather than the
        computation, so an override reaches `sandbox_behavior` and
        `requires_human_approval` too — an admin who marks an action `high` has
        also mocked it in every sandbox and put a person in front of it, without
        touching either field.
        """
        return self.risk_override or risk_of(self.name, self.description, read_only=self.read_only)

    @property
    def sandbox_behavior(self) -> SandboxBehavior:
        """What a trial run does with it — ADR-0023 §4, derived from risk rather than set.

        A second, independently-authored field would be free to say "run it for
        real" about an action risk already called `high`, and the two would drift
        apart exactly once.
        """
        return sandbox_behavior_for(self.risk_level)

    @property
    def requires_human_approval(self) -> bool:
        """Whether a person must stand between the start of a graph and this action.

        `high` always, `low` never, `medium` as declared (ADR-0023 §5). Enforced
        structurally by `src.workflow.validation`, not merely described here — a
        descriptive-only field would leave `risk_level: high` as documentation
        with nothing stopping a graph from calling it unguarded.
        """
        return approval_for(self.risk_level, when_medium=self.approval_when_medium)

    @model_validator(mode="after")
    def _a_mocked_action_has_something_to_answer_with(self) -> Self:
        """An action the sandbox must mock cannot reach `approved` without its mock.

        ADR-0023 §4 makes the example mandatory rather than optional because the
        alternative is a trial run that reaches a side effect and has nothing to
        say — which would either stop the rehearsal or, far worse, tempt the
        runtime into calling the real thing for want of an answer.
        """
        if self.sandbox_behavior is SandboxBehavior.MOCK and self.mock_response_example is None:
            raise ValueError(
                f"action {self.name!r} có rủi ro {self.risk_level.value!r} nên sandbox phải chạy bản mô phỏng — "
                f"thiếu `mock_response_example`"
            )
        return self


#: How a connection actually performs one of its actions: `(action, args) -> text`.
#:
#: Connection-level rather than per action, because that is what a connection is
#: — one MCP server answering `tools/call` for every name in its `actions[]`. A
#: callable per action would make the built-in wrapper N functions that differ
#: only in a string they pass on.
#:
#: `Callable[..., str]` rather than the exact two-argument shape, because a
#: connection may additionally declare keyword-only context it needs — the
#: per-user OAuth built-ins take `agent_id`, from which they resolve whose
#: account the run borrows. The runtime reads the signature and passes what the
#: implementation asks for — `src.agents.runtime.invoke_tool`'s own pattern for
#: `allowed_domains` — so a connection that never asked never sees it.
ToolInvoker = Callable[..., str]


class ToolDefinition(BaseModel):
    """One connected tool — a whole MCP server, or a family with a single action.

    Frozen, like `ToolSpec`: nothing downstream, least of all the content of a
    workflow, can talk a connection into a different credential or a different
    status.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    id: str = Field(..., min_length=1, description="Id kết nối, là thứ `McpConfig.tool_id` trỏ tới")
    family: ToolFamily
    name: str = Field(..., min_length=1, description="Tên hiển thị cho người tạo")
    source: ToolSource = ToolSource.BUILT_IN
    status: ToolStatus = ToolStatus.APPROVED
    credential: ToolCredential
    actions: tuple[ToolAction, ...] = Field(..., min_length=1)
    usage_scope: UsageScope = UsageScope.PUBLIC
    invoke: ToolInvoker = Field(..., description="Implementation thật của kết nối này")

    @model_validator(mode="after")
    def _actions_are_distinctly_named(self) -> Self:
        """Two actions under one name is a `McpConfig.action` that resolves to either.

        Refused at registration, where a developer sees it, rather than at run
        time as a side effect nobody chose.
        """
        names = [action.name for action in self.actions]
        if len(set(names)) != len(names):
            raise ValueError(f"tool {self.id!r}: có hai action trùng tên")
        return self

    def action(self, name: str) -> ToolAction | None:
        """The action called `name`, or `None` if this connection has no such action.

        `None` rather than a raise, for `WorkflowJSON.node`'s reason: a half-
        configured `MCP` node naming an action that is not there is a draft the
        canvas reports on, and it is not an error for a caller to *ask*.
        """
        return next((action for action in self.actions if action.name == name), None)

    @property
    def requires_human_approval(self) -> bool:
        """Whether *any* action on this connection needs a person in front of it.

        The connection-level reading of ADR-0023 §1's field. Derived from the
        actions rather than stored beside them, so a connection cannot claim to
        be safer than the riskiest thing it can do.
        """
        return any(action.requires_human_approval for action in self.actions)


class ToolDefinitionRegistry:
    """The closed catalogue of connections. Nothing outside this process adds to it."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> ToolDefinition:
        """Add a connection. An id may only be registered once."""
        if tool.id in self._tools:
            raise ValueError(f"tool definition {tool.id!r} đã được đăng ký")
        self._tools[tool.id] = tool
        return tool

    def get(self, tool_id: str) -> ToolDefinition | None:
        """The connection registered under `tool_id`, or `None`.

        `None` rather than a `KeyError`: both callers — the validator drawing
        rings on a draft, and the runtime refusing a node by name — want to say
        something specific about a `tool_id` nobody registered, and neither is
        served by an exception thrown from inside a lookup.
        """
        return self._tools.get(tool_id)

    def all(self) -> tuple[ToolDefinition, ...]:
        """Every registered connection, in registration order."""
        return tuple(self._tools.values())

    def ids(self) -> tuple[str, ...]:
        """Every registered connection id — the only ids an `MCP` node may name."""
        return tuple(self._tools)


#: The one registry the platform runs on, populated below.
TOOL_DEFINITIONS = ToolDefinitionRegistry()


def tool_definition(tool_id: str) -> ToolDefinition | None:
    """The connection `tool_id` names, wherever it is stored.

    The single seam between the code-defined half of the registry and the
    DB-backed creator-registered half ADR-0023 §1 puts beside it — and since #52
    both halves are real. Every consumer asks here, which is why standing the
    second half up was a change to this function rather than to the validator,
    the runtime and every screen at once.

    The import is local because the registrations module reads `src.db.models`,
    which reads this module: one of the two directions has to be deferred, and
    deferring it here keeps the dependency pointing the way it reads — a
    registration is a kind of tool, a tool is not a kind of registration.

    Only ids of the registered form reach the database (see
    `src.workflow.registrations.registration_id_of`), so a lookup of a built-in
    connection, or of a `tool_id` nobody ever registered, still costs nothing.
    """
    built_in = TOOL_DEFINITIONS.get(tool_id)
    if built_in is not None:
        return built_in
    from src.workflow.registrations import registered_tool

    return registered_tool(tool_id)


def resolved_action(tool_id: str, action_name: str) -> tuple[ToolDefinition, ToolAction] | None:
    """The connection and action an `MCP` node's config names, or `None` if either is missing.

    One lookup for the two-level reference ADR-0023 §2 fixed, because every
    caller needs both halves and a caller that resolved them separately could
    hold an action from one connection beside another connection's credential.
    """
    tool = tool_definition(tool_id)
    if tool is None:
        return None
    action = tool.action(action_name)
    return None if action is None else (tool, action)


#: Risk ordered from harmless to unrecoverable, so "the worst of these" is a
#: `max` rather than a chain of comparisons every caller writes for itself.
_RISK_ORDER: dict[RiskLevel, int] = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


def offered_actions(tool_id: str, allowed: Sequence[str] = ()) -> tuple[ToolDefinition, tuple[ToolAction, ...]] | None:
    """The connection and every action a `full`-mode node may call on it.

    `allowed` narrows, never widens: a name the connection does not offer is
    dropped rather than honoured, so a node config carrying a stale action name
    (an admin removed it from `ToolRegistration.allowed_actions` since) shrinks
    the set instead of resurrecting the action. Empty `allowed` means "every
    action this connection offers", which is what an unconfigured `full` node
    means and what Agent Builder's empty `allowed_tools` means too.

    `None` when the connection itself is unknown — the same answer
    `resolved_action` gives, so callers branch once for both modes.
    """
    tool = tool_definition(tool_id)
    if tool is None:
        return None
    wanted = {name.strip() for name in allowed if name.strip()}
    actions = tuple(action for action in tool.actions if not wanted or action.name in wanted)
    return tool, actions


def riskiest(actions: Sequence[ToolAction]) -> ToolAction | None:
    """The action of `actions` whose effect is worst, or `None` for an empty set.

    What a `full`-mode node's approval gate is keyed to. Taking the maximum
    rather than any kind of average is the whole safety argument: a set of five
    reads and one `send_email` is a node that can send an email, and the gate
    has to be the one that action would have got on its own.
    """
    return max(actions, key=lambda action: _RISK_ORDER[action.risk_level], default=None)


def mock_answer(action: ToolAction) -> str:
    """What a mocked action says it *would* have done, as text for the transcript.

    JSON rather than prose, because `mock_response_example` is shaped by
    `output_schema` and a reader comparing a rehearsal against a real run should
    be comparing the same shape. `ensure_ascii=False` for the same reason every
    other payload in this platform uses it: a Vietnamese example rendered as
    escape sequences is evidence nobody can read.
    """
    return json.dumps(action.mock_response_example, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# The built-in Gmail and Google Drive connections — ADR-0023 §8, one per service
# --------------------------------------------------------------------------- #

#: The two connection ids, split the way OpenAI's connector registry splits them
#: (`connector_gmail` / `connector_googledrive`): a canvas dropdown saying
#: "Gmail" tells the reader which account surface a node touches, where a merged
#: "Google Workspace" would say only "somewhere at Google".
#:
#: There is deliberately **no merged `google_workspace` entry** any more. One
#: connection per service is what the connector registry this platform is
#: modelled on does, and a compatibility alias beside them would be a second id
#: answering the same question — which is how two graphs come to name the same
#: mailbox by different names.
GMAIL = "gmail"
GOOGLE_DRIVE = "google_drive"


def _gmail_invoke(action: str, args: Mapping[str, Any], *, agent_id: int) -> str:
    """Real Gmail, as the agent owner. Import deferred: `google_tools` reads the
    database for `agents.owner_id`, and this module must stay importable first."""
    from src.workflow.google_tools import gmail_call

    return gmail_call(action, args, agent_id=agent_id)


def _drive_invoke(action: str, args: Mapping[str, Any], *, agent_id: int) -> str:
    """Real Google Drive, as the agent owner — `_gmail_invoke`'s twin."""
    from src.workflow.google_tools import drive_call

    return drive_call(action, args, agent_id=agent_id)


#: One consent covers both connections (`google_oauth.GOOGLE_SCOPES` is their
#: union), but each declares only what *it* uses, so the Connections screen can
#: say truthfully which powers a given connection holds.
_GMAIL_CREDENTIAL = ToolCredential(
    kind=CredentialKind.OAUTH_PER_USER,
    provider="google",
    scopes=(
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
    ),
)

_DRIVE_CREDENTIAL = ToolCredential(
    kind=CredentialKind.OAUTH_PER_USER,
    provider="google",
    scopes=(
        "https://www.googleapis.com/auth/drive.readonly",
        # `drive.file`, not `drive`: writes reach only files this platform
        # created. "Full quyền" đủ để tạo tài liệu, không phải chìa khoá cả ổ.
        "https://www.googleapis.com/auth/drive.file",
    ),
)


TOOL_DEFINITIONS.register(
    ToolDefinition(
        id=GMAIL,
        family=ToolFamily.MCP,
        name="Gmail",
        # `built_in` although it is structurally an MCP-shaped connection: the
        # team writes and hosts the implementation, so its risk is reviewed by
        # ordinary code review, not the creator-approval queue (ADR-0023 §8).
        source=ToolSource.BUILT_IN,
        status=ToolStatus.APPROVED,
        credential=_GMAIL_CREDENTIAL,
        actions=(
            ToolAction(
                name="get_profile",
                description="Xem địa chỉ email và số thư của tài khoản đã kết nối",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object", "properties": {"emailAddress": {"type": "string"}}},
                read_only=True,
            ),
            ToolAction(
                name="search_emails",
                description="Tìm thư theo cú pháp tìm kiếm của Gmail (from:, subject:, after:...)",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
                    "required": ["query"],
                },
                output_schema={"type": "object", "properties": {"messages": {"type": "array"}}},
                read_only=True,
            ),
            ToolAction(
                name="get_recent_emails",
                description="Đọc các thư gần nhất trong hộp thư đến",
                input_schema={"type": "object", "properties": {"max_results": {"type": "integer"}}},
                output_schema={"type": "object", "properties": {"messages": {"type": "array"}}},
                read_only=True,
            ),
            ToolAction(
                name="read_email",
                description="Đọc toàn văn một thư theo message_id",
                input_schema={
                    "type": "object",
                    "properties": {"message_id": {"type": "string"}},
                    "required": ["message_id"],
                },
                output_schema={"type": "object", "properties": {"body": {"type": "string"}}},
                read_only=True,
                verbatim={"message_id": "verbatim"},
            ),
            ToolAction(
                name="create_draft",
                # Worded without naming the effect it does *not* have. `risk_of`
                # reads this sentence with a word list (`src.workflow.effects`),
                # so a description saying "chưa gửi đi đâu" would classify a
                # draft as a send — the lopsidedness that module documents,
                # meeting a built-in whose effect the team already knows exactly.
                description="Tạo bản nháp thư trong Gmail, chỉ lưu lại chứ không phát đi",
                input_schema={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                    },
                },
                output_schema={"type": "object", "properties": {"draft_id": {"type": "string"}}},
                # A write, but a recoverable one — the `medium` middle ADR-0023
                # §3 leaves to judgement. Left without an approval gate because a
                # draft nobody sent is undone by deleting it.
                read_only=False,
                verbatim={"to": "email"},
                mock_response_example={
                    "draft_id": "draft-mo-phong-001",
                    "note": "Bản nháp đã lưu trong Gmail, chưa gửi đi đâu",
                },
            ),
            ToolAction(
                name="send_email",
                description="Gửi email tới người nhận — không lấy lại được",
                input_schema={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["to", "subject", "body"],
                },
                output_schema={"type": "object", "properties": {"message_id": {"type": "string"}}},
                verbatim={"to": "email"},
                read_only=False,
                mock_response_example={"message_id": "msg-mo-phong-001", "sent": False},
            ),
        ),
        invoke=_gmail_invoke,
    )
)


TOOL_DEFINITIONS.register(
    ToolDefinition(
        id=GOOGLE_DRIVE,
        family=ToolFamily.MCP,
        name="Google Drive",
        source=ToolSource.BUILT_IN,
        status=ToolStatus.APPROVED,
        credential=_DRIVE_CREDENTIAL,
        actions=(
            ToolAction(
                name="get_profile",
                description="Xem tài khoản Google Drive đã kết nối",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object", "properties": {"user": {"type": "object"}}},
                read_only=True,
            ),
            ToolAction(
                name="search",
                description="Tìm tệp trong Google Drive theo từ khoá, trong tên và nội dung",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
                    "required": ["query"],
                },
                output_schema={"type": "object", "properties": {"files": {"type": "array"}}},
                read_only=True,
            ),
            ToolAction(
                name="recent_documents",
                description="Liệt kê các tệp sửa gần nhất trong Drive",
                input_schema={"type": "object", "properties": {"max_results": {"type": "integer"}}},
                output_schema={"type": "object", "properties": {"files": {"type": "array"}}},
                read_only=True,
            ),
            ToolAction(
                name="fetch",
                description="Đọc nội dung một tệp theo file_id — Google Docs, Sheets, Slides và tệp văn bản",
                input_schema={
                    "type": "object",
                    "properties": {"file_id": {"type": "string"}},
                    "required": ["file_id"],
                },
                output_schema={"type": "object", "properties": {"content": {"type": "string"}}},
                verbatim={"file_id": "verbatim"},
                read_only=True,
            ),
            ToolAction(
                name="create_doc",
                description="Tạo một Google Doc mới trong Drive từ nội dung markdown",
                input_schema={
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["title", "content"],
                },
                output_schema={"type": "object", "properties": {"file_id": {"type": "string"}}},
                # A document's name is the user's to give, like `to` on an email:
                # a creator who never said what to call the file gets asked, not a
                # plausible title the model made up. Observed on the pitch fleet —
                # a run whose only missing field was the name saved a Google Doc
                # called *"Biên bản họp với Khách hàng 1 sáng 30/08/2026"* while the
                # template that graph had just read requires `BBLV-<ddmmyyyy>-<tên
                # khách>`, and nothing downstream can tell the invented name from a
                # given one. `content` stays free: it is what the agent is for.
                verbatim={"title": "verbatim"},
                read_only=False,
                mock_response_example={
                    # The same three keys the real call returns (`_create_doc`):
                    # a mock shaped differently from the real reply teaches the
                    # answering node the wrong shape, and the first agent built
                    # on it wrote ".../d/your_document_id_here" for the link.
                    "file_id": "doc-mo-phong-001",
                    "name": "Tài liệu mô phỏng",
                    "link": "https://docs.google.com/document/d/doc-mo-phong-001/edit",
                },
            ),
        ),
        invoke=_drive_invoke,
    )
)
