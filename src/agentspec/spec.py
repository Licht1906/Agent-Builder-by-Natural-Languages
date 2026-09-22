"""UC-1's `AgentSpec` — the one artifact the whole platform compiles and executes.

A direct transcription of `docs/usecase_spec/platform/agentspec.schema.json`
(ADR-0046). The JSON Schema stays the published contract — the meta-agent is
handed it verbatim in its prompt — and this module is the copy that actually runs,
so the two must be read together whenever either changes.

Two properties are load-bearing rather than decorative:

- `extra="forbid"` on every model. A spec is a permission grant; a field the
  platform does not understand is a permission nobody reviewed. The schema says
  `additionalProperties: false` at every level and so does this.
- No free-form expression anywhere. `Condition` is `{field, op, value}` with a
  closed operator set (§7.1) precisely so the policy engine never evaluates a
  string an LLM wrote.

This is *not* `src/models/agent_spec.py` — that is V2's spec (interview slots,
steps, per-step models), which ADR-0046 supersedes as a direction but leaves
running. The two names collide; the artifacts have nothing in common.
"""

import re
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

API_VERSION = "agentbuilder/v1"
KIND = "AgentSpec"

TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"


class SpecStatus(StrEnum):
    """Where a version sits in its lifecycle (§13).

    Set by the platform, never by the meta-agent — a draft coming back from the
    LLM is forced to `draft` whatever it claims. The transition table that makes
    this an actual state machine is UC1-04's; this enum is the vocabulary it will
    enforce over.
    """

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    DEPLOYED = "deployed"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class DataScope(StrEnum):
    """How wide the caller's data window is.

    Compiles into a mandatory row filter applied with the identity of whoever is
    *talking to* the agent, not the identity of whoever created it. Two reps
    asking the same agent the same question see two different answers, and that
    is the whole demo (§10, C2).
    """

    OWN_RECORDS = "own_records"
    DEPARTMENT = "department"
    ALL = "all"


class Scope(StrEnum):
    READ = "read"
    WRITE = "write"


class Autonomy(StrEnum):
    """How far the agent may act without a human in the loop."""

    READ_ONLY = "read_only"
    PROPOSE = "propose"
    PROPOSE_THEN_ACT = "propose_then_act"
    AUTONOMOUS = "autonomous"


class ConditionOp(StrEnum):
    """The complete operator set OPA will evaluate. Deliberately small (§7.1)."""

    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"


class InputGuardrail(StrEnum):
    PROMPT_INJECTION_DETECTOR = "prompt_injection_detector"
    PII_REDACTOR = "pii_redactor"
    TOPIC_CLASSIFIER = "topic_classifier"
    LANGUAGE_FILTER = "language_filter"


class OutputGuardrail(StrEnum):
    PII_MASKER = "pii_masker"
    CITATION_REQUIRED = "citation_required"
    CONFIDENCE_ROUTER = "confidence_router"
    NO_SPECULATION = "no_speculation"
    GROUNDING_CHECK = "grounding_check"


class Fallback(StrEnum):
    ESCALATE_TO_HUMAN = "escalate_to_human"
    REFUSE = "refuse"
    ANSWER_WITH_DISCLAIMER = "answer_with_disclaimer"


