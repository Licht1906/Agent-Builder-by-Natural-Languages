"""Request and response bodies for the REST API.

Distinct from `agent_spec.py`: that is the contract between the two halves of
the product, this is the wire format of the HTTP layer.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from src.agents.budget import BudgetLimit
from src.agents.threads import CONVERSATION_ID_PATTERN, ThreadNamespace
from src.agents.tools.registry import ToolRisk
from src.agentspec.spec import ConnectionType
from src.approvals.states import QueueState
from src.datasources.access import AccessRules
from src.datasources.dialects.base import ProbeFailure
from src.db.models import (
    MAX_RATE_LIMIT_PER_USER,
    MIN_RATE_LIMIT_PER_USER,
    ApprovalDecisionKind,
    DatabaseDialect,
    DatabaseSourceStatus,
    DeploymentAccessMode,
    DeploymentStatus,
    DocumentStatus,
    EvalCaseOrigin,
    ExternalConnectionProvider,
    ExternalConnectionStatus,
    KnowledgeBaseMode,
    SandboxRunStatus,
    UC1ConnectionProbeStatus,
    UserRole,
    UserStatus,
    VectorDistanceMetric,
    VersionStatus,
)
from src.interview.slots import Slot
from src.models.agent_spec import AgentSpec
from src.models.eval_check import ExpectedCheck
from src.models.model_catalog import ModelTier
from src.models.node_registry import NodeType
from src.models.solution_spec import (
    AgentSolutionSpecification,
    DataSource,
    DataSourceKind,
    SuccessCriterion,
    SuccessCriterionCategory,
)
from src.models.workflow import Edge, NodeInstance, WorkflowBudget, WorkflowJSON
from src.services.connection_probe import ProbeFailureReason
from src.solution.draft import SpecField
from src.workflow.templates import TemplateStatus
from src.workflow.tools import (
    CredentialKind,
    RiskLevel,
    SandboxBehavior,
    ToolFamily,
    ToolSource,
    ToolStatus,
    UsageScope,
)


def _stamped_utc(value: Any) -> Any:
    """A naive database timestamp, labelled as the UTC it already is.

    Every `created_at` in this platform is a `TIMESTAMP WITHOUT TIME ZONE` filled
    by Postgres `now()`, and the database runs in UTC — so the instant is right
    and only the *label* is missing. Pydantic then serialises it as
    `2026-08-16T01:48:33`, with nothing to say which zone that is.

    That unlabelled string is the bug. `new Date("2026-08-16T01:48:33")` in a
    browser reads it as **local** time, so a reader in UTC+7 sees a conversation
    they opened a moment ago dated seven hours ago. Stamping the zone here is
    what lets the browser convert instead of guess — and it is done at the wire
    boundary rather than in the column, because the stored value is not wrong.
    """
    return value.replace(tzinfo=UTC) if isinstance(value, datetime) and value.tzinfo is None else value


#: A timestamp that leaves this API knowing which zone it is in.
#:
#: Declare response datetimes as this rather than as bare `datetime`. Existing
#: fields still use the bare type and therefore still go out unlabelled; they are
#: the same latent bug and should move over, one screen at a time.
UtcTimestamp = Annotated[datetime, BeforeValidator(_stamped_utc)]


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=320)
    # No constraints at all: every rejected password should look the same to the
    # caller. A 422 on an empty or over-long one would tell an attacker their
    # guess was thrown out before it was ever checked.
    password: str


class RegisterRequest(BaseModel):
    """A new account: who you are, a secret, and which role you are asking for.

    UC1-01 (#117), FR-01: email, password, `role`, `job_title`, `department`
    are all required — the constraints here are shape only (an outer bound on
    every string so a megabyte of text never reaches Argon2id). What actually
    decides whether a signup succeeds lives in `src.auth.registration`: the
    address format, the password floor (FR-02), and whether this is the
    installation's first account (FR-03). Stating any of those twice would be
    two opinions about who starts out trusted, and the one on the screen
    would be whichever ran first.

    `role` has no default — FR-01 lists it as a required field, and an absent
    role silently becoming some default is exactly the ambiguity a role field
    exists to remove. The first account gets `agent_admin` regardless of what
    it asks for (see `src.auth.registration.register_user`), so this field
    only actually decides anything from the second registration onward.
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=1, max_length=320)
    password: str = Field(..., min_length=1, max_length=300)
    role: UserRole = Field(..., description="Vai trò muốn đăng ký")
    job_title: str = Field(..., min_length=1, max_length=200, description="Chức vụ")
    department: str = Field(..., min_length=1, max_length=200, description="Phòng ban")
    #: Optional — auto-generated (`E{id:03d}`) when absent. A demo seed script
    #: pins this explicitly so it lines up with `crm_sales_assignment.sales_rep_id`.
    employee_id: str | None = Field(default=None, max_length=64, description="Mã nhân viên (tự sinh nếu để trống)")


class ActivateUserResponse(BaseModel):
    """What `POST /admin/users/{id}/activate` hands back — the account, now usable."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    status: UserStatus


class AdminUserResponse(BaseModel):
    """One account as `GET /admin/users` shows it — the activation queue's row.

    `UserResponse` already says who somebody is, and this repeats most of it
    rather than reusing it because of `created_at`: an admin deciding what to
    activate next is reading a *queue*, and how long a row has been sitting
    there is the one field that turns a list of names into an order of work.
    `/auth/me` has no use for it, and a field only one caller reads does not
    belong on the response every caller gets.

    No `password_hash` and no token, for the obvious reason — this is the one
    endpoint on the platform that hands one person a list of other people.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: UserRole
    status: UserStatus
    created_at: UtcTimestamp
    employee_id: str | None = None
    job_title: str | None = None
    department: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: UserRole = Field(..., description="Vai trò của tài khoản, để frontend chọn màn hình")


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: UserRole
    status: UserStatus
    employee_id: str | None = None
    job_title: str | None = None
    department: str | None = None


class KnowledgeBaseResponse(BaseModel):
    """One Knowledge Base, as its owner's shelf list shows it — #90 / ADR-0043."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    mode: KnowledgeBaseMode
    document_count: int = Field(..., description="Số Document đang có trên tủ này")
    agent_names: list[str] = Field(
        default_factory=list, description="Tên các agent đang gán tủ này (agent_kb_links) — RAG-13's danh sách"
    )
    external_connection_id: int | None = Field(
        default=None, description="Kết nối vector DB ngoài, nếu mode=byodb — RAG-20 (#106)"
    )
    external_connection_name: str | None = Field(
        default=None, description="Tên người tạo đặt cho kết nối ngoài — nhãn 'Kết nối ngoài' ở danh sách tủ"
    )
    external_connection_status: ExternalConnectionStatus | None = Field(
        default=None, description="Kết quả lần /test gần nhất của kết nối ngoài; null nếu tủ tự quản lý"
    )
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class KnowledgeBaseCreateRequest(BaseModel):
    """A new shelf, in one of the two modes RAG-22's picker offers.

    `mode` defaults to `managed` so every caller written before BYODB existed
    keeps meaning what it meant. The two fields are checked against each
    other rather than accepted independently: a `byodb` shelf with no
    connection has nothing to search, and a `managed` shelf naming one is a
    row whose own two columns disagree about where its Documents live.
    """

    name: str = Field(..., min_length=1, description="Tên tủ tài liệu")
    description: str = Field(default="", description="Tủ này dùng cho việc gì — tuỳ chọn")
    mode: KnowledgeBaseMode = Field(
        default=KnowledgeBaseMode.MANAGED, description="managed = hệ thống tự quản lý; byodb = database riêng của bạn"
    )
    external_connection_id: int | None = Field(
        default=None, description="Bắt buộc khi mode=byodb, phải để trống khi mode=managed"
    )

    @model_validator(mode="after")
    def _mode_and_connection_agree(self) -> "KnowledgeBaseCreateRequest":
        if self.mode is KnowledgeBaseMode.BYODB:
            if self.external_connection_id is None:
                raise ValueError("Chế độ 'database riêng' cần chọn một kết nối ngoài")
        elif self.external_connection_id is not None:
            raise ValueError("Tủ do hệ thống tự quản lý không gắn kết nối ngoài")
        return self


class KnowledgeBaseUpdateRequest(BaseModel):
    """Rename a shelf or change what it is for. Both fields optional — send only what changed."""

    name: str | None = Field(default=None, min_length=1, description="Tên mới, nếu muốn đổi")
    description: str | None = Field(default=None, description="Mô tả mới, nếu muốn đổi")


class AgentKnowledgeBasesRequest(BaseModel):
    """The whole set of shelves an agent may search, replacing whatever it had before.

    A full list rather than one id to add/remove — see
    `src.workflow.knowledge_bases.set_agent_knowledge_bases` for why.
    """

    knowledge_base_ids: list[int] = Field(
        default_factory=list, description="Toàn bộ danh sách Knowledge Base agent này được phép tìm"
    )


class ExternalConnectionCreateRequest(BaseModel):
    """A creator's own vector database — RAG-20 / #106.

    `secret_ref` is a **name**, not a value — the same `credential_secret_ref`
    contract `ToolRegistration` uses (ADR-0045): a creator supplies the key
    their credential will live under, and only an admin's `PUT
    /admin/secrets/{key}` (RAG-19) ever puts a value behind it. Empty means
    the collection needs no credential, a real shape for a self-hosted
    instance with no API key configured.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Tên kết nối, người tạo tự đặt")
    provider: ExternalConnectionProvider
    connection_url: str = Field(..., min_length=1, description="Địa chỉ vector DB ngoài")
    secret_ref: str = Field(
        default="", max_length=200, description="Khoá secret store cho API key, để trống nếu không cần"
    )
    collection_name: str = Field(..., min_length=1, description="Collection đã có sẵn trên vector DB ngoài")
    embedding_dimension: int = Field(..., gt=0, description="Dimension khai báo — đối chiếu ở bước /test")
    distance_metric: VectorDistanceMetric
    field_mapping: dict[str, str] = Field(
        default_factory=dict, description="Tên field thay cho chuẩn mặc định, ví dụ {'text_field': 'content'}"
    )


class ExternalConnectionResponse(BaseModel):
    """One BYODB connection, secrets masked — RAG-20 / #106.

    `has_secret` stands in for `secret_ref`: the ref is only a key name, never
    the value, but it is still internal wiring a caller reading their own
    connection list does not need back verbatim — the acceptance line "GET
    (mask mọi thứ nhạy cảm)" is why this is a boolean rather than the string.
    Assembled field by field (`DeploymentResponse`'s reason), not
    `from_attributes`, because `has_secret` has no column of its own to read.
    """

    id: int
    name: str
    provider: ExternalConnectionProvider
    connection_url: str = Field(..., description="Không mang thông tin đăng nhập — mật khẩu trong URL đã bị che")
    has_secret: bool = Field(..., description="True nếu secret_ref đang trỏ tới một khoá — không lộ chính khoá đó")
    collection_name: str
    embedding_dimension: int
    distance_metric: VectorDistanceMetric
    field_mapping: dict[str, str]
    status: ExternalConnectionStatus
    status_detail: str | None = Field(default=None, description="Vì sao lần /test gần nhất connected/failed")
    status_reason: ProbeFailureReason | None = Field(
        default=None,
        description="Loại lỗi của lần /test gần nhất — màn hình kết nối (RAG-22) dựa vào đây để viết lời khuyên dễ hiểu",
    )
    last_checked_at: UtcTimestamp | None = Field(
        default=None, description="Lúc /test chạy lần gần nhất; null nếu chưa test"
    )
    created_at: UtcTimestamp


class DatabaseSourceCreateRequest(BaseModel):
    """A Hệ CSDL as a creator types it in — DS-02 / ADR-0059 §2.

    `dsn` is the one value this endpoint exists to keep: it goes straight into
    the secret store and is never stored on the row, echoed or logged.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Tên Hệ CSDL, người tạo tự đặt")
    dialect: DatabaseDialect
    dsn: str = Field(..., min_length=1, description="Chuỗi kết nối tới DB mẫu/staging — chỉ đi vào secret store")


class DatabaseSourceUpdateRequest(BaseModel):
    """Any of a name, a new DSN, or the ticked views. `None` leaves that part alone."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    dsn: str | None = Field(default=None, min_length=1, description="DSN mới — thay DSN cũ trong secret store")
    allowed_views: list[str] | None = Field(default=None, description="Các view agent được phép đọc")


class DatabaseViewResponse(BaseModel):
    """One view the last probe saw, and whether the creator ticked it."""

    name: str
    columns: list[str]
    row_count: int | None = Field(..., description="null khi lần kiểm tra không đếm kịp hoặc không có quyền đọc view")
    allowed: bool


class DatabaseSourceResponse(BaseModel):
    """One Hệ CSDL, as a creator reads it back — `has_dsn` in place of the DSN, always.

    Assembled field by field (`ExternalConnectionResponse`'s reason): `has_dsn`
    and `views` have no single column to read from.
    """

    id: int
    name: str
    dialect: DatabaseDialect
    has_dsn: bool = Field(..., description="True nếu đã có DSN trong secret store — không bao giờ là chính DSN")
    status: DatabaseSourceStatus
    status_reason: ProbeFailure | None = Field(
        default=None, description="Loại lỗi của lần kiểm tra gần nhất — để màn hình viết lời khuyên"
    )
    status_detail: str | None = None
    last_probed_at: UtcTimestamp | None = None
    allowed_views: list[str]
    views: list[DatabaseViewResponse] = Field(..., description="Các view lần kiểm tra gần nhất thấy")
    base_tables: list[str] = Field(..., description="Bảng gốc lần kiểm tra gần nhất thấy — không chọn được")
    created_at: UtcTimestamp


class AccessRulesResponse(BaseModel):
    """A Hệ CSDL's quy tắc truy cập (DS-03) and what is wrong with them against the database as last probed.

    `rules` null means none were saved — every asker is denied. `problems`
    is recomputed on every read, so a view unticked after saving shows up
    here rather than only when a query is refused.
    """

    rules: AccessRules | None
    problems: list[str]


class DistinctValuesResponse(BaseModel):
    """The distinct values of one column — the positions the access form offers (DS-03)."""

    values: list[Any]


class UC1ConnectionCreateRequest(BaseModel):
    """UC1-08 (#122), FR-20/FR-21: a credential typed into the connection form, headed straight for Vault.

    `credential` is the one field this whole endpoint exists to keep away
    from the meta-agent — a full connection string for `postgres`
    (`postgresql://user:pass@host:5432/erp`), an API key or password for
    `rest`/`smtp`. It is written to Vault and never stored, echoed, or logged
    by this platform past the one call that writes it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=r"^[a-z0-9_]{2,40}$", description="Đoạn cuối của credential_ref, ví dụ erp_readonly")
    type: ConnectionType
    credential: str = Field(..., min_length=1, description="Đi thẳng vào Vault — không bao giờ được lưu lại ở đây")
    allowed_objects: list[str] = Field(
        default_factory=list, description="Với kết nối SQL: các view agent được đọc — compiler từ chối bảng gốc (FR-24)"
    )
    allowed_hosts: list[str] = Field(default_factory=list, description="Với rest/smtp: hostname thêm vào allow-list")
    read_only: bool = Field(default=True)


