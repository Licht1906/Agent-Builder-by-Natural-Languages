"""UC1-10 (#124) — every tool call the compiled graph can make is a `BoundTool`,
and the only way to run one is `execute_tool_call`: ask the PEP first, run the
real implementation only on `allow`. A tool node built any other way could
construct a call that skips the PEP — this module is what makes that
impossible to write by accident, the same "runtime is a dumb executor"
argument `authz.rego`'s own header makes.

UC1-11 (#125) rides on the same function: every `scope: read` call's result
sets `provenance.from_untrusted_data = True` for the state update it returns,
because *any* content this agent reads — a database row, a document passage —
may carry an injected instruction (§10.3). `execute_tool_call` never inspects
what the tool returned to decide this; the scope alone decides it, which is
what makes the rule impossible for a convincing-sounding row to talk its way
around.
"""

import hashlib
import json
import smtplib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

import psycopg
from psycopg import sql

from src.agentspec.spec import AgentSpec, Scope
from src.compiler.state import EMPTY_PROVENANCE, ProvenanceState, SessionState
from src.config import get_settings
from src.policy.pep import Action, Approval, AuditSink, AuthzRequest, Effect, PEPClient, Principal, Provenance
from src.policy.pep import Session as PEPSession
from src.services.secret_store import SecretUnreadableError
from src.services.uc1_tracing import log_decision


@dataclass(frozen=True, slots=True)
class RowScope:
    """The three GUCs `schema.sql` §9.2's RLS reads, for exactly one tool call.

    UC1-20 (#136), FR-41: sourced from the *caller's own* identity
    (`ctx.principal.employee_id`/`department` — the person asking right now,
    never `metadata.created_by`) plus the PEP's own `row_filter.scope`
    decision, never re-derived by the implementation itself. An
    implementation that trusted its own judgment about scope instead of this
    struct could reach around the PEP for authorization the same way
    `execute_tool_call`'s own docstring already rules out reaching around it
    for identity — this struct is what makes that impossible: it is built
    once, in `execute_tool_call`, from the decision that already came back
    `allow`.
    """

    principal_id: str
    department: str
    scope: str


#: What a tool implementation actually does, given its call's arguments and
#: the row scope the PEP's decision authorised for this call (UC1-20 / #136).
#: Mock implementations for UC-1's five catalogue tools live in
#: `default_tool_implementations` below.
ToolImplementation = Callable[[Mapping[str, Any], RowScope], Any]


class ToolDeniedError(RuntimeError):
    """The PEP returned `deny` for this call. Carries every reason `authz.rego` gave."""

    def __init__(self, tool: str, reasons: Sequence[str]) -> None:
        self.tool = tool
        self.reasons = list(reasons)
        super().__init__(f"tool {tool!r} bị PEP từ chối: {'; '.join(self.reasons) or '(không có lý do)'}")


class ApprovalRequiredError(RuntimeError):
    """The PEP returned `require_approval` — a human, not this graph, decides next."""

    def __init__(self, tool: str, approver_roles: Sequence[str], action_hash: str) -> None:
        self.tool = tool
        self.approver_roles = list(approver_roles)
        self.action_hash = action_hash
        super().__init__(f"tool {tool!r} cần duyệt bởi {self.approver_roles} (action_hash={action_hash})")


@dataclass(frozen=True, slots=True)
class BoundTool:
    """One `capabilities[]` entry, bound to a real callable and the connection it runs against."""

    name: str
    scope: Scope
    connection_id: str
    implementation: ToolImplementation