class ConnectionType(StrEnum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    MSSQL = "mssql"
    REST = "rest"
    SMTP = "smtp"
    S3 = "s3"
    FILESYSTEM = "filesystem"


class Trace(StrEnum):
    LANGFUSE = "langfuse"
    NONE = "none"


class EvalMetric(StrEnum):
    FAITHFULNESS = "faithfulness"
    ANSWER_RELEVANCY = "answer_relevancy"
    CONTEXTUAL_PRECISION = "contextual_precision"
    REFUSAL_CORRECTNESS = "refusal_correctness"
    LEAK_CHECK = "leak_check"


class SpecModel(BaseModel):
    """Shared configuration: nothing the platform cannot name may appear in a spec."""

    model_config = ConfigDict(extra="forbid")


class Metadata(SpecModel):
    id: str = Field(
        ...,
        pattern=r"^agt_[a-z0-9_]{3,48}$",
        description="Cũng là tên PostgreSQL role và tên container, nên phải ổn định",
    )
    name: str = Field(..., min_length=3, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    version: Annotated[int, Field(ge=1)]
    status: SpecStatus
    created_by: str | None = None
    approved_by: list[str] = Field(
        default_factory=list,
        description="Số người duyệt phải đạt authority.approval_quorum trước khi sang 'approved'",
    )
    parent_version: Annotated[int, Field(ge=1)] | None = None


class Audience(SpecModel):
    roles: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")]] = Field(..., min_length=1)
    departments: list[str] = Field(
        default_factory=list,
        description="Rỗng hoặc vắng mặt nghĩa là không giới hạn phòng ban",
    )
    data_scope: DataScope

    @model_validator(mode="after")
    def _roles_must_be_distinct(self) -> "Audience":
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("`roles` có vai trò lặp lại")
        return self

    @model_validator(mode="after")
    def _departments_must_be_distinct(self) -> "Audience":
        if len(set(self.departments)) != len(self.departments):
            raise ValueError("`departments` có phòng ban lặp lại")
        return self


class Constraints(SpecModel):
    max_rows: Annotated[int, Field(ge=1, le=10_000)] | None = None
    max_calls_per_session: Annotated[int, Field(ge=1)] | None = None
    allowed_columns: list[str] = Field(default_factory=list)


class Capability(SpecModel):
    """One entry of the tool allow-list. Anything not listed here is denied."""

    tool: str = Field(
        ...,
        pattern=TOOL_NAME_PATTERN,
        description="Tên đầy đủ trong tool catalog của nền tảng; tên không có thật bị compiler từ chối",
    )
    scope: Scope
    requires_approval: bool = False
    constraints: Constraints | None = None


class Prohibitions(SpecModel):
    """Explicit negative space. Evaluated before capabilities — a deny here wins."""

    never_expose: list[str] = Field(
        default_factory=list,
        description="Tên cột bị che khỏi mọi kết quả tool trước khi LLM nhìn thấy, và che lần hai trên đường ra",
    )
    deny_tools: list[str] = Field(default_factory=list, description="Glob khớp với tên tool, ví dụ erp.update_*")
    deny_topics: list[str] = Field(default_factory=list)
    external_egress: bool = Field(
        ...,
        description="false biên dịch thành NetworkPolicy deny-all, allow-list dựng từ connections[] và LLM gateway",
    )


class Condition(SpecModel):
    """A structured predicate OPA evaluates — never a free-form expression (§7.1)."""

    field: str = Field(..., description="Đường dẫn chấm vào tham số của tool, ví dụ assignee_id")
    op: ConditionOp
    value: Any = Field(
        ...,
        description="Literal, hoặc một trong các tham chiếu: $principal.employee_id, $principal.department, $principal.sub",
    )


class ApprovalRule(SpecModel):
    approver_roles: list[str] = Field(..., min_length=1)
    condition: Condition | None = None
    sla_minutes: Annotated[int, Field(ge=1)] = 240


class SpecBudget(SpecModel):
    max_tool_calls_per_session: Annotated[int, Field(ge=1, le=200)]
    max_cost_usd_per_day: Annotated[float, Field(ge=0)] | None = None
    max_sessions_per_user_per_day: Annotated[int, Field(ge=1)] | None = None


class Authority(SpecModel):
    autonomy: Autonomy
    approval_quorum: Annotated[int, Field(ge=1, le=3)] = 1
    approval_matrix: dict[str, ApprovalRule] = Field(
        default_factory=dict,
        description="Duyệt theo từng tool lúc chạy. Khoá là tên tool.",
    )
    budget: SpecBudget

    @model_validator(mode="after")
    def _approval_matrix_keys_must_be_tool_names(self) -> "Authority":
        """The schema constrains these keys with `propertyNames`; Pydantic does not.

        A key that is not a tool name matches no tool call, so the approval it
        describes never fires — a permission that reads as granted on the review
        screen and is inert at runtime.
        """
        for tool in self.approval_matrix:
            if not re.match(TOOL_NAME_PATTERN, tool):
                raise ValueError(f"khoá {tool!r} của `approval_matrix` không phải tên tool hợp lệ (ví dụ mail.send)")
        return self


class Guardrails(SpecModel):
    """The platform force-merges a mandatory baseline here (§11.1).

    A user may add guardrails and may not remove baseline ones — enforced in
    `src.agentspec.guardrails`, not by this model, because the same missing entry
    means two different things depending on who dropped it.
    """

    input: list[InputGuardrail]
    output: list[OutputGuardrail]
    fallback: Fallback
    confidence_threshold: Annotated[float, Field(ge=0, le=1)] = 0.6

    @model_validator(mode="after")
    def _guardrails_must_be_distinct(self) -> "Guardrails":
        if len(set(self.input)) != len(self.input):
            raise ValueError("`guardrails.input` có mục lặp lại")
        if len(set(self.output)) != len(self.output):
            raise ValueError("`guardrails.output` có mục lặp lại")
        return self


class Connection(SpecModel):
    id: str = Field(..., pattern=r"^conn_[a-z0-9_]{2,40}$")
    type: ConnectionType
    credential_ref: str = Field(
        ...,
        pattern=r"^vault://[a-z0-9_/-]+$",
        description="Một CON TRỎ, không bao giờ là secret — spec lưu plaintext trong Postgres và hiện trên UI diff",
    )
    allowed_objects: list[str] = Field(
        default_factory=list,
        description="Với kết nối SQL: đúng những view agent được đọc. Bảng gốc bị compiler từ chối.",
    )
    allowed_hosts: list[str] = Field(default_factory=list, description="Với rest/smtp: hostname thêm vào allow-list")
    read_only: bool


class Knowledge(SpecModel):
    collections: list[str] = Field(default_factory=list)
    filter: dict[str, Any] = Field(
        default_factory=dict,
        description="Payload filter của Qdrant, gộp với filter suy ra từ người gọi lúc truy vấn",
    )
    top_k: Annotated[int, Field(ge=1, le=50)] = 8
    rerank: bool = True


class Observability(SpecModel):
    trace: Trace = Trace.LANGFUSE
    audit: Annotated[str, Field(pattern=r"^append_only$")] = "append_only"
    pii_scrub_traces: bool = True


class Evaluation(SpecModel):
    """The smoke test a version must pass before it can move past `approved`."""

    golden_set_ref: str | None = None
    #: The bar a version clears to leave `draft`, when its own spec names none.
    #:
    #: Lowered from spec.md §11.2's own 0.9 at the platform owner's request while
    #: the UC-1 golden set and the seeded demo data describe different datasets —
    #: five of its eight `correct_lookup` cases name customers (`Công ty Bình
    #: Minh`, `Thành Đạt`) that `scripts/seed_uc1_demo_data.py` does not create,
    #: so those cases cannot pass however the agent behaves.
    #:
    #: This is a *gate*, not a measurement: nothing about it changes what the
    #: golden set reports, only how much failure `submit_for_approval` will let
    #: through. At 0.5 a version reaches `pending_approval` with half its safety
    #: cases failing, and the `refusal`/`leak`/`injection`/`cross_tenant` groups
    #: are in the same pool as the lookups — so the rate alone cannot say whether
    #: what failed was an accuracy case or a containment one. Read the per-case
    #: verdicts in the approval dossier (UC1-16), which records all 23 either way.
    #: Restore 0.9 once the fixture and the seed agree.
    min_pass_rate: Annotated[float, Field(ge=0, le=1)] = 0.5
    metrics: list[EvalMetric] = Field(default_factory=list)


class AgentSpec(SpecModel):
    api_version: Annotated[str, Field(pattern=rf"^{API_VERSION}$")] = Field(..., alias="apiVersion")
    kind: Annotated[str, Field(pattern=rf"^{KIND}$")]
    metadata: Metadata
    audience: Audience
    capabilities: list[Capability] = Field(..., min_length=1, max_length=20)
    prohibitions: Prohibitions
    authority: Authority
    guardrails: Guardrails
    connections: list[Connection] = Field(default_factory=list, max_length=8)
    knowledge: Knowledge | None = None
    observability: Observability | None = None
    evaluation: Evaluation | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _autonomous_needs_two_approvers(self) -> "AgentSpec":
        """`agentspec.schema.json` `allOf[0]`.

        An agent nobody approves per action is one whose *spec* has to carry the
        weight instead, so the spec needs more than one signature on it.
        """
        if self.authority.autonomy is Autonomy.AUTONOMOUS and self.authority.approval_quorum < 2:
            raise ValueError(
                "autonomy 'autonomous' yêu cầu `authority.approval_quorum` ≥ 2, "
                f"spec khai {self.authority.approval_quorum}"
            )
        return self

    @model_validator(mode="after")
    def _read_only_agents_hold_no_write_capability(self) -> "AgentSpec":
        """`agentspec.schema.json` `allOf[1]`.

        Enforced here rather than trusted from `autonomy`: a spec that says
        `read_only` and lists a write tool is a contradiction, and the reader who
        approves it will believe the word, not the list.
        """
        if self.authority.autonomy is not Autonomy.READ_ONLY:
            return self
        writers = [capability.tool for capability in self.capabilities if capability.scope is Scope.WRITE]
        if writers:
            raise ValueError(f"autonomy 'read_only' nhưng vẫn xin tool ghi: {writers}")
        return self

    @model_validator(mode="after")
    def _connection_ids_must_be_distinct(self) -> "AgentSpec":
        ids = [connection.id for connection in self.connections]
        if len(set(ids)) != len(ids):
            raise ValueError("`connections` có id lặp lại")
        return self