class UC1ConnectionResponse(BaseModel):
    """One UC-1 connection — never the credential, only `credential_ref` (FR-22)."""

    id: int
    name: str
    type: ConnectionType
    credential_ref: str = Field(
        ..., description="vault://conn/<name> — giá trị dán vào connections[].credential_ref của spec"
    )
    allowed_objects: list[str]
    allowed_hosts: list[str]
    read_only: bool
    last_probe_status: UC1ConnectionProbeStatus
    last_probe_detail: str | None
    last_probe_objects: list[dict[str, Any]] = Field(
        default_factory=list, description="[{'object': 'v_ar_open', 'row_count': 1284}, ...] của lần probe gần nhất"
    )
    last_probed_at: UtcTimestamp | None
    created_at: UtcTimestamp

    model_config = ConfigDict(from_attributes=True)


class DocumentResponse(BaseModel):
    """One Document on a Knowledge Base — what a `KnowledgeSearch` node will search once RAG-05 (#91) wires it up.

    `id` is the whole address now, not `file_name` — two Documents on the same
    shelf may share a name (`src.db.models.Document`).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    file_name: str
    byte_size: int
    status: DocumentStatus
    status_detail: str | None
    chunk_count: int
    low_confidence: bool = Field(
        ..., description="Trên 30% nội dung trích xuất ở mức 'low' (RAG-06) — màn hình upload nên cảnh báo"
    )
    uploaded_at: UtcTimestamp
    indexed_at: UtcTimestamp | None


class AgentResponse(BaseModel):
    """One agent, as a list or a detail view shows it.

    Still thin — the spec, the version history and the cost figures belong to
    #7's and #15's own endpoints. `is_deployed` is here because a list without
    it cannot answer "which of these can I actually chat with" (#8), and the
    reader would find out by clicking and getting a 409.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    owner_id: int
    is_deployed: bool = Field(
        ..., description="Agent đã được publish thành Deployment chưa — ai chạm được vào là chuyện của access_mode"
    )
    deployed_version_id: int | None = Field(
        default=None,
        description="Version Deployment đang trỏ tới; null nếu chưa publish — con trỏ, không phải trạng thái version",
    )


class AgentBundleRequest(BaseModel):
    """A bundle on its way in — an exported agent, or a file somebody hand-wrote.

    The two documents are typed rather than taken as `dict`, so a malformed
    bundle is a 422 naming the field before anything is written. That is the
    whole reason import needs no validation logic of its own: the schemas that
    guard `PUT .../solution-spec` and `PUT .../workflow` are these schemas.

    `format` is required and checked against `BUNDLE_FORMAT`. A file from a
    later build is refused rather than partially understood — see
    `src.agents.portability`.

    `source_version_id` is provenance the export writes and the import reads
    past: it says which version the file came from, which is worth having in a
    file somebody will still be holding next month, and means nothing here — the
    id belongs to the deployment that wrote it. **Declared rather than allowed by
    `extra="ignore"`**, so that every field of a bundle is named in one place and
    a typo in `workflow` is still a 422 rather than a silently empty agent.
    """

    model_config = ConfigDict(extra="forbid")

    format: str = Field(..., description="Phải khớp BUNDLE_FORMAT, ví dụ 'agentlab.agent/1'")
    name: str = Field(..., min_length=1, max_length=200, description="Tên agent sẽ được tạo")
    solution_spec: AgentSolutionSpecification
    workflow: WorkflowJSON
    source_version_id: int | None = Field(
        default=None, description="Version gốc ở deployment đã xuất file — chỉ để tra ngược, không dùng khi nhập"
    )


class ImportedAgentResponse(BaseModel):
    """What an import created, and what it had to change to create it.

    `remapped_models` maps a node id to `[named_in_file, assigned_here]`. Empty
    on the ordinary import; non-empty when the exporting platform allowed a model
    this one does not, which is a sentence the creator has to read — the agent
    they got is not in every respect the agent in the file.
    """

    agent: AgentResponse
    version_id: int = Field(..., description="Version vừa tạo, đang ở workflow_pending_confirmation")
    remapped_models: dict[str, tuple[str, str]] = Field(
        default_factory=dict, description="node_id -> (model trong file, model được gán ở deployment này)"
    )


class RunningAgentResponse(BaseModel):
    """One agent this caller may talk to — see #15, narrowed by #50.

    A shape of its own rather than `AgentResponse`, because it answers a
    different question. `AgentResponse` is a creator looking at their own
    workspace, where `is_deployed` is news; here being reachable is the entry
    condition, so a field saying so would always be `true`.

    `owner_email` and not `owner_id`: this list is read by people who do not own
    what is on it, and an integer tells them nothing about who to ask when an
    agent misbehaves.
    """

    id: int
    name: str
    #: Beside the email, because the chat screen has one question the email
    #: cannot answer cheaply: "is the person reading this the owner?" — which
    #: decides whether the approval buttons are theirs to press. Matching on
    #: email worked only while the session happened to carry one.
    owner_id: int
    owner_email: str
    deployed_version_id: int
    #: `True` when the published version is a workflow (V2) and the chat goes
    #: through `/deployed-workflow-chat`; `False` for a V1 pipeline, which
    #: answers at `/chat`. The screen used to learn this from a 409.
    runs_workflow: bool = True
    disclaimer: str = Field(
        default="", description="metadata.disclaimer của workflow đang deploy (RAG-14) — rỗng nếu agent không đặt"
    )


class DeployVersionRequest(BaseModel):
    """Which version to point the agent at — the whole of both deploy and rollback.

    There is no `rollback` flag and there is not meant to be one. Rolling back is
    deploying a version that happens to be older (ADR-0005 §3); a flag would be a
    second name for one operation, and the two names would eventually be given
    two sets of rules.
    """

    model_config = ConfigDict(extra="forbid")

    version_id: int = Field(..., description="Version đã duyệt của chính agent này")


class AllowedUserResponse(BaseModel):
    """One person on a Deployment's allowlist — see #50 / ADR-0029 §4.

    Carries the email as well as the id, because the screen that shows this list
    is the owner deciding who to keep on it, and an id is not something anybody
    recognises a colleague by.
    """

    id: int
    email: str


class DeploymentResponse(BaseModel):
    """One agent's publication, as its owner sees it — see #50 / ADR-0029.

    Everything on it is a decision somebody made: which version serves, whether
    it is serving, who may reach it, and the ceiling each caller is held to. The
    API key is deliberately absent — it is a hash in the database and plaintext
    in exactly one response (`DeploymentApiKeyResponse`), so a field for it here
    could only ever be a lie or a leak.

    No `from_attributes`, unlike its neighbours: `allowed_users` is joined to
    `users` rather than read off the row, so this is assembled field by field and
    a config inviting `model_validate(deployment)` would only invite it to be
    assembled a second way — one of which would have ids where the other has
    people.
    """

    id: int
    agent_id: int
    agent_version_id: int = Field(..., description="Version đang được publish — con trỏ, đổi khi deploy bản mới")
    status: DeploymentStatus = Field(..., description="`active` là đang phục vụ, `disabled` là tạm dừng")
    access_mode: DeploymentAccessMode = Field(
        ..., description="`private` là chỉ chủ sở hữu; `selected_users` là danh sách dưới đây"
    )
    allowed_users: list[AllowedUserResponse] = Field(
        default_factory=list,
        description="Chỉ có hiệu lực khi access_mode là selected_users; vẫn giữ khi đổi về private",
    )
    rate_limit_per_user: int = Field(..., description="Số request mỗi người gọi mỗi phút, tính theo cửa sổ trượt")
    created_by: int
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class PublishedDeploymentResponse(DeploymentResponse):
    """A Deployment plus the API key, if this call is what minted one.

    `api_key` is filled on the publish that *created* the Deployment and null on
    every one after it. That asymmetry is the credential's whole shape: the
    plaintext exists in this one response and nowhere else, so a re-publish has
    nothing to put here and must not invent something.
    """

    api_key: str | None = Field(
        default=None, description="Bản rõ của API key, chỉ ở lần publish đầu — không có chỗ nào đọc lại được"
    )


class DeploymentExecutionResponse(BaseModel):
    """One run a Deployment served — see #55 / ADR-0039.

    `error` is the sentence the run died with, or null if it finished. A run
    stopped by its own budget is **not** an error and carries none: it produced
    real output and said why it stopped (ADR-0008 §3).

    `version_id` is on every row rather than on the dashboard as a whole, because
    the pointer moves: a list spanning a release has runs of two versions in it,
    and which release a failure belongs to is the first thing anybody asks.
    """

    id: int
    version_id: int
    conversation_id: str
    tokens: int
    cost_usd: float
    latency_seconds: float
    error: str | None
    at: UtcTimestamp


class DeploymentOperationsResponse(BaseModel):
    """How a Deployment has been serving: four totals and the last few runs — see #55.

    The totals cover every run the Deployment has served; `recent_executions` is
    a capped window onto the same population, not the population the totals are
    computed from. Both come from `run_costs` (ADR-0009 §4) through one SQL
    condition, so the number at the top and the list underneath can never be
    describing different runs.

    `agent_version_id` is what is serving **now**, which is deliberately not what
    the numbers are scoped to: a dashboard that reset on every publish would hide
    the release an owner opened it to look at.
    """

    deployment_id: int
    agent_id: int
    agent_version_id: int = Field(..., description="Version đang phục vụ lúc này — không phải phạm vi của các số dưới")
    runs: int = Field(..., description="Số lượt chạy thật, không tính lượt tập dượt của người tạo")
    errors: int = Field(..., description="Số lượt chết giữa chừng; dừng vì hết ngân sách không tính là lỗi")
    tokens: int
    cost_usd: float
    recent_executions: list[DeploymentExecutionResponse]


class DeploymentApiKeyResponse(BaseModel):
    """A freshly minted API key. The only other moment the plaintext exists."""

    api_key: str = Field(..., description="Key mới; key cũ ngừng hiệu lực ngay khi cái này tồn tại")


class DeploymentStatusRequest(BaseModel):
    """Turn serving on or off, without touching which version is published (ADR-0029 §2)."""

    model_config = ConfigDict(extra="forbid")

    status: DeploymentStatus = Field(..., description="`disabled` để tạm dừng, `active` để phục vụ lại")


class DeploymentAccessRequest(BaseModel):
    """Who may reach this Deployment — the mode, and for `selected_users`, exactly who.

    Emails rather than user ids, because this is the one request a person composes
    by hand: an owner knows their colleagues by address, and the platform has no
    directory endpoint for turning a name into an id. Unknown addresses are
    refused by name rather than silently dropped — an allowlist that quietly
    ignored a typo would be an allowlist its owner believed was longer than it is.

    Always the **whole** list, never an addition: removing somebody has to be the
    same request as adding somebody, or one of the two eventually goes missing.
    """

    model_config = ConfigDict(extra="forbid")

    access_mode: DeploymentAccessMode
    allowed_user_emails: list[str] = Field(
        default_factory=list, max_length=100, description="Toàn bộ danh sách, không phải phần thêm vào"
    )


class DeploymentRateLimitRequest(BaseModel):
    """How many requests one caller may make per minute — see #51 / ADR-0037 §6.

    Its own request rather than a field on the access or status body, for the
    reason ADR-0036 §3 keeps those two apart: an owner adjusting a ceiling must
    not be able to also widen their allowlist by sending a field they did not
    think about.

    Bounded rather than open. Zero would be "a Deployment that refuses everybody",
    which `status: disabled` already says and says more honestly, and an unbounded
    number is a way to switch cost control off that nobody would notice in a
    settings payload.
    """

    model_config = ConfigDict(extra="forbid")

    rate_limit_per_user: int = Field(
        ...,
        ge=MIN_RATE_LIMIT_PER_USER,
        le=MAX_RATE_LIMIT_PER_USER,
        description="Số request mỗi người gọi mỗi phút, áp riêng cho từng người gọi",
    )


class AgentRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class ChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1)
    # Omitted means "start a new chat window", so the server mints one and
    # announces it on the stream — the client never has to invent an id to get
    # memory, and can't accidentally resume someone else's conversation by
    # guessing one. The pattern is `ThreadRef`'s own rule, enforced here too so
    # a malformed id is a 422 instead of a 500 from inside the runtime.
    conversation_id: str | None = Field(
        default=None,
        pattern=CONVERSATION_ID_PATTERN.pattern,
        description="Thread hội thoại cần tiếp tục; bỏ trống để mở phiên mới",
    )
    namespace: ThreadNamespace = Field(
        default=ThreadNamespace.PRODUCTION,
        description="`trial` cho lần chạy thử của người tạo — thread nằm riêng, không lẫn vào lịch sử production",
    )


