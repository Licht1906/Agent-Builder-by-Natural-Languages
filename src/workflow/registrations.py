"""Creator-registered MCP connections, and the admin queue that reviews them — #52, ADR-0023 §1, §7.

`src.workflow.tools` is the *registry*: a code-defined built-in connection plus,
from here on, the DB-backed half `tool_definition` was always written to join.
This module is the other half — how a `ToolRegistration` row is born, what an
admin may change about it, and how a decided row turns back into the one
`ToolDefinition` shape both halves of the registry share.

**The creator never writes the schema** (ADR-0023 §7). `register_tool` takes a
`server_url` and a credential *reference* and calls `tools/list` on the server
itself (`src.workflow.mcp`); `actions[]` is whatever came back, stored verbatim.
A registration form that accepted hand-entered actions would let the registry
drift away from what the server implements — and the drift would be discovered
by a workflow calling an action that is not there.

**An admin may change the risk and nothing else.** `override_risk` writes one
entry in `risk_overrides`, keyed by action name; there is no function here that
writes `actions`, which is what makes "cannot edit the introspected schema"
structural rather than a rule somebody remembers. Risk is the one judgement the
computed answer can get wrong (`src.workflow.effects` is a word list over a
sentence a stranger wrote), and it is the one thing a reviewer is *for*.

**Approval is where the sandbox promise is collected.** ADR-0023 §4 requires a
`mock_response_example` for every action a rehearsal must mock. The creator
supplies them (`set_mock_responses`) once introspection has told them which
actions those are, and `approve_registration` refuses until they exist — the
last moment the promise can still be kept, because from `approved` onward a
workflow may reach the action.

**Disabling is enforced at run time only** (ADR-0023 §9). Nothing here touches a
saved workflow, and `registered_tool` keeps answering for a `disabled` row so
the runtime can refuse it *by name and status* rather than reporting a tool that
suddenly does not exist.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.base import SessionLocal
from src.db.models import ToolRegistration
from src.services.secret_store import EncryptedDbSecretStore, SecretNotFoundError
from src.workflow.mcp import IntrospectedAction, McpError, McpTransport, call_action, http_rpc, introspect
from src.workflow.tools import (
    CredentialKind,
    RiskLevel,
    SandboxBehavior,
    ToolAction,
    ToolCredential,
    ToolDefinition,
    ToolFamily,
    ToolInvoker,
    ToolSource,
    ToolStatus,
    ToolUnavailableError,
    UsageScope,
    approval_for,
    risk_of,
    sandbox_behavior_for,
)


class ToolRegistrationError(ValueError):
    """A registration, or a review decision, that the rules refuse. Message is user-facing."""


#: How a registered connection is addressed by `McpConfig.tool_id`.
#:
#: Namespaced against the built-in ids (`google_workspace`), which are bare words
#: with no separator — a creator naming their connection `google_workspace` must
#: not be able to shadow the connection of that name, and `tool_definition` asks
#: the code-defined registry first regardless.
_ID_PREFIX = "registered-"


def registration_id_of(row: ToolRegistration) -> str:
    """The `ToolDefinition.id` — and so the `McpConfig.tool_id` — this row answers to."""
    return f"{_ID_PREFIX}{row.id}"


def register_tool(
    db: Session,
    *,
    submitted_by: int,
    name: str,
    server_url: str,
    credential: ToolCredential,
    description: str = "",
    usage_scope: UsageScope = UsageScope.PUBLIC,
    mock_response_examples: Mapping[str, Any] | None = None,
    allowed_actions: Sequence[str] | None = None,
    rpc: McpTransport = http_rpc,
) -> ToolRegistration:
    """Introspect `server_url` and land the result as a `pending` registration.

    The introspection happens *before* the row exists, on purpose: a server that
    cannot be reached, or that answers with something other than a tool list, has
    not registered anything — there is no half-built row for the creator to come
    back and fix, and no entry in the admin's queue that only turns out to be
    empty when somebody opens it.

    `mock_response_examples` is accepted here for the creator who already knows
    their own server's actions, and left to `set_mock_responses` for the far more
    common case of finding out what the actions are by submitting.
    `allowed_actions` is accepted on the same terms and for the same reason, and
    `set_allowed_actions` is the door for the common case.

    **The introspection authenticates.** `tools/list` is a call like any other
    and most real servers want a credential for it; asking anonymously means a
    connection whose secret is perfectly good is refused at registration with a
    401 that reads, to its creator, as their own server being broken. A
    `SECRET_REFERENCE` credential is resolved here exactly as `_invoker`
    resolves it at run time — and a `secret_ref` naming nothing in the store is
    a registration error rather than a silent anonymous retry.
    """
    try:
        secret, headers = _introspection_auth(credential)
        actions = introspect(server_url, rpc=rpc, secret=secret, headers=headers)
    except McpError as exc:
        raise ToolRegistrationError(str(exc)) from None
    narrowed = _checked_allowlist(allowed_actions, known={action.name for action in actions})
    row = ToolRegistration(
        submitted_by=submitted_by,
        name=name,
        server_url=server_url,
        description=description,
        credential_kind=credential.kind,
        credential_provider=credential.provider,
        credential_scopes=list(credential.scopes),
        credential_secret_ref=credential.secret_ref,
        actions=[action.model_dump(mode="json") for action in actions],
        allowed_actions=narrowed,
        risk_overrides={},
        mock_response_examples=dict(mock_response_examples or {}),
        usage_scope=usage_scope,
        status=ToolStatus.PENDING,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def registrations_of(db: Session, submitted_by: int) -> Sequence[ToolRegistration]:
    """Every connection this creator has registered, newest last — where their own submissions stand."""
    statement = (
        select(ToolRegistration)
        .where(ToolRegistration.submitted_by == submitted_by)
        .order_by(ToolRegistration.created_at)
    )
    return db.execute(statement).scalars().all()


def pending_registrations(db: Session) -> Sequence[ToolRegistration]:
    """Every registration waiting on an admin, oldest first — the review queue of criterion 3."""
    statement = (
        select(ToolRegistration)
        .where(ToolRegistration.status == ToolStatus.PENDING)
        .order_by(ToolRegistration.created_at)
    )
    return db.execute(statement).scalars().all()


def set_mock_responses(db: Session, row: ToolRegistration, *, examples: Mapping[str, Any]) -> ToolRegistration:
    """Record what each action *would* have answered in a sandbox — ADR-0023 §4.

    `pending` only. Once an admin has approved a connection its mocks are part of
    what they approved: a creator who could rewrite them afterwards could change
    what every rehearsal of every workflow using this tool reports, without
    anybody reviewing the new answer.

    Merged rather than replaced, so supplying the second of three mocks does not
    silently drop the first. An action name the server never reported is refused
    outright — it is either a typo or a mock for an action that will never run,
    and both are better said now than discovered when approval is refused for a
    mock that looked present.
    """
    _require_pending(row, action="bổ sung mock cho")
    known = {action.name for action in introspected_actions(row)}
    unknown = sorted(set(examples) - known)
    if unknown:
        raise ToolRegistrationError(
            f"Server không khai action nào tên {', '.join(repr(name) for name in unknown)} — "
            f"chỉ đặt mock được cho: {', '.join(sorted(known))}"
        )
    row.mock_response_examples = {**row.mock_response_examples, **examples}
    return _persist(db, row)


def set_allowed_actions(db: Session, row: ToolRegistration, *, names: Sequence[str]) -> ToolRegistration:
    """Narrow this connection to `names` — the `allowed_tools` of ADR-0023's registry.

    Replaced rather than merged, unlike `set_mock_responses`: a mock is one
    answer among many and supplying the second must not drop the first, whereas
    an allowlist *is* the whole answer and merging would make removing an action
    impossible through the only door there is.

    An empty list means no narrowing — every action the server reported — which
    is both the default and the way back from having narrowed too far. It is
    deliberately not spelled `None`: a form that clears a multi-select sends an
    empty list, and reading that as "leave it alone" would make the clear button
    do nothing.

    `pending` only, for `set_mock_responses`' reason. From `approved` onward the
    allowlist is part of what the admin approved, and a creator who could widen
    it afterwards could put an action into live workflows that nobody reviewed —
    which is the whole of what the review queue is for.
    """
    _require_pending(row, action="đổi danh sách action cho phép của")
    row.allowed_actions = _checked_allowlist(names, known={action.name for action in introspected_actions(row)})
    return _persist(db, row)


def _checked_allowlist(names: Sequence[str] | None, *, known: set[str]) -> list[str]:
    """`names` as a stored allowlist, refusing any action the server never reported.

    Refused rather than quietly dropped, because a silently ignored name is an
    allowlist that looks narrower than it is: a creator who mistypes one of
    three actions would believe they had permitted three and have permitted two,
    and nothing on the screen would say which.

    Sorted and de-duplicated so that two requests naming the same actions store
    the same list — a diff of a registration should not show a change nobody
    made.
    """
    if not names:
        return []
    unknown = sorted(set(names) - known)
    if unknown:
        raise ToolRegistrationError(
            f"Server không khai action nào tên {', '.join(repr(name) for name in unknown)} — "
            f"chỉ cho phép được: {', '.join(sorted(known))}"
        )
    return sorted(set(names))


def _introspection_auth(credential: ToolCredential) -> tuple[str | None, dict[str, str] | None]:
    """How `tools/list` should authenticate: a bearer secret, custom headers, or neither.

    `SECRET_REFERENCE` resolves to a bearer token and `CUSTOM_HEADERS` to a
    header mapping — the two shapes Agent Builder's dialog offers beside "no
    auth". An `OAUTH_PER_USER` connection has no platform-held token by
    definition (`_invoker` refuses to call it at run time for that reason), so
    introspecting it anonymously is the honest attempt, and a server that then
    refuses says so in its own words.
    """
    if credential.kind not in (CredentialKind.SECRET_REFERENCE, CredentialKind.CUSTOM_HEADERS):
        return None, None
    if not credential.secret_ref:
        return None, None
    stored = _stored_secret(credential.secret_ref)
    if credential.kind is CredentialKind.CUSTOM_HEADERS:
        return None, _headers_from(stored)
    return stored, None


def _stored_secret(secret_ref: str) -> str:
    """The plaintext `secret_ref` names, or a registration error naming the gap."""
    with SessionLocal() as session:
        try:
            return EncryptedDbSecretStore(db=session).get(secret_ref)
        except SecretNotFoundError:
            raise ToolRegistrationError(
                f"Kết nối cần bí mật {secret_ref!r} nhưng secret store không có khoá này — chưa đăng ký được"
            ) from None


def _headers_from(stored: str) -> dict[str, str]:
    """A `CUSTOM_HEADERS` secret as the mapping it was stored as.

    Stored as one JSON object under one key (see the route that writes it), so
    a value that will not parse is a store somebody edited by hand rather than
    anything a creator did — and it is refused rather than sent as a header
    literally named after a brace.
    """
    try:
        parsed = json.loads(stored)
    except ValueError:
        raise ToolRegistrationError("Header tuỳ chỉnh đã lưu không đọc được — cần đăng ký lại kết nối") from None
    if not isinstance(parsed, dict):
        raise ToolRegistrationError("Header tuỳ chỉnh đã lưu không đúng dạng — cần đăng ký lại kết nối")
    return {str(key): str(value) for key, value in parsed.items()}


def override_risk(
    db: Session, row: ToolRegistration, *, admin_id: int, action_name: str, risk: RiskLevel
) -> ToolRegistration:
    """Replace one action's computed `risk_level` with the reviewing admin's — ADR-0023 §7.

    The *only* thing a review may change about a registration. There is
    deliberately no sibling of this function for the schema: `actions` is the
    server's own report, and an admin editing it would produce a registry entry
    that no longer describes any server.

    `pending` only, for `approve_registration`'s reason — a risk override is part
    of the decision, and re-grading an approved connection would move a live
    tool's approval gate under workflows already built against it. Raising the
    risk of something already in use is a `disable` and a re-registration, which
    is visible to its creator.

    **`low` is refused for an action that writes**, and this is the one place the
    override is not the admin's to make. `low` is not a milder opinion in this
    schema: ADR-0023 §4 reads `sandbox_behavior: "live_readonly"` straight off it,
    "safe to run for real in sandbox, since it only reads". Allowing it on an
    action introspected as a write would let one click turn every future
    rehearsal of `send_campaign` into a real send — and drop the mock requirement
    in the same motion, so nothing downstream would even notice the gap. What an
    admin is correcting when they lower a risk is the *word list*
    (`src.workflow.effects`), which only ever chooses between `medium` and
    `high`; whether the action reads at all is `src.workflow.mcp.reads_only`'s
    answer, and there is deliberately no path here to overrule it.

    `admin_id` is taken but not written to `decided_by`: that column means "who
    decided this row", and a re-grade is not a decision — writing it here would
    only be overwritten by the approve or reject that follows, leaving a value
    that looks like provenance and is not.
    """
    _require_pending(row, action="đổi mức rủi ro của")
    action = next((entry for entry in introspected_actions(row) if entry.name == action_name), None)
    if action is None:
        raise ToolRegistrationError(f"Kết nối này không có action nào tên {action_name!r}")
    if risk is RiskLevel.LOW and not action.read_only:
        raise ToolRegistrationError(
            f"Action {action_name!r} có ghi ra bên ngoài nên không hạ xuống mức {RiskLevel.LOW.value!r} được — "
            f"mức đó nghĩa là sandbox chạy thật. Hạ xuống {RiskLevel.MEDIUM.value!r} nếu nó không phải việc "
            f"không lấy lại được"
        )
    row.risk_overrides = {**row.risk_overrides, action_name: risk.value}
    return _persist(db, row)


def approve_registration(db: Session, row: ToolRegistration, *, admin_id: int) -> ToolRegistration:
    """`pending -> approved`. From this moment an `MCP` node may name this connection.

    Refuses a connection whose mocked actions have nothing to answer with
    (ADR-0023 §4). This is the last gate before a rehearsal could reach one of
    them, and a trial run that arrives at a side effect with no mock either stops
    or — the failure the rule exists against — runs the real thing for want of an
    answer. Named in one message, all of them at once, so the creator makes one
    trip rather than one per action.
    """
    _require_pending(row, action="duyệt")
    # `offered_actions`, not `reviewed_actions`: an action the allowlist leaves
    # out can never be reached by a rehearsal, so demanding a mock for it would
    # block approval on an answer nothing will ever ask for.
    missing = [action.name for action in offered_actions(row) if action.needs_mock and action.mock_response is None]
    if missing:
        raise ToolRegistrationError(
            "Chưa duyệt được: sandbox phải chạy bản mô phỏng cho các action "
            f"{', '.join(repr(name) for name in missing)} nhưng chưa có `mock_response_example`"
        )
    row.status = ToolStatus.APPROVED
    row.decided_by = admin_id
    row.decided_at = _now()
    return _persist(db, row)


def reject_registration(db: Session, row: ToolRegistration, *, admin_id: int, reason: str) -> ToolRegistration:
    """`pending -> rejected`, with a reason its creator will read. Never becomes selectable."""
    _require_pending(row, action="từ chối")
    if not reason.strip():
        raise ToolRegistrationError("Từ chối một kết nối phải kèm lý do")
    row.status = ToolStatus.REJECTED
    row.decided_by = admin_id
    row.decided_at = _now()
    row.rejection_reason = reason
    return _persist(db, row)


def disable_registration(db: Session, row: ToolRegistration, *, admin_id: int) -> ToolRegistration:
    """`approved -> disabled`. Blocks new runs; touches nothing already saved — ADR-0023 §9.

    Deliberately not a stronger rejection, and deliberately not a save-time
    check. A workflow built against this connection while it was approved keeps
    its `tool_id`, keeps loading on the canvas and keeps saving; what changes is
    that `src.workflow.runtime` finds `status: disabled` immediately before
    invoking and records a controlled execution error instead. Re-validating
    every draft against an admin action their creators do not control is the
    alternative the ADR rejected.
    """
    if row.status is not ToolStatus.APPROVED:
        raise ToolRegistrationError(
            f"Chỉ tắt được kết nối đang ở trạng thái approved — kết nối này đang {row.status.value!r}"
        )
    row.status = ToolStatus.DISABLED
    row.decided_by = admin_id
    row.decided_at = _now()
    return _persist(db, row)


class ReviewedAction(IntrospectedAction):
    """One introspected action with everything the platform decided about it.

    What the admin queue reads (criterion 3) and what `definition_of` builds a
    `ToolAction` out of — one derivation, so the risk an admin approves is the
    risk the runtime later enforces. It exists beside `ToolAction` rather than
    being one because an action still missing its mock **cannot be** a
    `ToolAction`: the model refuses to construct (ADR-0023 §4), and refusing to
    construct is exactly wrong for a queue whose job is to show the admin a row
    that is not finished yet.
    """

    risk_level: RiskLevel
    overridden: bool
    sandbox_behavior: SandboxBehavior
    requires_human_approval: bool
    mock_response: dict[str, Any] | None

    @property
    def needs_mock(self) -> bool:
        """Whether a rehearsal has to answer for this action instead of running it."""
        return self.sandbox_behavior is SandboxBehavior.MOCK


def introspected_actions(row: ToolRegistration) -> tuple[IntrospectedAction, ...]:
    """`row.actions` read back through the model that wrote it.

    Re-validated on the way out for the reason `src.workflow.submissions._spec_of`
    re-validates a stored graph: a row written before today's rules must not
    reach the registry unchecked either.
    """
    return tuple(IntrospectedAction.model_validate(action) for action in row.actions)


def reviewed_actions(row: ToolRegistration) -> tuple[ReviewedAction, ...]:
    """Every action of `row` with its risk, its sandbox behaviour and its mock.

    The single place the override is applied, so no caller can read the computed
    risk of an action an admin has already re-graded.
    """
    return tuple(_reviewed(row, action) for action in introspected_actions(row))


def offered_actions(row: ToolRegistration) -> tuple[ReviewedAction, ...]:
    """The actions of `row` a workflow may actually name — `reviewed_actions` past the allowlist.

    Two functions rather than one filtered flag, because the two readers want
    opposite things. A reviewing admin has to see **everything** the server
    reported, including what the creator excluded, or they are approving a
    narrowing they cannot see; the graph validator, the canvas dropdown and the
    runtime must see **only** what is offered, or the platform would let a node
    name an action the connection was never permitted to perform. `reviewed_actions`
    answers the first question and this one answers the second.

    Empty `allowed_actions` means no narrowing, which is why this is not simply
    a set intersection: an intersection with the empty set is empty, and every
    connection registered before the column existed would offer nothing at all.
    """
    allowed = set(row.allowed_actions)
    reviewed = reviewed_actions(row)
    return reviewed if not allowed else tuple(action for action in reviewed if action.name in allowed)


def _reviewed(row: ToolRegistration, action: IntrospectedAction) -> ReviewedAction:
    override = row.risk_overrides.get(action.name)
    risk = RiskLevel(override) if override else risk_of(action.name, action.description, read_only=action.read_only)
    return ReviewedAction(
        **action.model_dump(),
        risk_level=risk,
        overridden=override is not None,
        sandbox_behavior=sandbox_behavior_for(risk),
        # `medium` actions of a registered connection never carry an approval
        # gate: `approval_when_medium` is a judgement about a specific action of
        # a specific connection (ADR-0023 §5), and the review screen's one dial
        # is the risk itself — an admin who wants a person in front of a medium
        # action raises it to `high`, which says so in a word the runtime, the
        # graph validator and the creator all already read.
        requires_human_approval=approval_for(risk, when_medium=False),
        mock_response=row.mock_response_examples.get(action.name),
    )


def definition_of(row: ToolRegistration) -> ToolDefinition:
    """`row` read back as the one `ToolDefinition` shape the built-in half also uses.

    Every consumer downstream — the graph validator, the runtime, the tool
    catalogue — sees no difference between this and `google_workspace`, which is
    ADR-0023 §1's "the registry/DB split is a storage detail, not a schema
    divergence" made literal.

    Only meaningful for a row that has been approved at least once: an action
    that still needs a mock cannot become a `ToolAction`, so a `pending` row
    would raise here. `registered_tool` is the caller that guards for that, and
    it guards by status rather than by catching — a pending connection is not a
    connection that failed to load, it is one nobody may use yet.
    """
    credential = ToolCredential(
        kind=row.credential_kind,
        provider=row.credential_provider,
        scopes=tuple(row.credential_scopes),
        secret_ref=row.credential_secret_ref,
    )
    return ToolDefinition(
        id=registration_id_of(row),
        # Every registered connection is `mcp`: it is registered *by* being an
        # MCP server that answered `tools/list`. Knowledge Search and Web search
        # are node types in this platform (ADR-0021 §2) and have no server to
        # introspect, so there is no path by which a row here is either.
        family=ToolFamily.MCP,
        name=row.name,
        source=ToolSource.CREATOR_REGISTERED,
        status=row.status,
        credential=credential,
        # The allowlist is applied here and only here, which is what makes it
        # reach the canvas dropdown, the graph validator and the runtime at once
        # — all three read `ToolDefinition.actions` and none of them knows a
        # `ToolRegistration` exists.
        actions=tuple(_tool_action(action) for action in offered_actions(row)),
        usage_scope=row.usage_scope,
        invoke=_invoker(name=row.name, server_url=row.server_url, credential=credential),
    )


def _tool_action(action: ReviewedAction) -> ToolAction:
    """One reviewed action as the registry's own `ToolAction`.

    `risk_override` is written unconditionally rather than only when an admin
    changed something: the computed and the reviewed answer agree when nobody
    overrode anything, and passing the reviewed one always is what guarantees
    the two can never be read differently on either side of this line.
    """
    return ToolAction(
        name=action.name,
        # `tools/list` allows a tool with no description and `ToolAction` does
        # not: a nameless effect is still an effect, and the creator's own
        # connection name is the most truthful thing available to say about it.
        description=action.description or action.name,
        input_schema=action.input_schema,
        output_schema=action.output_schema,
        read_only=action.read_only,
        mock_response_example=action.mock_response,
        risk_override=action.risk_level,
    )


def _invoker(*, name: str, server_url: str, credential: ToolCredential) -> ToolInvoker:
    """How this connection actually performs one of its actions.

    Real, and honest about the half that does not exist yet — `src.workflow.tools`'
    own pattern. A connection needing no credential is called for real over MCP;
    one with a `SECRET_REFERENCE` resolves it through the secret store
    (RAG-19 / ADR-0045) and is called for real too. One authenticating per
    end-user still refuses: it needs `UserConnection`, which ADR-0023's
    Consequences leave to a later ticket, and refusing names what is missing
    rather than calling the server unauthenticated and reporting the server's
    401 as if the creator's registration were at fault.

    Closes over the frozen `ToolCredential` and two strings rather than over the
    `ToolRegistration` row, because `registered_tool` builds this inside a
    session it then closes — a closure holding a detached ORM instance is a lazy
    load waiting to happen in the middle of a run.
    """

    def invoke(action: str, args: Mapping[str, Any]) -> str:
        if credential.kind is CredentialKind.OAUTH_PER_USER:
            raise ToolUnavailableError(
                f"Kết nối {name!r} xác thực theo từng người dùng ({credential.provider or 'OAuth'}) "
                f"nên chưa chạy thật được action {action!r} — cần một UserConnection"
            )
        if credential.kind is CredentialKind.SECRET_REFERENCE:
            secret = _resolved_secret(name=name, secret_ref=credential.secret_ref, action=action)
            return call_action(server_url, action, args, secret=secret)
        if credential.kind is CredentialKind.CUSTOM_HEADERS:
            stored = _resolved_secret(name=name, secret_ref=credential.secret_ref, action=action)
            return call_action(server_url, action, args, headers=_headers_from(stored))
        return call_action(server_url, action, args)

    return invoke


def _resolved_secret(*, name: str, secret_ref: str, action: str) -> str:
    """The plaintext value `secret_ref` names, or a refusal distinct from the old "no secret store" one.

    Opens its own session, `registered_tool`'s reason: `invoke` runs on
    whatever thread the runtime schedules it on, long after the request that
    built this closure has ended. ADR-0045's Consequences draw the line this
    error keeps: the old message meant "there is no store to ask"; this one
    means the store was asked and `secret_ref` names nothing in it — a
    creator's or an operator's mistake, not a platform gap.
    """
    with SessionLocal() as session:
        try:
            return EncryptedDbSecretStore(db=session).get(secret_ref)
        except SecretNotFoundError:
            raise ToolUnavailableError(
                f"Kết nối {name!r} cần bí mật {secret_ref!r} nhưng secret store không có khoá này "
                f"— chưa chạy thật được action {action!r}"
            ) from None


def registered_tool(tool_id: str) -> ToolDefinition | None:
    """The registered connection `tool_id` names, or `None` — `tool_definition`'s DB half.

    Opens its own short-lived session because its callers are the graph
    validator and the workflow runtime, neither of which is handed one: a run in
    progress has a `WorkflowRunContext`, not a request. The read is one row by
    primary key, and only for an id of the registered form — a built-in id or a
    `tool_id` nobody registered never reaches the database at all.

    `pending` and `rejected` rows answer `None`, which is criterion 2: a
    connection nobody approved is not merely unusable, it is *not there* as far
    as the canvas, the Generator and the validator are concerned.
    `approved` and `disabled` both answer with the definition, because ADR-0023
    §9 wants the runtime to refuse a disabled tool by naming its status — a
    disabled connection that reported itself missing would tell a creator their
    workflow references a tool that does not exist, when what happened is that
    an admin turned theirs off.
    """
    registration_id = _registration_id(tool_id)
    if registration_id is None:
        return None
    with SessionLocal() as session:
        row = session.get(ToolRegistration, registration_id)
        if row is None or row.status in (ToolStatus.PENDING, ToolStatus.REJECTED):
            return None
        return definition_of(row)


def approved_registrations(db: Session, *, for_creator: int) -> Sequence[ToolDefinition]:
    """Every registered connection `for_creator` may currently point an `MCP` node at — criterion 5.

    Read fresh on every call rather than cached, for `approved_submission_specs`'
    reason: a connection an admin just disabled must stop being offered on the
    very next request, not eventually.

    Scoped by the viewer, because `usage_scope` (ADR-0023 §1) is a field about
    *who may wire this connection in* and a list that ignored it would make it
    decorative. `creator_only` is filtered in SQL to its submitter.
    `org_restricted` is deliberately treated as `public` here and not silently
    dropped: this platform has no organisation model to restrict against, so
    honouring it would mean inventing one — the gap is recorded in ADR-0038's
    Consequences rather than hidden behind a filter that looks like it works.
    """
    statement = select(ToolRegistration).where(
        ToolRegistration.status == ToolStatus.APPROVED,
        (ToolRegistration.usage_scope != UsageScope.CREATOR_ONLY) | (ToolRegistration.submitted_by == for_creator),
    )
    return [definition_of(row) for row in db.execute(statement).scalars().all()]


def _registration_id(tool_id: str) -> int | None:
    """The row id `registration_id_of` encoded, or `None` if `tool_id` is not one of ours."""
    if not tool_id.startswith(_ID_PREFIX):
        return None
    try:
        return int(tool_id[len(_ID_PREFIX) :])
    except ValueError:
        return None


def _require_pending(row: ToolRegistration, *, action: str) -> None:
    if row.status is not ToolStatus.PENDING:
        raise ToolRegistrationError(
            f"Chỉ {action} được kết nối đang chờ duyệt (pending) — kết nối này đang {row.status.value!r}"
        )


def _now() -> datetime:
    """A naive UTC timestamp, matching every other `decided_at` column in this codebase."""
    return datetime.now(UTC).replace(tzinfo=None)


def _persist(db: Session, row: ToolRegistration) -> ToolRegistration:
    db.commit()
    db.refresh(row)
    return row