def hash_action(tool: str, args: Mapping[str, Any]) -> str:
    """FR-45's `action_hash` — a stable digest of exactly the payload a human approves.

    Changing so much as one value in `args` after approval changes this hash,
    which is what makes `authz.rego`'s `a.action_hash == input.action.hash`
    check refuse a payload that was edited after the fact. `sort_keys=True` so
    the same args in a different dict order still hash the same.
    """
    canonical = json.dumps({"tool": tool, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    """What one `execute_tool_call` produced — the tool's own result, plus the
    provenance/session *deltas* the caller (the graph's tools node) merges
    into state via `src.compiler.state`'s reducers.
    """

    result: Any
    provenance_update: ProvenanceState
    session_update: SessionState
    action_hash: str
    #: The PEP's own `row_filter` for this call — UC1-30 (#131) reads it straight
    #: off the transcript step this becomes, rather than re-deriving `data_scope`.
    row_filter: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCallContext:
    """Everything about *the call site* `execute_tool_call` needs, bundled into one argument.

    Not `AuthzRequest` (`src.policy.pep`) itself: that dataclass also needs
    `action`, which does not exist until `execute_tool_call` has `bound`/`args`
    in hand to hash and build it — this is what the call site already knows
    *before* that point. Bundling these five was the code review's own fix
    for the "3 parameters" rule (`docs/code-style/python.md` §2) this
    function's flattened form broke, the same move
    `src.workflow.runtime.compile_workflow_graph` already makes with
    `WorkflowRunContext`.
    """

    policy_spec: AgentSpec
    principal: Principal
    provenance: ProvenanceState
    session: SessionState
    pep_client: PEPClient
    #: UC1-32 (#144): called with every decision `pep_client` returns —
    #: allow, deny, and require_approval alike — before `execute_tool_call`
    #: branches on `decision.effect`. Required, not defaulted to a no-op the
    #: way `approvals` defaults to `()`: a call site that forgot to wire an
    #: `AuditSink` would silently produce PEP decisions with no audit trail,
    #: exactly the "code path bỏ qua ghi log" UC1-32 rules out — so this
    #: dataclass simply cannot be constructed without one, the same
    #: protection `pep_client` itself already has.
    audit_sink: AuditSink
    approvals: Sequence[Approval] = ()
    #: UC1-31 (#143): the turn-level id `AgentState.request_id` carries —
    #: `""` only for call sites (mostly tests) that build a `ToolCallContext`
    #: outside of a graph turn and do not care what request an audit row
    #: joins the structured log by.
    request_id: str = ""


def execute_tool_call(bound: BoundTool, args: Mapping[str, Any], ctx: ToolCallContext) -> ToolCallOutcome:
    """Ask the PEP for `bound`, then run it for real only if the answer is `allow`.

    Raises `ToolDeniedError`/`ApprovalRequiredError` rather than returning a
    sentinel — a caller that forgets to check a return value cannot
    accidentally treat a denial as a result, which a `None`/falsy return could
    invite.
    """
    action_hash = hash_action(bound.name, args)
    request = AuthzRequest(
        spec=ctx.policy_spec,
        principal=ctx.principal,
        action=Action(
            hash=action_hash,
            tool=bound.name,
            scope=bound.scope,
            mode="execute",
            connection_id=bound.connection_id,
            args=dict(args),
        ),
        provenance=Provenance(
            from_untrusted_data=ctx.provenance["from_untrusted_data"], sources=list(ctx.provenance["sources"])
        ),
        session=PEPSession(
            tool_calls_used=ctx.session["tool_calls_used"], per_tool_calls=dict(ctx.session["per_tool_calls"])
        ),
        approvals=list(ctx.approvals),
    )
    decision = ctx.pep_client(request)
    # UC1-32 (#144): every decision the PEP returns gets audited here, before
    # this function branches on `decision.effect` — the one place every real
    # tool call passes through (this module's own docstring), so there is no
    # second call site that could reach a deny/require_approval/allow branch
    # without this line having already run for it.
    ctx.audit_sink(ctx.request_id, request, decision)
    # UC1-31 (#143): tickets.md gives UC1-31 and UC1-32 the identical "sau mỗi
    # lời gọi PEP" wording — the structured log line for this decision is
    # written from the same call, right next to the audit write it joins by
    # `request_id` (`src.services.uc1_tracing`'s own module docstring).
    log_decision(ctx.request_id, request, decision)

    if decision.effect is Effect.DENY:
        raise ToolDeniedError(bound.name, decision.reasons)
    if decision.effect is Effect.REQUIRE_APPROVAL:
        raise ApprovalRequiredError(bound.name, decision.approver_roles, action_hash)

    row_scope = RowScope(
        principal_id=ctx.principal.employee_id,
        department=ctx.principal.department,
        scope=str(decision.row_filter.get("scope", "none")),
    )
    result = bound.implementation(args, row_scope)

    # §10.3: *every* successful read marks the turn untrusted, unconditionally
    # on the tool's own scope — never on what it happened to return.
    provenance_update = (
        {"from_untrusted_data": True, "sources": [bound.name]} if bound.scope is Scope.READ else EMPTY_PROVENANCE.copy()
    )
    session_update: SessionState = {"tool_calls_used": 1, "per_tool_calls": {bound.name: 1}}
    return ToolCallOutcome(
        result=result,
        provenance_update=provenance_update,
        session_update=session_update,
        action_hash=action_hash,
        row_filter=dict(decision.row_filter),
    )


#: schema.sql §5's own pre-seeded role — created by migration, never by this
#: platform's deploy step, so it exists before any version is ever approved,
#: which is what lets sandbox chat (FR-36, always pre-approval) query real
#: data. Deliberately *not* the per-`metadata.id` role `src.agentspec.roles`
#: provisions at deploy time (UC1-18 / #134): that role's whole purpose is
#: FR-34/FR-35's lifecycle (created on deploy, revoked on suspend), which a
#: sandbox turn run against a draft version must not depend on existing yet.
#: The default for `default_tool_implementations`' own `erp_readonly_role`
#: parameter — every caller that has no deployed-agent role to use yet
#: (sandbox chat, the golden-set runner, action-approval's second call) keeps
#: querying through this one. UC1-36 (#148)'s production chat door
#: (`src.api.uc1_agent_chat_routes`) is the one caller that overrides it with
#: the deployed spec's own `metadata.id`, which is what makes FR-35's suspend
#: actually stop that agent's own reads rather than only the shared demo role.
_ERP_READONLY_ROLE = "agt_ar_assistant_ro"

#: A hard ceiling this tool applies on its own, independent of whatever a
#: capability's `constraints.max_rows` says (that enforcement is the PEP's
#: territory, not this function's) — just enough so one call can never pull
#: back an unbounded result set.
_MAX_ROWS = 500

#: How many rows of a result actually get shown to the model, as opposed to
#: fetched. `_MAX_ROWS` above is a safety ceiling on the *query*; this is a
#: separate limit for a separate reason, and conflating them broke both.
#:
#: `AGENT_MODEL` holds 32k tokens. A manager's department-scoped receivables
#: query returns ~170 rows of eight columns, which is ~17k tokens of JSON before
#: the model has read a word of the question — enough that the provider refused
#: the request outright ("max_tokens is too large: this model's maximum context
#: length is 32768 and your request has 17084 input tokens"). The turn died with
#: a 400, which is the *good* case; a slightly smaller result would instead have
#: quietly crowded out the conversation.
#:
#: `row_count` is reported from the full result, never from the truncated list,
#: because the questions this agent is actually asked are counting questions
#: ("cả nhóm có bao nhiêu khách quá hạn?"). Handing a 7B model 170 rows and
#: asking it to count is a worse answer than handing it the number: before this
#: limit existed the model was reading a long list and reporting the length of
#: however much of it it had attended to.
_MODEL_ROW_BUDGET = 40

_CONNECT_TIMEOUT_SECONDS = 5


def _within_model_budget(rows: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
    """A query result trimmed to what the model can actually read, with the true
    total kept alongside it so a count is never inferred from the trimmed list.
    """
    if len(rows) <= _MODEL_ROW_BUDGET:
        return {"rows": rows, "source": source, "row_count": len(rows)}
    return {
        "rows": rows[:_MODEL_ROW_BUDGET],
        "source": source,
        "row_count": len(rows),
        "truncated": True,
        "note": (
            f"chỉ liệt kê {_MODEL_ROW_BUDGET} dòng đầu để vừa ngữ cảnh; "
            f"row_count ({len(rows)}) mới là tổng số dòng thực tế trong phạm vi được phép"
        ),
    }


def _positive_int(value: Any) -> int | None:
    """`args`' own `days_overdue_gt` (or any similarly-shaped arg), read defensively.

    A tool call's `args` come from a model's function call — untyped, and never
    trusted to be the type the name implies. `None` for anything that is not a
    non-negative integer, which every caller here reads as "no filter", the same
    fail-open-to-"show everything-allowed" default `v_ar_open` itself has with no
    `WHERE` clause at all.

    A string of digits counts, and that is not laxness — it is the other half of
    `TOOL_SCHEMAS` declaring these arguments as `"string"` rather than
    `"integer"`. See that table's own note on why the wire type had to change;
    the value still has to *be* a non-negative integer to survive this function,
    a string that is not one is refused exactly like a list or a `bool` is.
    """
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.isdigit() else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _non_empty_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _rows_of(cur: Any) -> list[dict[str, Any]]:
    columns = [column.name for column in cur.description]
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _unreadable(source: str, reason: str) -> dict[str, Any]:
    """A read that never happened — deliberately *without* a `rows` key.

    The model is the only reader of this dict, and it has no way to know
    whether a key is absent because nothing matched or because nothing was
    asked. `{"rows": [], "note": ...}` — what a missing credential used to
    return — reads exactly like a query that matched nothing, and a real turn
    did the predictable thing with it: told the user "Hiện tại không có khách
    hàng nào ... quá hạn hơn 30 ngày. (nguồn: v_ar_open)" while `v_ar_open`
    held 231 rows and the credential had simply not resolved. §11.1's
    `citation_required` passed it, because the sentence names a source; that
    guardrail checks for a citation, never that a read occurred.

    So the failure shapes omit `rows` entirely, leaving nothing to count.
    A genuinely empty answer still carries `rows: []` — `_query_view`'s
    RLS-scoped-to-nothing case (§9.2's fail-closed `app_scope()`), which is a
    fact about the data and must stay distinguishable from this.
    """
    return {"source": source, "error": reason}


#: What each catalogue tool tells the model it does and what it accepts — the
#: `function` half of the OpenAI tool spec `src.compiler.graph._tool_spec` puts
#: on the wire.
#:
#: **Here, beside the implementations, and not in the compiler**, because the two
#: have to agree and only adjacency makes that checkable. `_tool_spec` used to
#: build `{"properties": {}, "additionalProperties": True}` for every tool with
#: the description `"UC-1 tool (read)"`, deferring the real shapes to UC1-19/21
#: — and those tickets then landed the implementations below, which read
#: `days_overdue_gt`/`customer_id`/`assignee_id`, without the advertisement ever
#: catching up. A model told only that five identically-described tools take
#: "any object" does what the golden set caught it doing: it invents plausible
#: argument names (`status`, `days`, `where_clause`, `overdue_days`), the reader
#: below finds none of them, and the filter is silently dropped — an answer
#: about *every* open receivable presented as an answer about the overdue ones.
#: `test_every_tool_advertises_exactly_the_arguments_it_reads` is what now keeps
#: the two halves in step.
#:
#: **Numbers are advertised as `"string"`, deliberately.** `AGENT_MODEL` is
#: served on OpenRouter by exactly one provider that supports tool calls at all
#: (Phala; the other, Together, does not), and that provider truncates *numeric*
#: argument values as it serialises them — `{"days_overdue_gt": 30}` arrives as
#: the unterminated `{"days_overdue_gt": 3`, which no JSON parser can read and
#: `repair_tool_call_arguments` can only drop, ending the turn with no answer at
#: all. String values come back whole. Since there is no second provider to
#: route to, the wire type is the only thing left to change; `_positive_int`
#: absorbs it and the model is told plainly to write digits. Revisit if
#: OpenRouter ever lists another tool-capable endpoint for this model.
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "erp.query_receivables": {
        "description": (
            "Tra cứu các đơn hàng công nợ đang mở trong view v_ar_open. Trả về order_id, customer_id, "
            "sales_rep_id, department, amount_due, due_date, days_overdue, order_status — đã sắp xếp "
            "theo số ngày quá hạn giảm dần."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days_overdue_gt": {
                    "type": "string",
                    "description": (
                        'Chỉ lấy các đơn quá hạn NHIỀU HƠN số ngày này, viết bằng chữ số, ví dụ "30". '
                        "Bỏ trống để lấy toàn bộ công nợ đang mở."
                    ),
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "erp.query_customer": {
        "description": (
            "Tra cứu thông tin khách hàng trong view v_customer_safe. Trả về customer_id, customer_name, "
            "sales_rep_id, department, contact_email, city, state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "Mã khách hàng cần tra cứu. Bỏ trống để liệt kê các khách hàng trong phạm vi.",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "kb.search": {
        "description": "Tìm trong kho tài liệu nội bộ về quy trình thu hồi công nợ.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Câu truy vấn cần tìm."}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "mail.send_reminder": {
        "description": (
            "Gửi email nhắc nợ cho một khách hàng. Cần người có thẩm quyền phê duyệt trước khi thực sự gửi. "
            "Địa chỉ email được hệ thống tự phân giải từ customer_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "Mã khách hàng cần nhắc nợ."},
                "amount_due": {
                    "type": "string",
                    "description": 'Số tiền còn nợ, viết bằng chữ số, ví dụ "1813.32".',
                },
            },
            "required": ["customer_id"],
            "additionalProperties": False,
        },
    },
    "task.create": {
        "description": "Tạo một công việc theo dõi thu hồi công nợ. Cần người có thẩm quyền phê duyệt trước khi tạo.",
        "parameters": {
            "type": "object",
            "properties": {
                "assignee_id": {
                    "type": "string",
                    "description": "Mã nhân viên được giao việc. Bỏ trống để giao cho chính người đang hỏi.",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}


def default_tool_implementations(
    *, resolve_credential: Callable[[str], str], erp_readonly_role: str = _ERP_READONLY_ROLE
) -> dict[str, ToolImplementation]:
    """UC-1's five catalogue tools. `erp.query_receivables`/`erp.query_customer`
    (UC1-19 / #135, FR-40) run a real, cited query against `v_ar_open`/
    `v_customer_safe`, scoped to the caller's own `RowScope` (UC1-20 / #136,
    FR-41). `mail.send_reminder`/`task.create` (UC1-21 / #137, FR-42/FR-43)
    only ever run here *after* `execute_tool_call` has already seen `allow` —
    for these two, that is only ever true on the second, post-approval call
    `src.api.uc1_action_approval_routes` makes, never on the first one a
    turn's own tool call makes (`capabilities[].requires_approval: true`, no
    exception in `authz.rego`'s matrix for either tool) — so "don't send
    real SMTP/create a real task until a human approves" is a structural fact
    about when this function is reached, not a check either closure below has
    to make itself.

    Every read here queries under `erp_readonly_role`, with `app.principal_id`/
    `app.department`/`app.scope` set from `scope` — the `RowScope`
    `execute_tool_call` built from the *caller's own* identity and the PEP's
    own `row_filter` decision — in the same transaction as the query
    (`schema.sql` §9.2). This function never reads `scope` to make its own
    authorization decision; it only forwards it to Postgres, which is what
    keeps "who may see which rows" answerable by RLS alone.

    `erp_readonly_role` defaults to `_ERP_READONLY_ROLE`, schema.sql §5's
    pre-seeded role every sandbox turn and golden-set run still queries
    through (neither has a deployed agent's own role yet to use). UC1-36
    (#148)'s production chat door (`src.api.uc1_agent_chat_routes`) passes
    the deployed spec's own `metadata.id` instead — the role
    `src.agentspec.roles.provision_agent_role` grants at deploy and
    `suspend_agent_role` revokes at suspend — which is what makes FR-35's
    kill switch actually stop that agent's own reads, not merely the shared
    demo role every other caller still uses.
    """

    def _query_receivables(args: Mapping[str, Any], scope: RowScope) -> dict[str, Any]:
        try:
            dsn = resolve_credential("conn/erp_readonly")
        except SecretUnreadableError as exc:
            return _unreadable("v_ar_open", f"không đọc được credential ERP từ kho secret: {exc}")
        if not dsn:
            return _unreadable("v_ar_open", "chưa có kết nối ERP nào được cấu hình")
        min_days_overdue = _positive_int(args.get("days_overdue_gt"))
        query = (
            "SELECT order_id, customer_id, sales_rep_id, department, amount_due, "
            "due_date, days_overdue, order_status FROM v_ar_open"
        )
        params: list[Any] = []
        if min_days_overdue is not None:
            query += " WHERE days_overdue > %s"
            params.append(min_days_overdue)
        query += " ORDER BY days_overdue DESC LIMIT %s"
        params.append(_MAX_ROWS)
        return _query_view(dsn, query, params, source="v_ar_open", scope=scope)

    def _query_customer(args: Mapping[str, Any], scope: RowScope) -> dict[str, Any]:
        try:
            dsn = resolve_credential("conn/erp_readonly")
        except SecretUnreadableError as exc:
            return _unreadable("v_customer_safe", f"không đọc được credential ERP từ kho secret: {exc}")
        if not dsn:
            return _unreadable("v_customer_safe", "chưa có kết nối ERP nào được cấu hình")
        customer_id = _non_empty_str(args.get("customer_id"))
        query = "SELECT customer_id, customer_name, sales_rep_id, department, contact_email, city, state FROM v_customer_safe"
        params: list[Any] = []
        if customer_id is not None:
            query += " WHERE customer_id = %s"
            params.append(customer_id)
        query += " LIMIT %s"
        params.append(_MAX_ROWS)
        return _query_view(dsn, query, params, source="v_customer_safe", scope=scope)

    def _query_view(dsn: str, query: str, params: Sequence[Any], *, source: str, scope: RowScope) -> dict[str, Any]:
        try:
            with (
                psycopg.connect(dsn, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(erp_readonly_role)))
                # §9.2: all three GUCs, every time — `app_scope()` fails closed to
                # `'none'` (zero rows) whenever one of these is somehow skipped, so
                # there is no branch here that queries without setting them.
                cur.execute(sql.SQL("SET LOCAL app.principal_id = {}").format(sql.Literal(scope.principal_id)))
                cur.execute(sql.SQL("SET LOCAL app.department = {}").format(sql.Literal(scope.department)))
                cur.execute(sql.SQL("SET LOCAL app.scope = {}").format(sql.Literal(scope.scope)))
                cur.execute(query, params)  # noqa: S608 — query text is a fixed literal, never built from args
                rows = _rows_of(cur)
        except (psycopg.Error, OSError) as exc:
            return _unreadable(source, f"không truy vấn được ERP: {exc}")
        return _within_model_budget(rows, source=source)

    def _kb_search(args: Mapping[str, Any], scope: RowScope) -> dict[str, Any]:
        return {"passages": [], "note": "stub — knowledge retrieval wiring is a separate ticket"}

    def _send_reminder(args: Mapping[str, Any], scope: RowScope) -> dict[str, Any]:
        """FR-42: real SMTP (§16.2's MailHog mock), reached only on the
        post-approval execution `src.api.uc1_action_approval_routes` makes —
        never from the first, `require_approval` call `execute_tool_call`
        raised out of before this closure was ever entered.

        `customer_id` resolves to a synthetic address server-side (schema.sql
        §4's own comment: "the mail tool resolves the real address
        server-side... the LLM never needs it") — Olist has no real customer
        email column, so this is the same honest synthetic construction
        `v_customer_safe.contact_email`'s masking already stands in for.
        """
        resolve_credential("conn/smtp_internal")
        customer_id = _non_empty_str(args.get("customer_id")) or "unknown"
        amount_due = args.get("amount_due")
        to = f"{customer_id}@customer.internal"
        settings = get_settings()
        if not settings.smtp_host:
            return {"sent": False, "to": to, "note": "SMTP_HOST chưa cấu hình — không gửi được"}
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = to
        message["Subject"] = "Nhắc nhở công nợ quá hạn"
        message.set_content(
            f"Kính gửi khách hàng {customer_id},\n\n"
            f"Quý khách còn khoản công nợ {amount_due if amount_due is not None else ''} chưa thanh toán. "
            "Vui lòng liên hệ để được hỗ trợ.\n"
        )
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            if settings.smtp_username:
                smtp.starttls()
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return {"sent": True, "to": to}

    def _create_task(args: Mapping[str, Any], scope: RowScope) -> dict[str, Any]:
        """FR-43: §16.2's "REST stub" — enough to prove the approval flow ran
        for real, not a real task-tracker integration. Reached only on the
        post-approval execution path for the same structural reason
        `_send_reminder` is.
        """
        resolve_credential("conn/tasktracker")
        assignee_id = _non_empty_str(args.get("assignee_id")) or scope.principal_id
        return {
            "created": True,
            "task_id": f"task-{hash_action('task.create', dict(args))[:12]}",
            "assignee_id": assignee_id,
            "note": "REST stub (§16.2) — chứng minh luồng approval, không phải tích hợp task tracker thật",
        }

    return {
        "erp.query_receivables": _query_receivables,
        "erp.query_customer": _query_customer,
        "kb.search": _kb_search,
        "mail.send_reminder": _send_reminder,
        "task.create": _create_task,
    }