class ThreadStartedEvent(BaseModel):
    """The stream's first event: which thread this turn ran on.

    Sent on every turn, not just the first, so a client that lost its state
    (a reload, a reconnect) can always tell which conversation it is in.
    """

    conversation_id: str
    namespace: ThreadNamespace
    #: The run's trace id (ADR-0053): what a thumbs from the chat window names,
    #: since the ledger row it lands on is written after the stream ends.
    trace_id: str | None = None


class TranscriptTurnResponse(BaseModel):
    """One line of a thread, as the tracing client and the chat panel see it.

    `node_type` is the field the chat panel reads: `agent_llm` is the agent
    speaking, `mcp`/`human_approval` is the run acting, and the panel draws the
    two differently. Null on V1 lines and on checkpoints written before the
    runtime stamped it — both read as speech, as they always did.
    """

    role: Literal["user", "agent"]
    content: str
    step_id: str | None = Field(default=None, description="Bước nào của spec sinh ra dòng này; null nếu là người dùng")
    node_id: str | None = Field(
        default=None, description="Node nào của workflow ghi dòng này; null với V1 hoặc người dùng"
    )
    node_type: str | None = Field(
        default=None, description="Loại node ghi dòng này — `agent_llm` là agent nói, `mcp`/`human_approval` là run làm"
    )
    tool: str | None = Field(default=None, description="Với dòng `mcp`: tool.action đã gọi")
    ok: bool | None = Field(default=None, description="Với dòng `mcp`: lời gọi có thành công không")


class PendingApprovalResponse(BaseModel):
    """The pause a conversation is currently stopped at, if any.

    Carried with the history so a reopened conversation shows the same card
    the live stream did — "đang chờ duyệt", and the buttons for whoever may
    decide — instead of ending on the draft with nothing to say it is waiting.
    """

    approval_id: int
    node_id: str
    approver_role: str
    message: str
    actions: list["ApprovalActionResponse"] = Field(
        default_factory=list, description="Các lời gọi lần duyệt này đứng trước, nguyên văn (DS-08)"
    )


class ApprovalActionResponse(BaseModel):
    """One call a pause stands in front of, exactly as it will be made — DS-08.

    `args` verbatim: the card shows this, and the action node refuses anything
    whose `hash` differs from it.
    """

    node_id: str
    tool: str = Field(..., description="tool.action sẽ gọi, ví dụ gmail.send_email")
    action: str
    args: dict[str, Any] = Field(..., description="Tham số nguyên văn sẽ gửi đi")
    hash: str = Field(..., description="Hash chuẩn hoá của payload đã hiển thị")


class ThreadHistoryResponse(BaseModel):
    """A thread read back for tracing — empty `turns` for a thread never used."""

    conversation_id: str
    namespace: ThreadNamespace
    turns: list[TranscriptTurnResponse]
    pending_approval: PendingApprovalResponse | None = Field(
        default=None, description="Lần chạy cuối trên thread này đang dừng chờ duyệt — null nếu không"
    )


class ConversationResponse(BaseModel):
    """One row of the chat sidebar — a way back into a conversation.

    No message text. The sidebar draws a list of labels and the transcript is
    fetched only for the one that gets clicked; sending every conversation's
    turns to render a list would make opening the screen cost the whole history.

    `last_message_at` rather than a relative "2 hours ago": the server knows the
    instant, only the browser knows the reader's timezone, and a string formatted
    here would be wrong for everybody who is not in the server's.
    """

    model_config = ConfigDict(from_attributes=True)

    conversation_id: str
    title: str
    namespace: ThreadNamespace
    created_at: UtcTimestamp
    last_message_at: UtcTimestamp


class ConversationRenameRequest(BaseModel):
    """A new label for one conversation.

    Bounded because this is a list item: a title long enough to fill the sidebar
    would push every other conversation off the screen it exists to make
    navigable. `min_length=1` so clearing the box is a 422 rather than a
    conversation with no name — the way to lose a title is to delete the
    conversation, not to blank it.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=120, description="Nhãn hiển thị trên sidebar")


class AgentVersionResponse(BaseModel):
    """One row of an agent's version history — see #7 / ADR-0005.

    `is_deployed` is computed by the route (`version.id == agent.deployed_version_id`),
    not read off the ORM row, since a version does not know it is deployed —
    only the agent's pointer does.
    """

    id: int
    number: int = Field(
        ...,
        description=(
            "Version thứ mấy CỦA AGENT NÀY, đếm từ 1 — thứ để hiển thị. "
            "`id` là khoá chính toàn bảng nên version đầu tiên của một agent mới vẫn có id lớn"
        ),
    )
    status: VersionStatus
    rejection_reason: str | None
    is_deployed: bool
    created_at: UtcTimestamp


class AgentVersionSpecResponse(AgentVersionResponse):
    """One version with the spec it holds — what the wizard's spec screen reads (#19).

    Not folded into `AgentVersionResponse`: the history list carries a row per
    version, and putting every spec on it would send a whole agent's worth of
    prompts down to draw a list of statuses. A screen showing *one* spec asks for
    one.

    `agent_id` is carried even though the caller put it in the path, because the
    refine turn (#18) hands back a version of the same agent and the screen must
    be able to hold both shapes without reading the URL back.
    """

    agent_id: int
    spec: AgentSpec = Field(..., description="Spec đã lưu, đã qua validate lại lúc đọc")


class SandboxRunRequest(BaseModel):
    """What a creator sends to trial a version — see #9 / ADR-0007.

    There is no `namespace` field, unlike `ChatMessageRequest`: a trial run is
    always on a `trial` thread, and offering the choice would let a caller write
    test chatter into an agent's real conversation history.
    """

    message: str = Field(..., min_length=1, description="Dữ liệu đầu vào người tạo tự chọn")
    conversation_id: str | None = Field(
        default=None,
        pattern=CONVERSATION_ID_PATTERN.pattern,
        description="Thread thử cần tiếp tục; bỏ trống để mỗi lần chạy thử là một hội thoại mới",
    )
    run_as: str | None = Field(
        default=None,
        max_length=320,
        description=(
            "Chạy với tư cách ai (DS-05) — giá trị khớp identity.match_by của Hệ CSDL, ví dụ email; "
            "đổi được giữa các lượt trong cùng thread; bỏ trống = không ai hỏi, mọi lần đọc dữ liệu bị cấm"
        ),
    )
    send_for_real: bool = Field(
        default=False,
        description=(
            "Gửi thật (DS-09): hành động đứng sau một bước duyệt đã duyệt đúng payload chạy thật bằng kết nối "
            "của chính người tạo thay vì mô phỏng. Chỉ cho lượt này — lượt sau mặc định tắt lại"
        ),
    )


class AskerResponse(BaseModel):
    """One person a creator can run a trial as — "Chạy với tư cách…"'s suggestion (DS-05)."""

    value: str = Field(..., description="Giá trị khớp identity.match_by — thứ gửi lại trong run_as")
    position: str | None = Field(default=None, description="Chức vụ, đọc từ view nhân viên")
    source: str = Field(..., description="Hệ CSDL người này được tìm thấy")


class SandboxTranscriptEntry(BaseModel):
    """One line of a sandbox transcript — both what is stored and what is served.

    The two are the same shape on purpose: the transcript is evidence, and an
    admin reading it should be reading the run's own record rather than a
    rendering of it (#9, ADR-0007 §5).

    One flat shape rather than one per kind, because the consumer that matters
    is a person reading down the run looking for where it went wrong, and they
    read it as one ordered list.

    One shape for **both runtimes**, too (#46), for the same reason: the approval
    dossier renders one transcript, and a V1 and a V2 trial are the same kind of
    evidence about the same kind of question. What differs is which document a
    line points into — a `step_id` indexes `AgentSpec.steps`, a `node_id` indexes
    `WorkflowJSON.nodes` — and that difference is kept in two fields rather than
    collapsed into one, exactly as ADR-0031 §6 kept the two event vocabularies
    apart. A reader handed one under the other's name would draw a run overlay on
    a graph that never executed. `_one_document` is what makes "exactly one of
    them" a rule rather than a convention.
    """

    kind: Literal["step", "node", "tool", "tool_result", "budget_exceeded"]
    step_id: str | None = Field(default=None, description="Bước nào của spec V1 sinh ra dòng này")
    node_id: str | None = Field(default=None, description="Node nào của workflow V2 sinh ra dòng này")
    node_type: str | None = Field(default=None, description="Loại node đã chạy, chỉ có ở dòng V2")
    title: str | None = Field(default=None, description="Tiêu đề bước, chỉ có ở dòng `step`")
    tool: str | None = Field(default=None, description="Tên tool, chỉ có ở dòng `tool` / `tool_result`")
    args: dict[str, Any] | None = Field(default=None, description="Tham số agent đã chọn cho tool")
    simulated: bool | None = Field(
        default=None,
        description="True = tool có tác dụng phụ, đã chạy bản mô phỏng — dòng này nói nó *sẽ* làm gì",
    )
    live_in_sandbox: bool | None = Field(
        default=None,
        description="True = lượt chạy thử bật 'Gửi thật' và lời gọi đã duyệt này chạy thật (DS-09)",
    )
    ok: bool | None = Field(default=None, description="False = tool gọi hỏng; lần chạy vẫn tiếp tục")
    content: str | None = Field(default=None, description="Câu trả lời của bước, hoặc kết quả tool trả về")
    # ADR-0061. Only ever on a failed `tool_result`, and only when the runtime
    # *knew* — the exception declared itself, or the node could not resolve at
    # all. `None` covers both "this failure is the agent's business" and "this
    # line was written before the field existed"; the two are told apart by
    # `src.workflow.faults.fault_of_reason`, which reads only sentences the
    # platform composes itself.
    fault: str | None = Field(
        default=None,
        description="Lỗi thuộc về nền tảng chứ không phải agent (credential, action, kết nối, model) — null nếu không",
    )
    # Only on a `budget_exceeded` line (#10). Numbers as well as the message in
    # `content`, so a reader can tell a runaway agent from a ceiling that was
    # always too tight for the work — those need different fixes.
    limit: BudgetLimit | None = Field(default=None, description="Trần nào bị vượt: token hay thời gian")
    limit_value: float | None = Field(default=None, description="Trần đó là bao nhiêu")
    used: float | None = Field(default=None, description="Lần chạy đã dùng bao nhiêu khi bị dừng")
    # The three ADR-0026 §3–4 fields, on `node` lines of a V2 run only. Optional
    # and additive rather than a second entry shape: the transcript is a flat
    # list read one line at a time, and a reader who had to know which of two
    # shapes a line was would be doing the cross-referencing the flat shape
    # exists to avoid.
    port_taken: str | None = Field(
        default=None,
        description="Cổng mà node quyết định đã đi ra — ghi ngay tại chỗ quyết định, không suy lại từ cạnh của graph",
    )
    iteration: int | None = Field(
        default=None,
        description="Vòng lặp thứ mấy của While bao quanh; null với node không nằm trong thân vòng lặp nào",
    )
    forced_exit: bool | None = Field(
        default=None,
        description="Chỉ có ở lối ra của While: true = thoát vì chạm trần vòng lặp, không phải vì điều kiện sai",
    )

    @model_validator(mode="after")
    def _one_document(self) -> "SandboxTranscriptEntry":
        """Every line names exactly one of the two documents a run can be written in.

        Neither is the case that matters. A line with no id at all is a line an
        admin cannot place — "something went wrong somewhere in this run" is not
        evidence — and it is reachable by a caller building an entry from an
        event whose payload key they misread, which is precisely the mistake two
        similarly-shaped vocabularies invite. Both at once is the same mistake
        wearing a belt: the two ids index different documents, so a line
        answering to both is a line that is lying about one of them.
        """
        if (self.step_id is None) == (self.node_id is None):
            raise ValueError("Mỗi dòng transcript phải mang đúng một trong hai: `step_id` (V1) hoặc `node_id` (V2)")
        return self


class SandboxRunResponse(BaseModel):
    """One recorded trial run, read back in full — the evidence an approval request carries."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    version_id: int
    status: SandboxRunStatus
    conversation_id: str
    input_message: str
    error: str | None = Field(default=None, description="Vì sao lần chạy không hoàn tất; null nếu thành công")
    created_at: UtcTimestamp
    transcript: list[SandboxTranscriptEntry]


class EvalEvidence(BaseModel):
    """One tool call the case's run made, as the judge saw it and the creator can now see it.

    Stored on the result (ADR-0052) because a failed case used to be the one
    line of an eval nobody could check: the judge had read the tool result,
    said *"không có bằng chứng"* or *"đã bịa"*, and the transcript it judged
    against was gone by the time anyone asked. `result` is cut to what the
    judge itself was shown (`src.evals.judge._RESULT_CHARS`), so the record
    says exactly what the verdict was based on — no more, no less.
    """

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool | None = Field(default=None, description="None = kết quả không được ghi lại")
    result: str
    simulated: bool = Field(
        default=False, description="True = kết quả mô phỏng của sandbox, không phải của hệ thống thật"
    )


class CaseApplicability(StrEnum):
    """Whether a run-eligible suite case's criterion still lives in *this* version's spec — ADR-0054 §1.

    Computed at run time, never stored on the case: applicable-or-not is a fact
    about a (case, version) pair, and the same case is `live` against the
    version it was written for and `orphan` against one whose creator has since
    reworded or dropped that criterion. A case whose rule-based check cannot be
    grounded against the version's graph at all does not get either value — it
    is `ungrounded` and never reaches this type; see `eval_runs.skipped`.
    """

    LIVE = "live"
    ORPHAN = "orphan"


class SkippedCase(BaseModel):
    """One suite case a run left out — rule-based, `ungrounded` against this version's graph (ADR-0054 §1).

    Never sent to the sandbox and never judged: an `expected_check` naming an
    action or node this version's graph does not have would only ever grade a
    string nothing can produce. Kept here rather than silently dropped, so a
    creator whose graph changed sees exactly which of their own cases stopped
    applying and why — the case itself is untouched and reappears the moment
    the graph does again.
    """

    suite_case_id: int = Field(..., description="Case bị bỏ qua lần chạy này")
    reason: str = Field(..., description="Vì sao không chạy được — hành động/node nào không có trong graph")


class EvalCaseResult(BaseModel):
    """One case's outcome — both what is stored in `eval_runs.results` and what is served.

    The same shape for both, for the reason `SandboxTranscriptEntry` is: the
    results are evidence, and an admin reading them should be reading the run's
    own record rather than a rendering of it.

    Every field of the case is copied in rather than referenced. Cases are
    editable and deletable by design (#13), so a result that pointed at a row
    would, the moment that row changed, describe a test that never ran.
    """

    case_id: int = Field(..., description="Case đã sinh ra kết quả này; hàng đó có thể đã bị sửa hoặc xoá từ lúc đó")
    suite_case_id: int = Field(
        default=0,
        description=(
            "Cùng giá trị với case_id — id ổn định của case trong suite, dùng để ghép hai lần chạy "
            "(ADR-0054 §1). Mặc định 0 chỉ để đọc lại run cũ ghi trước khi trường này tồn tại"
        ),
    )
    applicability: CaseApplicability = Field(
        default=CaseApplicability.LIVE,
        description="`live` nếu tiêu chí còn trong spec version này, `orphan` nếu không còn — case vẫn chạy cả hai",
    )
    criterion: str = Field(..., description="Tiêu chí thành công case này kiểm, đúng lời người tạo")
    input_message: str
    expected: str
    answer: str = Field(..., description="Câu trả lời agent đưa ra trong lần chạy sandbox")
    passed: bool
    reason: str = Field(..., description="Vì sao đạt hoặc vì sao không đạt")
    # The three #48 / ADR-0028 fields. All three default, because a run stored
    # before this ticket has none of them and `results_of` parses those rows
    # back with the same model — a required field here would make every V1 run
    # in the database unreadable.
    category: SuccessCriterionCategory | None = Field(
        default=None, description="Check tự động nào đã chấm case này; null với case V1"
    )
    expected_check: ExpectedCheck | None = Field(
        default=None, description="Luật đã đọc thẳng transcript để chấm; null = case do LLM judge chấm"
    )
    safety_violation: bool = Field(
        default=False,
        description=(
            "Case trượt vì vi phạm luật cứng — gọi tool bị cấm, hoặc hành động sau khi bị từ chối. "
            "Không vượt rào được bằng eval_override (ADR-0028 §6)"
        ),
    )
    evidence: list[EvalEvidence] = Field(
        default_factory=list,
        description="Các tool call của lần chạy và kết quả — thứ máy chấm đã đối chiếu; rỗng với run cũ hay graph không gọi tool",
    )
    metrics: list["MetricScoreOut"] = Field(
        default_factory=list,
        description=(
            "Điểm thô 0..1 của các metric catalog (ADR-0054 §4) đã chấm case này — hiện chỉ "
            "`context_precision`/`task_completion` ghi vào đây; rỗng với mọi case khác"
        ),
    )


class MetricScoreOut(BaseModel):
    """One `METRIC_CATALOG` metric's raw score on one case — ADR-0054 §4.

    Kept beside `EvalCaseResult.passed` rather than instead of it: `passed` is
    the binary verdict that gates anything (the lecture's own rule, s52), this
    is the number underneath it a team calibrates a threshold against later.
    """

    metric: str = Field(..., description="Tên metric trong METRIC_CATALOG, vd `context_precision`")
    score: float = Field(..., ge=0.0, le=1.0, description="Điểm thô máy chấm cho, trước khi cắt nhị phân")
    threshold: float = Field(..., description="Ngưỡng cắt nhị phân đã dùng cho lần chấm này")
    passed: bool
    reason: str


class EvalCaseResponse(BaseModel):
    """One test case as the creator's screen shows it.

    `expected_check` is served beside `expected` rather than instead of it,
    because they answer different readers: `expected` is the sentence a person
    reads to know what "đạt" means, and the check is the rule that actually
    decides it. On a rule-graded case the first is derived from the second
    (`src.models.eval_check.describe_check`), so a screen showing only `expected`
    is not showing anything untrue — it is showing less.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    criterion: str
    input_message: str
    expected: str
    origin: EvalCaseOrigin = Field(..., description="`generated` = máy đề xuất, `manual` = người tạo tự viết")
    category: SuccessCriterionCategory | None = Field(
        default=None, description="Check tự động nào chấm case này; null với case V1 và case sinh từ prohibitions"
    )
    expected_check: ExpectedCheck | None = Field(
        default=None, description="Luật đọc thẳng transcript; null = case do LLM judge chấm"
    )
    tags: list[str] = Field(default_factory=list, description="Nhãn của người tạo, để lọc — ADR-0054 §1")
    run_as: str | None = Field(default=None, description="Case chạy với tư cách ai (DS-05); null = không ai hỏi")
    # Two columns a retired feature left on the row (ADR-0065). They are still
    # reported because rows carrying them still exist and a creator looking at
    # one should see everything it holds; nothing writes them any more.
    cell_key: str | None = Field(default=None, description="Di sản lưới tình huống (ADR-0065) — chỉ hàng cũ mới có")
    zone: str | None = Field(default=None, description="Di sản lưới tình huống (ADR-0065) — chỉ hàng cũ mới có")
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class EvalCaseCreateRequest(BaseModel):
    """A case the creator writes by hand — the escape hatch from a generator that misread them."""

    criterion: str = Field(..., min_length=1, description="Tiêu chí thành công case này kiểm")
    input_message: str = Field(..., min_length=1, description="Đầu vào đưa cho agent")
    expected: str = Field(..., min_length=1, description="Thế nào là đạt")
    run_as: str | None = Field(default=None, max_length=320, description="Chạy với tư cách ai (DS-05)")


class EvalCaseUpdateRequest(BaseModel):
    """An edit to a case. Omit a field to leave it as it is.

    `extra="forbid"` for the same reason `PlatformConfigUpdateRequest` has it: a
    silently ignored field looks exactly like one that was applied, and here that
    would mean a creator believing they fixed a case that still says what it said.
    """

    model_config = ConfigDict(extra="forbid")

    criterion: str | None = Field(default=None, min_length=1)
    input_message: str | None = Field(default=None, min_length=1)
    expected: str | None = Field(default=None, min_length=1)
    run_as: str | None = Field(
        default=None, max_length=320, description="Chạy với tư cách ai (DS-05); chuỗi rỗng = bỏ, không ai hỏi"
    )


class EvalGenerationResponse(BaseModel):
    """What generating from the spec's criteria produced, and what it did not.

    `skipped_criteria` is part of the answer rather than a log line: a criterion
    with no case behind it is a hole in the test set that the score cannot show,
    and the only person who can fill it is the creator reading this.
    """

    cases: list[EvalCaseResponse]
    skipped_criteria: list[str] = Field(
        ..., description="Tiêu chí máy không viết nổi test case — hãy tự thêm case cho những tiêu chí này"
    )
    duplicate_count: int = Field(
        default=0,
        description=(
            "Số case đề xuất bị bỏ qua vì tiêu chí đó đã có case generated trong suite rồi (ADR-0054 §1) — "
            "không phải lỗi, chỉ là không có gì mới để thêm"
        ),
    )


class SuiteImportRowResponse(BaseModel):
    """One row of an uploaded test-set file, and what happened to it — ADR-0054 §2.

    Row-level rather than a single file-level verdict, on purpose: a creator
    reading this after uploading 20 rows needs to know which 2 failed and why,
    not just that the file "partially imported".
    """

    row: int = Field(..., description="Số dòng trong file (1-based, không tính header)")
    status: Literal["added", "skipped", "error"]
    reason: str | None = Field(default=None, description="Vì sao — bỏ trống khi added và không có gì đáng nói")


class SuiteImportResponse(BaseModel):
    """The whole report of one import — a row-by-row account plus the totals a screen shows first."""

    rows: list[SuiteImportRowResponse]
    added_count: int
    skipped_count: int
    error_count: int
    dry_run: bool = Field(..., description="`true` = không có gì được ghi xuống DB, đây chỉ là bản xem trước")


class EvalRunRequest(BaseModel):
    """Which cases one run covers — absent body means the whole set.

    ADR-0054 §5's "Chạy ▾ tất cả / chỉ fail / đã chọn": the screen decides the
    id list ("chỉ fail" is the ids the latest run failed), the backend only
    filters — so the two doors stay one mechanism and an id that is not in the
    suite is simply not run rather than being an error a batch would die on.
    """

    model_config = ConfigDict(extra="forbid")

    case_ids: list[int] | None = Field(default=None, description="null = mọi case của suite")


class EvalRunResponse(BaseModel):
    """One eval run: every case's verdict, the score, and how far it is from the bar.

    `threshold` is the bar **in force now** — the one the approval gate actually
    applies — while `threshold_at_run` is what it was when the run happened. They
    differ only when an admin has changed the setting since, and when they do,
    the number that decides anything is the first one (ADR-0011 §5).
    """

    id: int
    version_id: int
    passed_count: int
    total_count: int
    pass_rate: float
    threshold: float = Field(..., description="Ngưỡng đang có hiệu lực — đây là con số quyết định")
    threshold_at_run: float = Field(..., description="Ngưỡng lúc lần chạy này diễn ra, giữ lại cho hồ sơ duyệt")
    meets_threshold: bool
    gap: float = Field(..., description="Còn thiếu bao nhiêu tỷ lệ nữa mới tới ngưỡng; 0 nếu đã đạt")
    cases_short: int = Field(..., description="Cần thêm bao nhiêu case đạt nữa mới tới ngưỡng; 0 nếu đã đạt")
    blocked_reason: str | None = Field(
        default=None,
        description=(
            "Vì sao version này chưa xin duyệt được và việc tiếp theo phải làm; null nếu xin duyệt được. "
            "Nói về *version*, không phải về lần chạy đang xem: cổng duyệt chỉ đọc lần chạy gần nhất"
        ),
    )
    judge_model: str = Field(
        default="",
        description=(
            "Model đã chấm lần chạy này (ADR-0054 §4) — rỗng cho run ghi trước khi trường này tồn tại. "
            "Đổi judge model là đổi baseline: `src.pm.reports.version_report` không so delta giữa hai lần "
            "chạy có judge_model khác nhau"
        ),
    )
    created_at: UtcTimestamp
    results: list[EvalCaseResult]
    skipped: list[SkippedCase] = Field(
        default_factory=list, description="Case rule-based ungrounded với graph của version này — không chạy"
    )


class ModelCostResponse(BaseModel):
    """What one model spent inside a run — where "the cheap step was cheap" is visible."""

    input_tokens: int
    output_tokens: int
    usd: float


class CostFigureResponse(BaseModel):
    """One cost figure, and — inseparably — what kind of claim it is making.

    `label` and `nature` are part of the payload rather than left to the client
    (#11, ADR-0009 §2). A frontend that had to supply its own wording would
    eventually supply the wrong one, and the failure mode is the exact thing this
    ticket exists to prevent: a reader taking a guess for a guarantee.
    """

    basis: Literal["estimate", "worst_case", "measured"]
    label: str = Field(..., description="Tên con số, ví dụ 'Trần xấu nhất'")
    nature: str = Field(..., description="Bản chất con số: phỏng đoán, đảm bảo, hay số liệu")
    input_tokens: int
    output_tokens: int
    usd: float
    seconds: float | None = Field(default=None, description="Lần chạy kéo dài bao lâu; chỉ có ở con số đo thật")
    bound: Literal["budget", "structure"] | None = Field(
        default=None,
        description="Trần nào đang quyết định con số xấu nhất: ngân sách token hay hình dạng của spec",
    )
    by_model: dict[str, ModelCostResponse] = Field(
        default_factory=dict, description="Tách theo model; chỉ có ở con số đo thật"
    )


class VersionCostsResponse(BaseModel):
    """The three figures for one version, side by side — never merged.

    Every one of them may be absent, and each absence means its own thing.
    `measured` is `None` for a version nobody has trialled yet, and deliberately
    not zero: "we ran it and it was free" is the one thing a measurement must
    never say when nothing was measured.

    `estimate` and `worst_case` are `None` for a V2 version (#49): both are
    computed from an `AgentSpec`, which a workflow-based version does not have,
    and nothing in this platform prices a graph. Its ceiling is
    `ApprovalDossierResponse.budget` instead. What stays filled is `measured` —
    a V2 trial spends real money and writes a real `run_costs` row, and dropping
    the one figure that is a *fact* because the two projections are missing would
    hide the only cost anybody actually paid.
    """

    version_id: int
    estimate: CostFigureResponse | None = Field(
        default=None, description="Phỏng đoán từ AgentSpec; null với version V2 — chưa có mô hình giá cho graph"
    )
    worst_case: CostFigureResponse | None = Field(
        default=None, description="Trần từ AgentSpec; null với version V2 — trần của nó là `budget` của workflow"
    )
    measured: CostFigureResponse | None = Field(
        default=None, description="Đọc từ lần chạy thử gần nhất; null nếu version chưa từng chạy thử"
    )


class InvokedStepResponse(BaseModel):
    """What one step of the spec produced, for a caller with no stream to watch."""

    step_id: str
    title: str
    content: str


class InvokedToolResponse(BaseModel):
    """One tool the run reached for, and how it went.

    The same facts the chat window renders live from `tool` / `tool_result`
    (#8) — an API caller integrating this agent into their own product needs to
    be able to answer "what did it actually do", and a bare answer string cannot.
    """

    step_id: str
    tool: str
    ok: bool
    result: str


class AgentInvocationRequest(BaseModel):
    """One call to a running agent from outside the platform's own UI — see #15.

    No `namespace`, unlike `ChatMessageRequest`: an API call is a real
    conversation with the deployed version, and there is no such thing as an
    integration wanting to rehearse on the creator's trial threads.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1)
    conversation_id: str | None = Field(
        default=None,
        pattern=CONVERSATION_ID_PATTERN.pattern,
        description="Thread cần tiếp tục; bỏ trống để mở phiên mới — id được trả về để gọi tiếp",
    )


class AgentInvocationResponse(BaseModel):
    """Everything one API call produced, once the run is over.

    The same run the chat window streams, delivered whole. `version_id` is on it
    deliberately: an integration that starts getting different answers should be
    able to see *which* version began giving them, without asking a second
    endpoint.

    Declared here rather than beside `AgentResponse` because it carries a
    `CostFigureResponse`, and a response cannot name a model defined below it.
    """

    agent_id: int
    version_id: int
    conversation_id: str = Field(..., description="Gửi lại id này ở lượt sau để agent nhớ cuộc trò chuyện")
    answer: str = Field(..., description="Nội dung các bước nối lại — thứ để hiển thị cho người dùng cuối")
    steps: list[InvokedStepResponse]
    tools: list[InvokedToolResponse]
    stopped_reason: str | None = Field(
        default=None,
        description="Vì sao lần chạy dừng sớm — ví dụ chạm trần ngân sách (#10); null nếu chạy hết các bước",
    )
    cost: CostFigureResponse | None = Field(
        default=None, description="Lần chạy này tốn bao nhiêu; null nếu runtime không báo được"
    )


class AgentCostsResponse(BaseModel):
    """Everything one agent has actually cost so far — see #11.

    Rehearsal and production are broken out because they mean different things:
    an agent whose bill is all trial runs is being built, one whose bill is all
    production is being used.
    """

    agent_id: int
    runs: int
    tokens: int
    usd: float
    trial_usd: float
    production_usd: float


class IngestionSpendEntryResponse(BaseModel):
    """One bucket of the admin's ingest cost board — see #101.

    `key` reads differently depending on which axis the request grouped by:
    an ISO date, a `YYYY-MM` month, or an owner/Knowledge Base id as a string —
    the same "one flat shape, the axis is in the request" call
    `src.agents.spend.agent_spend` makes for `group_by` on the run-cost side.
    """

    key: str
    document_count: int
    page_count: int
    input_tokens: int
    output_tokens: int
    usd: float


class AgentCostBoardEntry(AgentCostsResponse):
    """One row of the admin's cost board — the same totals, plus who they belong to.

    Extends rather than repeats `AgentCostsResponse`: a creator looking at their
    own agent and an admin looking down the board must be reading the same
    numbers, and two independent shapes is how they stop being.
    """

    name: str


class SpecComparisonResponse(BaseModel):
    """The wire shape of `src.agents.versions.SpecComparison` — V1's pipeline.

    Each entry in `changed_fields` / `changed_steps` is a `[before, after]`
    pair, keyed by field name — only fields that actually differ are present.
    """

    changed_fields: dict[str, tuple[Any, Any]]
    added_step_ids: list[str]
    removed_step_ids: list[str]
    changed_steps: dict[str, dict[str, tuple[Any, Any]]]


class WorkflowEdgeRefResponse(BaseModel):
    """One connection of a graph, by where it runs from and to.

    No `id`: an edge's id is minted by the canvas and means nothing across two
    documents, which is exactly why the diff does not key on it
    (`src.agents.versions.EdgeRef`).
    """

    source_node_id: str
    source_port: str
    target_node_id: str


class WorkflowNodeDiffResponse(BaseModel):
    """How one node of a graph changed — `src.agents.versions.NodeDiff`.

    `changed_config_keys` names what moved inside `config`; the `config` entry in
    `changed_fields` carries both whole mappings for a screen that wants to show
    them side by side.
    """

    changed_fields: dict[str, tuple[Any, Any]]
    changed_config_keys: list[str]


class WorkflowComparisonResponse(BaseModel):
    """The wire shape of `src.agents.versions.WorkflowComparison` — V2's graph."""

    changed_fields: dict[str, tuple[Any, Any]]
    added_node_ids: list[str]
    removed_node_ids: list[str]
    changed_nodes: dict[str, WorkflowNodeDiffResponse]
    added_edges: list[WorkflowEdgeRefResponse]
    removed_edges: list[WorkflowEdgeRefResponse]


class VersionComparisonResponse(BaseModel):
    """What two versions of one agent differ by, document by document — see #55.

    `spec` and `workflow` are independently nullable because a version carries
    whichever documents its stage has reached (ADR-0020 §5): a V1 pair diffs a
    pipeline and has no graph to speak of, a V2 pair the other way round. `null`
    means neither version had that document; `skipped` is the other case, where
    exactly one of them did and the reason is spelled out rather than dropped.

    The two ids travel on the response so a screen rendering a diff can always
    say which direction it is reading — a `[before, after]` pair with no
    statement of what "before" was is a pair a reader has to guess at.
    """

    version_a_id: int
    version_b_id: int
    spec: SpecComparisonResponse | None
    workflow: WorkflowComparisonResponse | None
    skipped: dict[str, str]


class AssignableModelResponse(BaseModel):
    """One model the admin may put on the allowlist, and how far it is trusted.

    The tier travels with the name because it is the reason a model may or may
    not be given tools (ADR-0001). An admin about to drop the last strong model
    should be able to see that on the screen, not discover it later as a spec
    rejected for a reason that names the spec.
    """

    name: str
    tier: ModelTier


class PlatformConfigResponse(BaseModel):
    """The admin config screen's whole state — see #12.

    Carries `available_models` as well as the current allowlist, so the form is
    a set of choices rather than a text box: the first thing typed into a text
    box is a model nobody vetted, and vetting is a person's job.

    There is deliberately no field here for the meta-agent's model. It is fixed
    in code, outside `ASSIGNABLE_MODELS` entirely, and a setting that does not
    exist is the only kind nobody can change by accident.
    """

    # `model_allowlist` starts with pydantic's protected `model_` prefix. The
    # name is the domain's and the wire format is not the place to rename it.
    model_config = ConfigDict(protected_namespaces=())

    eval_pass_threshold: float = Field(..., description="Tỷ lệ case eval phải đạt, từ 0 đến 1")
    model_allowlist: list[str] = Field(..., description="Model một spec mới được phép gán")
    available_models: list[AssignableModelResponse] = Field(
        ..., description="Toàn bộ model đã kiểm chứng — allowlist chỉ có thể là tập con của danh sách này"
    )
    updated_at: UtcTimestamp


class EvalScoreResponse(BaseModel):
    """A score beside the bar it is being judged against — see #14 / ADR-0012.

    Never the bare fraction. `passed_count/total_count` on its own is a number
    two readers will disagree about, and the whole reason
    `src.evals.results.Score` carries its threshold is that the same run is
    legitimately measured against two different ones.

    Distinct from `EvalRunResponse`, which carries every case's verdict. This is
    the summary an audit entry or a queue row shows, where the per-case reasons
    would be noise.
    """

    passed_count: int
    total_count: int
    pass_rate: float
    threshold: float = Field(..., description="Ngưỡng con số này đang được đo với")
    meets_threshold: bool


class RequestedToolResponse(BaseModel):
    """One tool permission this version is asking for — see #14.

    `risk` comes from the tool registry and never from the spec, which is what
    lets a screen highlight it: an `AgentSpec` cannot talk a tool into a milder
    level (`src.agents.tools.registry`, rule 1), so the flag on the screen is a
    fact about the code rather than a claim by the agent that wants the tool.
    """

    name: str
    description: str
    risk: ToolRisk = Field(..., description="`high` = việc đáng dừng lại đọc kỹ")
    has_side_effects: bool = Field(..., description="True = tool tác động ra thế giới bên ngoài")
    requested_by_step_ids: list[str] = Field(..., description="Bước nào của spec xin tool này")


class RequestedActionResponse(BaseModel):
    """One action a V2 workflow asks permission to call — see #49, ADR-0023 §2.

    `RequestedToolResponse`'s V2 counterpart, and different in the two ways
    ADR-0023 changed the question. A permission is the *pair* `tool_id.action`,
    because a connection called `google_workspace` says nothing about whether the
    run reads an inbox or sends an email; and `scopes` stands where V1 put
    `allowed_domains`, because a connection reaches outside the organisation
    through an OAuth grant rather than through a domain allowlist.

    `risk` and `requires_human_approval` are null together, and only for an action
    nobody has registered — a state a confirmed graph really can be in
    (`src.workflow.validation` warns and does not refuse). Null is not `low`: "we
    cannot vouch for this" and "this is safe" are opposite things to show a
    reviewer, and a screen must be able to tell them apart.
    """

    name: str = Field(..., description="Tên trên transcript, dạng `tool_id.action` — khớp đúng dòng `tool`")
    tool_id: str
    action: str
    tool_name: str = Field(..., description="Tên kết nối cho người đọc; rỗng nếu registry chưa biết kết nối này")
    description: str = Field(..., description="Action này làm gì; rỗng nếu registry chưa biết action này")
    risk: RiskLevel | None = Field(
        default=None, description="`high` = việc đáng dừng lại đọc kỹ; null = registry không tra được"
    )
    requires_human_approval: bool | None = Field(
        default=None, description="Có bắt buộc một người đứng trước không; null = registry không tra được"
    )
    scopes: list[str] = Field(..., description="Phạm vi quyền kết nối này xin — chỗ V2 thay cho `allowed_domains`")
    requested_by_node_ids: list[str] = Field(..., description="Node nào của graph gọi action này")


class EvalTallyResponse(BaseModel):
    """One grader's share of a run: how many passed, out of how many were asked.

    No rate and no threshold, unlike `EvalScoreResponse`. The bar is a statement
    about the whole set (ADR-0011 §5), and half a run measured against it would
    read as a second verdict on a version that only ever gets one.
    """

    passed_count: int
    total_count: int


class EvalBreakdownResponse(BaseModel):
    """Who decided what, on one eval run — see #49 / ADR-0028 §2.

    The score alone cannot answer the question an admin actually has. *"4/5 đạt"*
    is one sentence over two different kinds of verdict: an opinion an LLM formed
    about an answer, and a fact read straight off the transcript. The first is
    what `eval_override` exists for, because a judge wobbles; the second is what
    it may never touch.

    `safety_failures` is deliberately not "the rule-based cases that failed": a
    workflow that never reached its `End` fails a rule and is merely broken, and
    only a forbidden action or an action after a refusal is unwaivable
    (ADR-0028 §6).
    """

    rule_based: EvalTallyResponse = Field(..., description="Case chấm bằng cách đọc thẳng transcript, không qua judge")
    judged: EvalTallyResponse = Field(..., description="Case do LLM judge chấm")
    safety_failures: int = Field(
        ..., description="Case trượt vì vi phạm luật cứng — không ai vượt rào được, kể cả admin"
    )


class FlowNodeResponse(BaseModel):
    """One step on the flow diagram, with what it runs on and what it may reach for."""

    step_id: str
    title: str
    model: str
    tools: list[str]


class FlowEdgeResponse(BaseModel):
    """One join on the flow diagram: this step runs, then that one."""

    from_step_id: str
    to_step_id: str


class FlowDiagramResponse(BaseModel):
    """The spec as the graph it executes as.

    `nodes` are in declaration order, so `nodes[0]` is where a run starts — there
    is deliberately no separate "start" marker saying the same thing twice. A
    node with no outgoing edge is where a run ends.
    """

    nodes: list[FlowNodeResponse]
    edges: list[FlowEdgeResponse]


class VersionFlowResponse(BaseModel):
    """One version drawn as a graph, plus every permission it is asking for — see #20.

    The two travel together because they are the two halves of one question. A
    creator looking at the diagram asks "what does step 2 do", and the next
    thing they ask is "so what is this agent allowed to touch, all told" — and a
    screen that had to fetch that separately would be a screen where the answer
    sometimes arrives and sometimes does not.

    Both are things the spec screen cannot work out for itself. `next=None` means
    "the step after this one in the list", a rule `AgentSpec` owns; `risk` is
    assigned by whoever wrote the tool and never by the spec asking for it.
    Deriving either in the browser is how a diagram starts disagreeing with the
    runtime about what an agent does.

    Deliberately not the whole dossier. This is the creator's own draft before
    anybody has reviewed it: there is no eval, usually no trial run, and nothing
    for an approval decision to be about yet.
    """

    version_id: int
    flow: FlowDiagramResponse
    tools: list[RequestedToolResponse] = Field(..., description="Rủi ro cao xếp trước — chỗ đáng đọc kỹ nằm trên cùng")


class ApprovalDecisionResponse(BaseModel):
    """One decision an admin made, as the audit history shows it — see #14.

    `eval_score` is the snapshot taken when the decision was made, not a fresh
    reading. That is the whole point of storing it: the gate consults the latest
    eval run, so a creator who evaluated again afterwards would otherwise rewrite
    what a past decision appears to have been made against.
    """

    id: int
    kind: ApprovalDecisionKind
    reason: str | None = Field(default=None, description="Bắt buộc với từ chối và vượt rào; null với duyệt")
    admin_email: str = Field(..., description="Ai quyết định — một bản ghi kiểm toán phải có người chịu trách nhiệm")
    eval_score: EvalScoreResponse | None = Field(
        default=None, description="Điểm eval tại đúng thời điểm quyết định, không đọc lại từ eval_runs"
    )
    created_at: UtcTimestamp


class ApprovalQueueEntryResponse(BaseModel):
    """One row of the admin's approval queue — see #14 / ADR-0012 §4.

    Enough to choose what to open next and nothing more. `high_risk_tools` is
    here rather than only in the dossier because it is what an admin picks by:
    given six versions and an afternoon, the one asking to send email is the one
    to read first.
    """

    version_id: int
    agent_id: int
    agent_name: str
    owner_email: str
    state: QueueState = Field(
        ...,
        description=(
            "`awaiting_decision` = đang chờ duyệt hoặc từ chối; "
            "`blocked_by_eval` = điểm eval chặn, chỉ admin vượt rào mới đi tiếp được"
        ),
    )
    score: EvalScoreResponse | None = Field(default=None, description="Null nếu version chưa chạy eval lần nào")
    blocked_reason: str | None = Field(
        default=None, description="Vì sao version này chưa xin duyệt được; null với version đã ở trong hàng chờ"
    )
    high_risk_tools: list[str]
    created_at: UtcTimestamp


class ApprovalDecidedEntryResponse(BaseModel):
    """One review that is over — see ADR-0064.

    A separate shape from `ApprovalQueueEntryResponse` rather than that one with
    a wider `state`. A waiting row is picked by risk and by how long it has
    waited; a settled row is read for what was decided and by whom. Folding them
    together would mean half the fields being null in each mode, and a `state`
    enum whose docstring — *"the two ways a version can be waiting on an
    admin"* — would have stopped being true.

    **Four fields are nullable because the data really is missing sometimes.** A
    version can reach `approved` with no `ApprovalDecision` behind it (this
    platform's own fleet has two such), so `decided_by`/`decided_at` are `null`
    for it rather than borrowed from `created_at`. An invented decided-at looks
    exactly like a real one; a null can be questioned.
    """

    version_id: int
    agent_id: int
    agent_name: str
    owner_email: str
    status: VersionStatus = Field(..., description="`approved` hoặc `rejected` — chính là quyết định")
    decided_by: str | None = Field(
        default=None, description="Email admin đã quyết; null nếu version tới trạng thái này mà không có bản ghi nào"
    )
    decided_at: UtcTimestamp | None = Field(
        default=None, description="Lúc quyết định được ghi; null cùng lý do với `decided_by` — không suy từ created_at"
    )
    note: str | None = Field(default=None, description="Câu admin để lại; null với duyệt, vốn không cần lý do")
    eval_override_reason: str | None = Field(
        default=None, description="Lý do vượt rào eval nếu từng dùng; null nghĩa là không ai vượt rào"
    )
    score: EvalScoreResponse | None = Field(
        default=None, description="Điểm eval đúng lúc quyết định — bản chụp, không đọc lại từ eval_runs"
    )
    created_at: UtcTimestamp


class ApprovalDossierResponse(BaseModel):
    """Everything an admin needs to decide about one version, in one payload — see #14.

    Wide on purpose. The ticket's argument is that a review spread over five
    screens is a review nobody does properly, and a narrower payload would only
    mean the screen fetching the rest itself.

    Three fields are `null` for three different and real reasons — no trial run,
    no eval, nothing measured — and none of them is ever a zero. "We ran it and
    it was free" is the one thing a measurement must not say when nothing was
    measured (ADR-0009).

    **The documents come in two sets and a version holds one of them** (#49). A
    V1 version is reviewed on `spec`/`flow`/`tools`/`allowed_domains`/`costs`; a
    V2 one on `solution_spec`/`workflow`/`actions`/`budget`. The other set is
    null rather than empty, because a blank graph on the screen would read as a
    workflow nobody drew — and a client tells which review it is looking at by
    which set arrived, not by a flag that could contradict them.
    """

    version_id: int
    agent_id: int
    agent_name: str
    owner_email: str
    status: VersionStatus
    created_at: UtcTimestamp
    spec: AgentSpec | None = Field(default=None, description="Pipeline V1; null với version V2")
    flow: FlowDiagramResponse | None = Field(
        default=None, description="Pipeline V1 vẽ thành graph; null với version V2"
    )
    tools: list[RequestedToolResponse] = Field(..., description="Rủi ro cao xếp trước — chỗ đáng đọc kỹ nằm trên cùng")
    allowed_domains: list[str] = Field(..., description="Domain version này xin để gọi HTTP ra ngoài")
    solution_spec: AgentSolutionSpecification | None = Field(
        default=None, description="Bài toán nghiệp vụ đã xác nhận (V2); null với version V1"
    )
    workflow: WorkflowJSON | None = Field(
        default=None, description="Graph đã xác nhận (V2), nguyên văn tài liệu runtime sẽ chạy; null với version V1"
    )
    actions: list[RequestedActionResponse] = Field(
        default_factory=list,
        description="Quyền V2 xin, đọc từ các node MCP — rủi ro cao trước, rồi tới action registry không tra được",
    )
    budget: WorkflowBudget | None = Field(
        default=None, description="Trần một lần chạy graph (V2) — chỗ V2 thay cho `costs.worst_case`"
    )
    sandbox_run: SandboxRunResponse | None = Field(
        default=None, description="Lần chạy thử gần nhất, kể cả lần hỏng; null nếu chưa chạy thử lần nào"
    )
    eval_run: EvalRunResponse | None = Field(default=None, description="Null nếu version chưa chạy eval lần nào")
    eval_breakdown: EvalBreakdownResponse | None = Field(
        default=None, description="Case nào rule-based, case nào judge chấm; null nếu chưa chạy eval lần nào"
    )
    blocked_reason: str | None = Field(
        default=None,
        description=(
            "Vì sao *version* này chưa qua được ngưỡng eval; null nếu đã qua. "
            "Vẫn hiện với version đã được vượt rào — người duyệt cần thấy khoảng hụt, không chỉ thấy nó đã được mở"
        ),
    )
    costs: VersionCostsResponse = Field(
        ..., description="Ba con số chi phí; với version V2 chỉ `measured` có giá trị, trần nằm ở `budget`"
    )
    rejection_reason: str | None = Field(default=None, description="Lý do lần từ chối gần nhất, nếu có")
    decisions: list[ApprovalDecisionResponse] = Field(..., description="Lịch sử kiểm toán, cũ trước mới sau")


class VersionApprovalResponse(BaseModel):
    """Where one version stands in the approval flow — the creator's own view of it.

    What a creator asks after submitting: has anyone looked, and if they said no,
    what did they say. So it carries the decision history rather than only the
    latest verdict — a version rejected twice for two different reasons is a
    different story from one rejected twice for the same one.

    Readable by the version's owner and by an admin, and by nobody else.
    """

    version_id: int
    status: VersionStatus
    blocked_reason: str | None = Field(
        default=None,
        description=(
            "Vì sao version này chưa xin duyệt được và việc tiếp theo phải làm; null nếu xin duyệt được, "
            "và null với version đã nộp hoặc đã có quyết định. Version chưa chạy thử được trả lời bằng "
            "câu về sandbox, không phải câu về eval — cả hai đều đúng nhưng chỉ câu đầu là việc phải làm"
        ),
    )
    rejection_reason: str | None = Field(default=None, description="Lý do lần từ chối gần nhất, nếu có")
    decisions: list[ApprovalDecisionResponse]


class ApprovalReasonRequest(BaseModel):
    """The reason behind a rejection or an eval override — mandatory, by #14.

    `min_length=1` refuses the empty body early, but it is not the rule: a reason
    of pure whitespace passes here and is refused by `src.agents.versions`, which
    is the one place that decides what counts as a reason. Two independent
    opinions on that would eventually differ, and the one on the screen would be
    whichever happened to run first.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, description="Người tạo sẽ đọc câu này — hãy nói rõ phải sửa gì")


class PlatformConfigUpdateRequest(BaseModel):
    """A change to either setting, or both.

    Both fields are optional and `None` means "leave this one alone", so a form
    that edits the threshold does not have to send back an allowlist it read
    before somebody else changed it.

    `extra="forbid"` so a client naming a setting that does not exist — the
    meta-agent's model, most of all — is told so. A silently ignored field looks
    from the outside exactly like one that was applied.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    eval_pass_threshold: float | None = Field(default=None, description="Ngưỡng mới, từ 0 đến 1")
    model_allowlist: list[str] | None = Field(default=None, description="Allowlist mới, thay thế toàn bộ")


class InterviewStartRequest(BaseModel):
    """The creator's own description of what they need — where an interview begins."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        ...,
        min_length=1,
        description="Mô tả nghiệp vụ bằng tiếng Việt tự nhiên — viết càng kỹ càng ít bị hỏi lại",
    )


class InterviewAnswerRequest(BaseModel):
    """An answer to whichever question the interview is currently asking.

    The question is not named in the body. There is only ever one question open —
    the first empty slot — and letting a client say which one it is answering
    would invite it to answer a question the interview is not asking, or to answer
    a slot the creator already filled.
    """

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(..., min_length=1, description="Trả lời bằng lời của bạn; hệ thống tự đọc ra các slot")


class InterviewSlotUpdateRequest(BaseModel):
    """A slot the creator is correcting by hand — the review screen's edit.

    Accepts either shape and lets `src.interview.slots` decide what the slot wants,
    so a client that sends one sentence for a list slot is understood rather than
    refused on a technicality it cannot see.
    """

    model_config = ConfigDict(extra="forbid")

    value: str | list[str] = Field(..., description="Giá trị mới; slot dạng danh sách nhận mảng chuỗi")


class InterviewSlotResponse(BaseModel):
    """One slot on the review screen: what was asked, and what the system thinks it heard.

    Empty slots are included rather than omitted. *"Xem lại toàn bộ slot"* is asked
    for so a creator can check the system understood them, and a screen that showed
    only what was filled would answer a different question — it would hide the very
    gaps the interview is still working through.
    """

    slot: Slot
    question: str = Field(..., description="Câu hỏi tương ứng, đúng như lúc phỏng vấn hỏi")
    value: str | list[str] | None = Field(..., description="null nghĩa là slot còn trống")
    filled: bool


class InterviewSessionResponse(BaseModel):
    """A whole interview as its screen shows it: what is known, what is left, what is next.

    `next_question` and `remaining` are derived from the slots rather than stored
    beside them, so the progress a creator reads can never disagree with the
    answers they gave. When `complete` is true the first is `null` and the second
    is `0` — there is no third state where the interview is over but still asking.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    description: str = Field(..., description="Mô tả ban đầu, nguyên văn")
    slots: list[InterviewSlotResponse] = Field(..., description="Cả năm slot theo thứ tự phỏng vấn")
    next_question: InterviewSlotResponse | None = Field(
        ..., description="Câu hỏi đang chờ trả lời; null khi đã đủ slot"
    )
    answered: int = Field(..., description="Số slot đã điền")
    remaining: int = Field(..., description="Số slot còn trống — phần 'còn bao nhiêu câu nữa'")
    total: int = Field(..., description="Tổng số slot của phỏng vấn")
    complete: bool = Field(..., description="Đủ slot; đây là lúc chuyển sang sinh spec")
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class GeneratedSpecResponse(BaseModel):
    """What a finished interview turned into: an agent, a draft version, and the spec — see #17.

    All three, because a client that has just generated needs all three and would
    otherwise fetch the other two straight back. The agent is where the wizard goes
    next, the version is what the refine loop (#18) adds to, and the spec is the
    only thing on the screen a creator can actually read.

    `status` is carried rather than assumed to be `draft`. It comes off the row
    that was written, so a screen showing it can never claim a state the lifecycle
    did not give it.
    """

    agent_id: int
    version_id: int
    status: VersionStatus
    spec: AgentSpec = Field(..., description="Spec vừa sinh, đã qua validate — đúng thứ được lưu vào version")
    created_at: UtcTimestamp


class RefineVersionRequest(BaseModel):
    """One turn of refining an agent by chat — see #18.

    One field, and that is the argument the ticket makes: a whole form of
    add/remove/reorder widgets for a nested structure, replaced by the sentence a
    creator would have said out loud anyway. There is no second edit path beside
    it — the flow diagram is read-only — because two of them would fight, and the
    chat turn would win by overwriting whatever the other one had just changed.
    """

    instruction: str = Field(
        ...,
        description='Câu người tạo nói, ví dụ "bỏ bước 3" hay "bước 2 thêm quyền tra cứu đơn hàng"',
    )
    """No `min_length`, deliberately. An empty box and a box holding two spaces are
    the same mistake, and `BlankInstructionError` already answers it in the
    creator's own language (ADR-0016 §4). A length constraint here would intercept
    exactly one of the two and answer it with pydantic's English error list — the
    wrong sentence, on the more likely of the two paths."""


class RefinedSpecResponse(GeneratedSpecResponse):
    """What one refinement turn produced — the generated shape, plus where it came from.

    The same payload as generation because it *is* the same event: a new draft
    version of one agent, carrying a validated spec. Only one thing is new, and it
    is the thing version history is read with — which version this one was refined
    from, so a client can ask for the diff without guessing that "the previous one
    by date" is the right pair.

    Nothing is stored for it: the parent is what the request named, and a column
    recording it would be a second answer to a question the URL already answers.
    """

    refined_from_version_id: int = Field(
        ..., description="Version bản nháp này được tinh chỉnh từ — vẫn còn nguyên, không bị sửa"
    )


# --------------------------------------------------------------------------- #
# V2 — the AI Solution Designer interview and the spec it produces (#42)
# --------------------------------------------------------------------------- #


class SolutionInterviewStartRequest(BaseModel):
    """The creator's own description of their business problem — where V2 begins."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        ...,
        min_length=1,
        description="Mô tả bài toán nghiệp vụ bằng ngôn ngữ tự nhiên — viết càng kỹ càng ít bị hỏi lại",
    )


class SolutionInterviewAnswerRequest(BaseModel):
    """An answer to whichever question the interview is currently asking.

    The question is not named in the body. There is only ever one open, and
    letting a client say which one it is answering would invite it to answer a
    question the interview is not asking.
    """

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(..., min_length=1, description="Trả lời bằng lời của bạn; hệ thống tự đọc ra các trường")


class SolutionQuestionResponse(BaseModel):
    """The one question waiting for an answer, and which field it is about.

    `field` is `null` for the question that is not about a single field: the one
    that puts a contradiction back to the creator, where two fields disagree and
    neither on its own is the thing to change.
    """

    field: SpecField | None = Field(..., description="Trường câu hỏi nhắm tới; null khi hỏi về một mâu thuẫn")
    text: str


class SolutionFieldResponse(BaseModel):
    """One of the seven fields on the review screen: what it asks, and whether it is answered.

    All seven are listed whether or not they hold anything, for the reason V1's
    slot list gives: a screen showing only what was filled would hide the very
    gaps the interview is still working through.
    """

    field: SpecField
    question: str = Field(..., description="Câu hỏi tương ứng của trường này")
    filled: bool


class SolutionSpecDraftResponse(BaseModel):
    """The seven fields as the interview currently holds them — the half-filled spec.

    Typed the same as `AgentSolutionSpecification` except that every field may be
    empty, which is exactly the difference between a draft and a confirmed spec
    (ADR-0020 §3). A client can render one screen for both.
    """

    goal: str | None = Field(..., description="null khi chưa trả lời")
    users: list[str]
    data: list[DataSource]
    actions: list[str]
    expected_outcome: list[str]
    success_criteria: list[SuccessCriterion]
    prohibitions: list[str]


class SolutionTurnResponse(BaseModel):
    """One exchange of the interview, kept so a resumed session reads as a conversation."""

    question: str
    answer: str


class SolutionInterviewResponse(BaseModel):
    """A whole V2 interview as its screen shows it — see #42.

    Carries both halves of the ticket's first criterion at once: `complete` says
    whether a spec can be built now, and `fields` plus `conflicts` are the list of
    missing or conflicting points to clarify when it cannot.

    `answered`, `remaining` and `complete` are all derived from the draft rather
    than stored beside it, so the progress a creator reads can never disagree with
    the answers they gave. Unlike V1 they are **not** a promise about how many
    questions are left: a contradiction can keep the interview open with every
    field answered (ADR-0020 §3).
    """

    id: int
    description: str = Field(..., description="Mô tả ban đầu, nguyên văn")
    draft: SolutionSpecDraftResponse = Field(..., description="Bảy trường với những gì đã hiểu được")
    fields: list[SolutionFieldResponse] = Field(..., description="Cả bảy trường theo thứ tự phỏng vấn")
    open_question: SolutionQuestionResponse | None = Field(..., description="Câu hỏi đang chờ trả lời; null khi đã đủ")
    conflicts: list[str] = Field(..., description="Những chỗ mâu thuẫn còn phải làm rõ")
    turns: list[SolutionTurnResponse] = Field(..., description="Các lượt hỏi–đáp đã diễn ra")
    answered: int = Field(..., description="Số trường đã trả lời")
    remaining: int = Field(..., description="Số trường còn trống")
    total: int = Field(..., description="Tổng số trường của đặc tả")
    complete: bool = Field(..., description="Đủ trường và không còn mâu thuẫn — lúc dựng được đặc tả")
    agent_id: int | None = Field(..., description="Agent phiên này đã sinh ra, nếu đã sinh")
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class SolutionSuggestion(BaseModel):
    """Một câu trả lời gợi ý cho câu hỏi phỏng vấn đang mở — người tạo bấm chọn hoặc bỏ qua.

    `recommended` đúng cho nhiều nhất một phương án: cái model xếp đầu và đủ tự tin
    là câu trả lời đúng. Không có cái nào `recommended` là chuyện bình thường.
    """

    text: str = Field(..., description="Nội dung trả lời, đưa thẳng vào câu trả lời phỏng vấn")
    recommended: bool = Field(..., description="Phương án khuyến nghị; nhiều nhất một cái true")


class SolutionSuggestionsResponse(BaseModel):
    """Gợi ý trả lời cho câu hỏi đang mở của một phiên phỏng vấn — kiểu recommend của Claude Code.

    Danh sách rỗng là kết quả hợp lệ: mô tả chưa đủ tín hiệu cho trường này, câu
    hỏi đang hỏi về một mâu thuẫn (không gợi ý được), hoặc phỏng vấn đã xong. Màn
    hình khi đó chỉ còn ô nhập tay.
    """

    field: SpecField | None = Field(
        ..., description="Trường câu hỏi nhắm tới; null khi hỏi về mâu thuẫn hoặc phỏng vấn đã xong"
    )
    multi: bool = Field(..., description="Trường dạng danh sách → chọn được nhiều gợi ý một lúc")
    suggestions: list[SolutionSuggestion]


class SolutionSpecResponse(BaseModel):
    """An Agent Solution Specification and the version stage it is sitting at — see #42.

    `status` is carried rather than assumed, because it is the whole meaning of
    the payload: the same seven fields are a proposal at
    `spec_pending_confirmation` and an immutable contract at `spec_confirmed`.
    """

    agent_id: int
    version_id: int
    version_number: int = Field(
        ..., description="Version thứ mấy của agent này — số để hiển thị, xem AgentVersionResponse.number"
    )
    status: VersionStatus
    spec: AgentSolutionSpecification = Field(..., description="Bảy trường, đã qua validate")
    created_at: UtcTimestamp


# V2 — the workflow the Generator drafts from a confirmed spec (#43)


class WorkflowDraftRequest(BaseModel):
    """How the creator wants their first workflow made — ADR-0041.

    One optional field, and its absence is the interesting state: no
    `template_id` means *compose one for my problem*, which is what a creator who
    described a business and never opened the gallery is asking for. Naming a
    template means the opposite and means it deliberately — they picked a shape
    off a shelf and want it filled in.

    Optional rather than required-with-a-null, because the whole request body is
    optional: `POST .../workflow` with no body at all is the common case and has
    to keep working exactly as it did before this schema existed.
    """

    template_id: str | None = Field(
        default=None,
        description="Mẫu người tạo chọn trong thư viện; bỏ trống thì hệ thống tự soạn workflow từ đặc tả",
    )


class WorkflowTemplateResponse(BaseModel):
    """One entry in the template gallery, as the screen that offers it reads.

    `description` is the same sentence the platform's own selection fallback
    reads, and that is deliberate: a template's account of what it is for should
    not have one wording for a model and another for a person, because then only
    one of them is kept up to date.

    `node_count` rather than the nodes themselves. What a creator needs from a
    gallery is how big a thing they are about to start from; the graph itself
    arrives the moment they pick it, and shipping every skeleton in the list
    would be a payload nobody reads to answer a question nobody asked.
    """

    id: str
    name: str
    description: str = Field(..., description="Mẫu này hợp với bài toán nào")
    required_data_kinds: list[DataSourceKind] = Field(
        ..., description="Loại nguồn dữ liệu mẫu này cần có thì mới dùng được"
    )
    node_count: int = Field(..., description="Số node của bộ khung, để người tạo ước lượng độ lớn")
    source: str = Field(..., description="built_in hay creator_submitted")


class WorkflowProblemResponse(BaseModel):
    """One thing standing between a draft and confirmation, and where it is.

    Carried on the read rather than left for the client to re-derive: the same
    list is what `ensure_confirmable` will refuse the transition on (#44), and a
    canvas computing its own version of it would draw warnings the backend does
    not agree with.
    """

    message: str = Field(..., description="Câu mô tả lỗi, viết cho người tạo đọc")
    node_id: str | None = Field(..., description="Node cần sửa, nếu lỗi thuộc về một node")
    edge_id: str | None = Field(..., description="Cạnh cần sửa, nếu lỗi thuộc về một cạnh")


class WorkflowDraftResponse(BaseModel):
    """A version's workflow, the stage it sits at, and what it still needs — see #43.

    `status` is carried for the reason `SolutionSpecResponse` carries it: the
    same document means different things at different stages — editable draft at
    `workflow_pending_confirmation`, immutable contract once confirmed.

    `problems` is expected to be non-empty on a fresh draft. The Generator fills
    what the spec answers and leaves the rest blank rather than inventing it, so
    a workflow arriving with a few unfilled fields is the normal outcome, not a
    failed generation.

    `warnings` is a separate list and not more `problems`, because the two lead
    to different places: every problem blocks confirmation, and no warning does.
    Merging them would leave the canvas unable to tell a creator which of the
    sentences on their screen is the one standing between them and a confirmed
    workflow.
    """

    agent_id: int
    version_id: int
    version_number: int = Field(
        ..., description="Version thứ mấy của agent này — số để hiển thị, xem AgentVersionResponse.number"
    )
    status: VersionStatus
    workflow: WorkflowJSON = Field(..., description="WorkflowJSON, đã qua validate")
    problems: list[WorkflowProblemResponse] = Field(..., description="Những chỗ phải sửa trước khi xác nhận được")
    warnings: list[WorkflowProblemResponse] = Field(
        default_factory=list, description="Cảnh báo nên xem nhưng không chặn xác nhận"
    )
    created_at: UtcTimestamp


class WorkflowRefineRequest(BaseModel):
    """One turn of editing a workflow graph by chat — the V2 sibling of `RefineVersionRequest`.

    Same one field, for the same reason, and with the same absence of a
    `min_length`: `BlankGraphInstructionError` answers an empty box in the
    creator's own language, and a constraint here would answer it in pydantic's.
    """

    instruction: str = Field(
        ...,
        description='Câu người tạo nói, ví dụ "thêm bước duyệt tay trước khi gửi mail"',
    )


class WorkflowRefinementResponse(BaseModel):
    """The graph one chat turn proposes — and nothing else, because nothing was saved.

    Deliberately **not** a `WorkflowDraftResponse`. That shape carries `status`,
    `problems` and `version_id`, all of which are facts about a document the
    server has stored; this endpoint stores nothing, so every one of them would
    be an answer about the *old* graph sitting beside the new one. The canvas
    applies this graph and its ordinary save is what asks the server what is
    wrong with it — one place that answers that question, as before.
    """

    workflow: WorkflowJSON = Field(..., description="Workflow sau khi sửa, đã qua validate — chưa được lưu")


class WorkbenchTurnRequest(BaseModel):
    """One turn of the meta-agent loop — ADR-0050's successor to `WorkflowRefineRequest`.

    `viewing_run_id` is the diagnosis door: set when the creator has a run open,
    so the meta agent knows it may read that transcript before answering.
    """

    loi_nhan: str = Field(..., description="Câu người tạo nói với meta agent")
    viewing_run_id: str | None = Field(
        default=None, description="Id lần chạy đang mở trên màn hình, nếu có — cửa vào chẩn đoán"
    )


class WorkbenchStepResponse(BaseModel):
    """One thing the meta agent did during its turn — its own evidence trail."""

    tool: str
    ke: str = Field(..., description="Một dòng kể việc vừa làm, hiện trong khung chat")
    ok: bool = True


class WorkbenchTurnResponse(BaseModel):
    """What one meta-agent turn concluded. Like `WorkflowRefinementResponse`,
    nothing here was saved: `workflow` is a proposal the canvas applies (with
    undo) and the ordinary save stores."""

    loi_nhan: str = Field(..., description="Meta agent nói nó đã làm gì và vì sao")
    cau_hoi: str | None = Field(default=None, description="Câu hỏi meta agent cần người tạo trả lời, nếu có")
    workflow: WorkflowJSON | None = Field(default=None, description="Bản graph đề xuất — null nếu lượt chỉ nói")
    van_de: list[str] = Field(default_factory=list, description="Vấn đề validator còn phàn nàn trên bản đề xuất")
    cac_buoc: list[WorkbenchStepResponse] = Field(
        default_factory=list, description="Vết từng bước meta agent đã làm trong lượt"
    )


class NodeTypeResponse(BaseModel):
    """One of the eight node types, as the canvas needs it — see #44, ADR-0021.

    Three fields, and each answers one thing the canvas would otherwise have to
    know by heart: `type` is the palette entry it drags, `outputs` are the
    handles it draws and the port names its edges carry, and `config_schema` is
    the property form it renders.

    Served rather than hard-coded in the browser, which is the whole point of
    `NODE_REGISTRY` being a closed table (ADR-0021 §1). A second copy in
    TypeScript would drift into a canvas offering a port the validator refuses,
    with nothing on screen saying why — and the drift would show up at confirm
    time, on a graph the creator had already finished.

    `config_schema` is passed through as the JSON Schema Pydantic derives from
    the type's `NodeConfig`, field descriptions and all. Those descriptions are
    the labels beside the inputs, so the wording a creator reads on the canvas is
    the wording written once on the model.
    """

    type: NodeType = Field(..., description="Loại node, khoá của NODE_REGISTRY")
    outputs: list[str] = Field(..., description="Các cổng output có tên mà cạnh được đi ra từ đó")
    config_schema: dict[str, Any] = Field(..., description="JSON Schema của config, panel thuộc tính dựng form từ đây")


class WorkflowChatRequest(BaseModel):
    """One turn of a chat with a confirmed workflow — see #45.

    No `namespace`, for `SandboxRunRequest`'s reason and one more. A workflow that
    is only `workflow_confirmed` has not been approved and is not deployed to
    anybody, so every run of it is the creator trying their own graph out: putting
    those turns anywhere near an agent's production history would let a rehearsal
    become the memory a real conversation continues from.
    """

    message: str = Field(..., min_length=1, description="Câu người dùng gửi cho workflow")
    conversation_id: str | None = Field(
        default=None,
        pattern=CONVERSATION_ID_PATTERN.pattern,
        description="Thread cần tiếp tục; bỏ trống để mở một hội thoại mới",
    )


class ApprovalDecisionRequest(BaseModel):
    """One person's answer to a run waiting on them — see #47 / ADR-0024.

    `approved` is a boolean rather than a status string, so there is no third
    value to invent and no spelling to get wrong: a `HumanApproval` node has
    exactly two ports.

    `reason` is optional here and mandatory in the domain for a rejection —
    `src.workflow.approvals.decide_approval` is where that is enforced, for
    `ApprovalDecision`'s reason: the rule is about a decision, not about a
    request body, and enforcing it at the boundary would leave the service layer
    able to record an unexplained "no" from any other caller.
    """

    approved: bool = Field(..., description="True = cho chạy tiếp qua cổng 'approved'")
    reason: str | None = Field(default=None, description="Bắt buộc khi từ chối — nhánh 'rejected' còn dựa vào đó")


class TemplateSubmissionRequest(BaseModel):
    """A creator's "save as template" ask — see #53, ADR-0025 §7.

    The graph itself is never in this body. It comes from the `workflow_confirmed`
    version named in the URL, copied and stripped server-side
    (`src.workflow.templates.strip_for_submission`) — a request that could name
    its own `{nodes, edges}` would be exactly the freehand submission path
    ADR-0025 §7 rejects.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Tên template, người tạo tự đặt")
    description: str = Field(
        ..., min_length=1, description="Template này hợp với bài toán nào — Generator đọc câu này để chọn"
    )
    required_data_kinds: list[DataSourceKind] = Field(
        default_factory=list, description="Loại nguồn dữ liệu template này cần, nếu có"
    )


class TemplateSubmissionResponse(BaseModel):
    """One creator-submitted template, wherever it is in admin review — see #53, ADR-0025 §6-8.

    One shape for both audiences, the same choice `VersionApprovalResponse`
    makes: the creator reading where their own submission stands and the admin
    reading the review queue see the same fields, just scoped by which endpoint
    they reached it through.

    `nodes`/`edges`/`start_node_id` are the stripped skeleton (§8) — placeholders
    where the source workflow had a resource reference or a business sentence,
    everything else kept — which is what the admin review queue (criterion 4)
    reads to show what this template actually is before deciding on it.
    """

    id: int
    submitted_by: int
    source_version_id: int | None = Field(
        default=None, description="Version workflow_confirmed đã copy ra template này, nếu còn"
    )
    name: str
    description: str
    nodes: list[NodeInstance]
    edges: list[Edge]
    start_node_id: str
    required_data_kinds: list[DataSourceKind]
    status: TemplateStatus
    rejection_reason: str | None = Field(default=None, description="Lý do từ chối, nếu có")
    created_at: UtcTimestamp


class ToolCredentialRequest(BaseModel):
    """How a creator's MCP connection authenticates — the *shape* of the secret, never the secret.

    Deliberately not `src.workflow.tools.ToolCredential` reused as a body. That
    type is what the registry holds; this is what a browser may send, and the
    difference matters the day the registry grows a field no client should be
    able to set. `secret_ref` names a key in the backend secret store, which is
    where the Publish security requirement puts credentials — a body carrying an
    actual token would be a token in a request log.
    """

    model_config = ConfigDict(extra="forbid")

    kind: CredentialKind = Field(..., description="Kiểu xác thực của kết nối này")
    provider: str = Field(default="", max_length=100, description="Nhà cung cấp OAuth, ví dụ 'google'")
    scopes: list[str] = Field(default_factory=list, description="Quyền OAuth kết nối này xin")
    secret_ref: str = Field(default="", max_length=200, description="Khoá trong secret store, không phải bí mật")
    #: The one field that *is* the secret, and the only one — Agent Builder's
    #: "Access token / API key" box. Accepted here so a creator can paste a token
    #: while registering instead of needing an operator to seed the store first;
    #: the route hands it straight to `EncryptedDbSecretStore` and keeps only the
    #: generated `secret_ref`. It is never stored on the row, never returned by
    #: any response model, and never logged.
    secret: str = Field(
        default="",
        max_length=8000,
        description="Token/API key dán vào — đi thẳng vào secret store, không lưu trên hàng",
    )
    #: Agent Builder's third auth choice. Values are secrets too, so the whole
    #: mapping is JSON-encoded into the secret store under one key rather than
    #: kept on the row.
    headers: dict[str, str] = Field(
        default_factory=dict, max_length=20, description="Header tuỳ chỉnh, ví dụ {'X-Api-Key': '...'}"
    )


class ToolRegistrationRequest(BaseModel):
    """A creator registering one MCP connection — #52, ADR-0023 §7.

    **There is no `actions` field, and that is the point.** The system calls
    `tools/list` on `server_url` and reads the actions off the answer; a body
    that could name its own actions and schemas would let the registry drift
    away from what the server implements, with the drift surfacing as a workflow
    calling an action that is not there. `extra="forbid"` is what makes the
    absence enforced rather than merely documented.

    `mock_response_examples` is optional here because a creator generally learns
    which actions need one *by* registering — introspection is what tells them.
    They fill the gaps afterwards through the mocks endpoint, and approval is
    where the requirement is finally collected (ADR-0023 §4).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200, description="Tên kết nối, người tạo tự đặt")
    server_url: str = Field(..., min_length=1, max_length=2000, description="Địa chỉ MCP server để introspect")
    description: str = Field(default="", max_length=500, description="Mô tả kết nối này dùng để làm gì")
    credential: ToolCredentialRequest
    usage_scope: UsageScope = Field(default=UsageScope.PUBLIC, description="Ai được dùng kết nối này")
    mock_response_examples: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="Ví dụ kết quả cho sandbox, khoá là tên action"
    )
    allowed_actions: list[str] = Field(
        default_factory=list,
        description="Chỉ các action này được dùng; rỗng = tất cả. Tương ứng `allowed_tools` của Agent Builder",
    )


class ToolAllowedActionsRequest(BaseModel):
    """Which of a connection's introspected actions it actually offers.

    Its own endpoint rather than a field a creator edits in place, for
    `ToolMockResponsesRequest`'s reason: they cannot name the subset until
    introspection has told them what the whole set is, so the natural order is
    register, read `actions[]`, then narrow.

    Replaced rather than merged — unlike the mocks. An allowlist *is* the whole
    answer, and merging would leave no way to remove an action through the only
    door there is. An empty list therefore means "no narrowing", which is both
    the default and the way back.
    """

    model_config = ConfigDict(extra="forbid")

    actions: list[str] = Field(..., description="Tên các action được phép dùng; danh sách rỗng = cho phép tất cả")


class ToolMockResponsesRequest(BaseModel):
    """The sandbox answers a creator supplies once introspection has named the actions.

    Merged into whatever the row already has rather than replacing it, so
    supplying the second of three mocks does not drop the first — see
    `src.workflow.registrations.set_mock_responses`.
    """

    model_config = ConfigDict(extra="forbid")

    examples: dict[str, dict[str, Any]] = Field(..., description="Ví dụ kết quả, khoá là tên action")


class ToolRiskOverrideRequest(BaseModel):
    """An admin's reviewed verdict on one action's risk — the *only* thing a review may change.

    There is no sibling request for the schema. ADR-0023 §7 lets the admin
    override risk and nothing else, and the way that rule is kept is that no
    endpoint exists to break it.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., min_length=1, description="Tên action muốn đổi mức rủi ro")
    risk_level: RiskLevel = Field(..., description="Mức rủi ro admin tự đặt sau khi xem")


class ToolActionResponse(BaseModel):
    """One action of a connection, with everything the platform decided about it.

    `risk_level`, `sandbox_behavior` and `requires_human_approval` are all
    *derived* (ADR-0023 §3-5) and appear together because a screen showing risk
    without its consequences would leave a reader to re-derive them — and the
    two derivations would eventually disagree.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = Field(description="JSON Schema tham số, do chính server khai")
    output_schema: dict[str, Any] = Field(description="JSON Schema kết quả, do chính server khai")
    read_only: bool
    risk_level: RiskLevel
    sandbox_behavior: SandboxBehavior
    requires_human_approval: bool


class ToolActionReviewResponse(ToolActionResponse):
    """One action as the admin review queue reads it — criterion 3.

    Adds the three things that only matter while a connection is still being
    decided on: whether this risk is already somebody's override rather than the
    computed answer, what the sandbox would reply, and whether that reply is
    still missing — which is what will refuse the approval.
    """

    overridden: bool = Field(description="True = mức rủi ro này do admin đặt, không phải tính ra")
    needs_mock: bool = Field(description="True = sandbox phải chạy bản mô phỏng nên phải có ví dụ kết quả")
    mock_response_example: dict[str, Any] | None = Field(default=None, description="Ví dụ kết quả, nếu đã có")


class ToolCredentialResponse(BaseModel):
    """How a registered connection authenticates, as anybody reading it may see.

    A separate type from `ToolCredentialRequest` rather than the same one used
    both ways, and the difference is the whole point: the request carries
    `secret` and `headers` — the values themselves — so a response reusing it
    would be one populated field away from returning a token to a browser. The
    fields a *reader* is entitled to are the shape of the credential and the key
    it is filed under, never the value; leaving the value out of the type makes
    that structural instead of a rule somebody has to keep remembering.
    """

    model_config = ConfigDict(extra="forbid")

    kind: CredentialKind
    provider: str = ""
    scopes: list[str] = Field(default_factory=list)
    secret_ref: str = Field(default="", description="Khoá trong secret store — không bao giờ là chính bí mật")


class ToolRegistrationResponse(BaseModel):
    """One registered connection, wherever it is in review — #52, ADR-0023 §7.

    One shape for both audiences, as `TemplateSubmissionResponse` is: the
    creator reading where their own submission stands and the admin reading the
    queue see the same fields, scoped by which endpoint they reached it through.

    `tool_id` is here rather than left to be assembled by a client, because it is
    what a workflow's `MCP` node will actually carry once this is approved — a
    frontend building that string itself would be a second place the id format
    is written down.
    """

    id: int
    tool_id: str = Field(description="Id `McpConfig.tool_id` sẽ trỏ tới khi kết nối được duyệt")
    submitted_by: int
    name: str
    server_url: str
    description: str = ""
    credential: ToolCredentialResponse
    usage_scope: UsageScope
    actions: list[ToolActionReviewResponse]
    allowed_actions: list[str] = Field(
        default_factory=list,
        description="Action được phép dùng; rỗng = tất cả. `actions` vẫn là báo cáo đầy đủ từ server",
    )
    status: ToolStatus
    rejection_reason: str | None = Field(default=None, description="Lý do từ chối, nếu có")
    created_at: UtcTimestamp


class ToolCatalogEntryResponse(BaseModel):
    """One connection an `MCP` node may currently be pointed at — criterion 5.

    Both halves of the registry in one list and one shape: a built-in connection
    and an approved creator-registered one are indistinguishable here except by
    `source`, which is ADR-0023 §1's "the storage split is not a schema
    divergence" as the canvas sees it.
    """

    id: str
    name: str
    family: ToolFamily
    source: ToolSource
    usage_scope: UsageScope
    actions: list[ToolActionResponse]


class EmbeddingModelUsageResponse(BaseModel):
    """What one embedding model has spent against its allowance.

    Exists because no provider offers this (ADR-0045 §8): Voyage publishes a
    per-model grant and no endpoint to ask how much is left, OpenAI simply
    bills. So the platform counts, and this is how the count is read.
    """

    model_config = ConfigDict(extra="forbid")

    model_name: str = Field(..., description="Tên model, ví dụ text-embedding-3-small")
    tokens: int = Field(..., description="Tổng token đã tiêu, tính từ khi bật tính năng")
    allowance: int = Field(..., description="Hạn mức token của model này, 0 nghĩa là không giới hạn")
    remaining: int = Field(..., description="Còn lại bao nhiêu token, 0 nếu không có hạn mức")
    used_percent: float = Field(..., description="Đã dùng bao nhiêu phần trăm hạn mức")
    exhausted: bool = Field(..., description="Đã hết hạn mức và bị bỏ qua khi chọn model")


class EmbeddingUsageResponse(BaseModel):
    """The whole state of embedding spending, in the order models are tried.

    `active_model` is the one a search would use right now — `None` means every
    configured model is spent (or none is configured), and `KnowledgeSearch`
    has fallen back to keyword-only ranking. That single field is what somebody
    checks after noticing that answers got worse.
    """

    model_config = ConfigDict(extra="forbid")

    active_model: str | None = Field(
        None, description="Model đang được dùng, None nghĩa là đã chuyển sang tìm bằng từ khoá"
    )
    models: list[EmbeddingModelUsageResponse] = Field(
        default_factory=list, description="Từng model, theo thứ tự được thử"
    )


class SecretValueRequest(BaseModel):
    """An admin setting or rotating a secret store value — RAG-19 / ADR-0045.

    One field, deliberately: this body exists only to carry a value from an
    admin's own hands into the store, and `extra="forbid"` keeps it that way —
    there is no second field for anything that would make this row easier to
    find later, because that is exactly the response path the store's own
    `get` is built to be the only one of.
    """

    model_config = ConfigDict(extra="forbid")

    value: str = Field(..., min_length=1, description="Giá trị bí mật, ghi thẳng vào secret store")


class SecretRefResponse(BaseModel):
    """The acknowledgement a secret-store write returns — the reference, never the value.

    Every `/admin/secrets` endpoint answers this same shape, `value` included
    nowhere on it: ADR-0045's acceptance is that no response path can return a
    secret, and the way that holds for these three endpoints is that there is
    no field here it could be written into.
    """

    key: str = Field(description="Cái mà `ToolCredential.secret_ref` sẽ trỏ tới")
