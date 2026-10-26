"""ORM tables backing the platform's own data — users, agents, versions, config.

Mirrors the version lifecycle from issue #2: `Agent` is a persistent identity,
`AgentVersion` is an **immutable** `AgentSpec` snapshot with a status, and one
pointer says which version is live — editing a spec never touches a running
version, it creates a new draft instead.

Since #50 that pointer is `Deployment.agent_version_id` rather than a column on
`Agent` — ADR-0029 §1, with the old column dropped rather than mirrored
(ADR-0036 §1). The pointer's *behaviour* is ADR-0013's, unchanged: one value,
and a deploy replaces it. What moved is where it lives, because the
publication grew properties of its own — whether it is serving, who may reach
it, which key opens the API door — and none of those are facts about the agent.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.agents.threads import ThreadNamespace
from src.agentspec.spec import ConnectionType
from src.db.base import Base
from src.models.model_catalog import SEEDED_MODEL_ALLOWLIST
from src.models.solution_spec import SuccessCriterionCategory
from src.workflow.templates import TemplateStatus
from src.workflow.tools import CredentialKind, ToolStatus, UsageScope


class UserRole(StrEnum):
    """Every role the platform knows about.

    `CREATOR`/`ADMIN` are the V2 platform's two roles (issue #4). UC1-01
    (#117, spec.md §3) adds three more for the UC-1 build target: `AGENT_ADMIN`
    is UC-1's admin-equivalent role (IT staff — creates/approves/deploys
    agents), `SALES_MANAGER` and `SALES_REP` are the two audience roles the
    accounts-receivable assistant serves. `require_admin` (`src.auth.dependencies`)
    treats `ADMIN` and `AGENT_ADMIN` as the same admin-equivalent gate so the V2
    admin surface keeps working for both generations of admin account.
    """

    CREATOR = "creator"
    ADMIN = "admin"
    AGENT_ADMIN = "agent_admin"
    SALES_MANAGER = "sales_manager"
    SALES_REP = "sales_rep"
    #: ADR-0051 — the person `docs/PM-PLAYBOOK.md` is written for. Reads the
    #: control room and pulls its levers (pause a deployment, decide a waiting
    #: run, thumbs, run the gate, promote a run to a case) but is **not** an
    #: admin: no accounts, no platform config, no dossiers. A role of its own
    #: rather than a second admin account because the two jobs are held by
    #: different people and the admin's powers are not the PM's to have.
    PM = "pm"


#: Every role `require_admin` (`src.auth.dependencies`) treats as admin-equivalent
#: — V2's `ADMIN` and UC-1's `AGENT_ADMIN`. Also what `register_user`
#: (`src.auth.registration`) checks before deciding a fresh signup needs to
#: bootstrap one: "is there already an admin-equivalent account" is one fact,
#: defined once, rather than two modules independently guessing the same set.
ADMIN_EQUIVALENT_ROLES = frozenset({UserRole.ADMIN, UserRole.AGENT_ADMIN})

#: Who may open the PM control room (`src.api.pm_routes`): the PM, and the
#: admin-equivalents — a platform admin answers for everything the PM reads,
#: and the kill switch on that screen is a power they already hold elsewhere.
#: `require_pm` (`src.auth.dependencies`) enforces it; `frontend/src/lib/roles.js`
#: mirrors it for the sidebar.
PM_CONTROL_ROOM_ROLES = frozenset({UserRole.PM}) | ADMIN_EQUIVALENT_ROLES


class UserStatus(StrEnum):
    """UC1-01 (#117), FR-03: the first account on a fresh install is `ACTIVE`
    immediately; every account after it starts `PENDING` until an
    `agent_admin` activates it (`POST /admin/users/{id}/activate`).
    """

    ACTIVE = "active"
    PENDING = "pending"


class VersionStatus(StrEnum):
    """`draft -> sandbox_passed -> pending_approval -> approved`, or `rejected`.

    "Deployed" is not a status here: it is a Deployment pointing at an `approved`
    version, per the version-lifecycle decision in issue #2.

    V2 adds four stages **before** all of those (ADR-0020 §4, ADR-0022 §5): a
    version begins life holding an Agent Solution Specification nobody has
    confirmed yet, confirming it opens the Workflow Generator's door (#42), the
    workflow the Generator drafts (#43) then waits for its own confirmation on
    the canvas, and confirming *that* (#44) makes the graph immutable. They are
    listed first because that is the order they happen in, and they belong on
    the same enum rather than beside it — a version has one status, and two
    status columns would be two answers to "what stage is this at".

    `workflow_confirmed` is the one stage of the four that is a promise about
    *immutability* rather than about progress. It is reached only through
    `src.workflow.drafts.confirm_workflow`, behind the graph validator
    (`src.workflow.validation.ensure_confirmable`), and nothing writes
    `AgentVersion.workflow` on a version that has reached it — an edit after
    this point forks a new version instead (ADR-0022 §5).
    """

    SPEC_PENDING_CONFIRMATION = "spec_pending_confirmation"
    SPEC_CONFIRMED = "spec_confirmed"
    WORKFLOW_PENDING_CONFIRMATION = "workflow_pending_confirmation"
    WORKFLOW_CONFIRMED = "workflow_confirmed"
    DRAFT = "draft"
    SANDBOX_PASSED = "sandbox_passed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    password_hash: Mapped[str]
    role: Mapped[UserRole]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # UC1-01 (#117), FR-01/FR-03/FR-04. Nullable at the DB level because every
    # V2 account predates these fields and has none of them; the *new*
    # registration flow (`src.auth.registration`) is what makes job_title and
    # department required in practice. `status` defaults `ACTIVE` so an
    # existing/fixture-created row (never touching `register_user`) is usable
    # without having to know the pending-activation flow exists.
    # `server_default` names the *label* the native Postgres enum stores —
    # the member's name ("ACTIVE"), not its value ("active"); see the
    # `userrole` precedent this column follows (`1135e617defc`'s `row['role'].name`).
    status: Mapped[UserStatus] = mapped_column(default=UserStatus.ACTIVE, server_default=UserStatus.ACTIVE.name)
    #: The identity RLS keys off of (schema.sql §2) and the JWT's `employee_id`
    #: claim (FR-04) — distinct from the numeric `id` because it is meant to be
    #: stable, human-referenceable, and matchable against seeded demo data
    #: (`crm_sales_assignment.sales_rep_id`), none of which an autoincrement PK is.
    employee_id: Mapped[str | None] = mapped_column(unique=True, default=None)
    job_title: Mapped[str | None] = mapped_column(default=None)
    department: Mapped[str | None] = mapped_column(default=None)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    versions: Mapped[list["AgentVersion"]] = relationship(back_populates="agent", foreign_keys="AgentVersion.agent_id")
    #: At most one, and it is where the deployed pointer lives since #50. `None`
    #: means nobody has published this agent yet — which is the state every agent
    #: starts in and stays in while it is being built.
    #:
    #: `selectin` because the three properties below all read it, and a creator's
    #: agent list renders every one of them per row: the pointer used to be a
    #: column on this table, and a lazy load would have turned reading it into a
    #: query per agent. One extra statement per query, whatever the row count.
    deployment: Mapped["Deployment | None"] = relationship(
        back_populates="agent", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    #: Which shelves this agent may search — see #90's acceptance criterion
    #: (assign one Knowledge Base to two agents) and `agent_kb_links`. Plain
    #: many-to-many: wiring a `KnowledgeSearch` node's `source_ids` to actually
    #: read from these is RAG-05 (#91), not this relationship's job.
    knowledge_bases: Mapped[list["KnowledgeBase"]] = relationship(secondary="agent_kb_links")

    @property
    def deployed_version_id(self) -> int | None:
        """Which version is published, or `None` if none is — read through the Deployment.

        Kept as a property rather than deleted along with the column (#50): every
        reader of a *pointer* is still asking the right question, and the answer
        moved rather than changed. `status: disabled` deliberately does not show
        up here — disabling a Deployment does not un-choose its version (ADR-0029
        §2), so "which version is published" and "is anyone being served" are two
        questions with two answers.
        """
        return self.deployment.agent_version_id if self.deployment is not None else None

    @property
    def deployed_version(self) -> "AgentVersion | None":
        """The published version itself, or `None` — the row `deployed_version_id` names."""
        return self.deployment.agent_version if self.deployment is not None else None

    @property
    def is_deployed(self) -> bool:
        """Whether this agent has been published at all.

        **Not** whether a stranger can reach it. That is a question about the
        Deployment's `status` and `access_mode` (ADR-0029 §2, §3), and answering
        it here would give one boolean two meanings — the owner's "have I
        published this" and a caller's "may I talk to it" part company the moment
        a Deployment is disabled or kept private.
        """
        return self.deployment is not None


class AgentVersion(Base):
    """One immutable snapshot of an agent, and the stage it has reached.

    V2 widened it to carry **three** documents instead of one (ADR-0020 §5,
    ADR-0022 §5), each with its own confirmation gate: `solution_spec` is the
    business problem the interview produced (#42), `workflow` is the graph the
    Workflow Generator drafted from it (#43), `spec` is V1's executable
    pipeline. A row holds whichever ones its stage has reached, so all three are
    nullable and none is a promise the others are there —
    `src.solution.specs.solution_spec_of`, `src.workflow.generator.workflow_of`
    and `src.api.agent_routes.validated_spec` are where asking for one a version
    does not have is refused by name.
    """

    __tablename__ = "agent_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    # Nullable since V2: a version at `spec_pending_confirmation` has a business
    # problem and no pipeline yet, and writing an empty `AgentSpec` to fill the
    # column would be a spec claiming an agent that cannot run.
    spec: Mapped[dict | None] = mapped_column(JSON, default=None, doc="The AgentSpec payload, immutable once written")
    solution_spec: Mapped[dict | None] = mapped_column(
        JSON,
        default=None,
        doc="AgentSolutionSpecification đã xác nhận hoặc đang chờ xác nhận — bất biến từ lúc spec_confirmed",
    )
    workflow: Mapped[dict | None] = mapped_column(
        JSON,
        default=None,
        doc="WorkflowJSON do Workflow Generator dựng — sửa thoải mái tới khi workflow_confirmed",
    )
    status: Mapped[VersionStatus] = mapped_column(default=VersionStatus.DRAFT)
    rejection_reason: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    agent: Mapped["Agent"] = relationship(back_populates="versions", foreign_keys=[agent_id])


#: Requests per caller per minute a fresh Deployment starts with. High enough
#: that publishing does not quietly throttle the owner's own testing, low enough
#: to be a ceiling rather than a decoration. Shared with the migration so the two
#: never disagree about what a row written before #51 was promised.
DEFAULT_RATE_LIMIT_PER_USER = 60

#: What an owner may set the ceiling to — see #51 / ADR-0037 §6. One and not zero
#: because "nobody may call this" is `status: disabled`, which says the same thing
#: without leaving a Deployment that looks like it is serving; the upper bound is
#: far above anything a person types into a chat window, so it constrains only the
#: runaway integration it exists for rather than switching cost control off.
MIN_RATE_LIMIT_PER_USER = 1
MAX_RATE_LIMIT_PER_USER = 1000


class DeploymentStatus(StrEnum):
    """Whether a publication is currently serving — see #50 / ADR-0029 §2.

    `disabled` is the unpublish path ADR-0013 left open, and it is deliberately
    *independent* of `agent_version_id`: pausing a Deployment does not clear the
    pointer, so turning it back on is one write rather than "now pick a version
    again".
    """

    ACTIVE = "active"
    DISABLED = "disabled"


class DeploymentAccessMode(StrEnum):
    """Who may reach a Deployment — see #50 / ADR-0029 §3.

    Two values and not three. `private` is the owner alone; `selected_users` is
    the owner plus whoever is named in `DeploymentAllowedUser`. The requirement
    doc's third value (`organization`) presumes a tenant boundary V2 has not
    decided on, and a public link is outside the MVP by the doc's own admission.

    `private` is the default a fresh publication gets, which is stricter than V1
    (where deploying meant "anyone signed in may chat") and is the one behavioural
    break #50 takes on purpose — see ADR-0036 §2. Widening is a decision the owner
    makes deliberately, not one they discover afterwards.
    """

    PRIVATE = "private"
    SELECTED_USERS = "selected_users"


class Deployment(Base):
    """One agent's publication: which version serves, whether it is serving, and to whom — #50 / ADR-0029.

    At most one per agent (`agent_id` is unique). Publishing a newer approved
    version moves `agent_version_id` on this same row, exactly as ADR-0013's
    `PUT /agents/{id}/deployed-version` moved the old column — so this is a
    widening of that pointer, not a second mechanism beside it. Nothing here is
    append-only: the publish history a reviewer would want is already the run
    trace (ADR-0006, ADR-0009), which records the version that served every run.

    The three columns V1 had nowhere to put are what justify the table.
    `status` says whether the door is open, without touching which version is
    behind it. `access_mode` says who may come through, for **both** channels at
    once — a Deployment does not get to be more open over API than over Web Chat.
    `api_key_hash` is the API channel's credential: one key per Deployment,
    regenerate-able, stored as a hash like every other secret in this system, so
    the plaintext exists only in the response that minted it.

    `rate_limit_per_user` is carried here and counted in
    `src.deployments.rate_limit` (#51 / ADR-0037): a sliding minute per
    `(deployment, caller)`, charged at the door before a run starts. ADR-0029 §6
    fixes the *scope* — per caller, not an aggregate ceiling, so one heavy caller
    cannot starve the others — and the counting stayed out of the schema so the
    two are never out of step about what is being limited.
    """

    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Unique: one Deployment per Agent (ADR-0029 §1). Cascade because a
    #: publication of an agent that no longer exists serves nothing.
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), unique=True)
    #: The pointer itself. Not nullable, and that is the point: an unpublished
    #: agent has no row here at all, so there is no "deployed to nothing" state
    #: for a reader to have to think about.
    agent_version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id"))
    status: Mapped[DeploymentStatus] = mapped_column(default=DeploymentStatus.ACTIVE)
    access_mode: Mapped[DeploymentAccessMode] = mapped_column(default=DeploymentAccessMode.PRIVATE)
    api_key_hash: Mapped[str] = mapped_column(doc="Băm của API key kênh API — bản rõ chỉ tồn tại ở lần trả về tạo key")
    rate_limit_per_user: Mapped[int] = mapped_column(
        default=DEFAULT_RATE_LIMIT_PER_USER,
        doc="Số request mỗi người gọi mỗi phút — đếm ở src.deployments.rate_limit (#51)",
    )
    #: No cascade, for `ApprovalDecision.admin_id`'s reason: who published this
    #: is audit information, and a row naming a deleted account still says more
    #: than a row that quietly forgot.
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    #: Moves on any change to `agent_version_id`, `status` or `access_mode` —
    #: ADR-0029 §7's whole answer to "when did this last change".
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    agent: Mapped["Agent"] = relationship(back_populates="deployment")
    agent_version: Mapped["AgentVersion"] = relationship()
    allowed_users: Mapped[list["DeploymentAllowedUser"]] = relationship(
        back_populates="deployment", cascade="all, delete-orphan"
    )

    @property
    def is_serving(self) -> bool:
        """Whether this Deployment is answering anyone but its owner right now."""
        return self.status is DeploymentStatus.ACTIVE


class DeploymentAllowedUser(Base):
    """One person allowed onto one Deployment — see #50 / ADR-0029 §4.

    Consulted only when `access_mode == "selected_users"`; rows are kept across a
    switch back to `private` so that flipping the mode is not also a way to
    silently lose the list.

    A join table rather than a JSON column on `Deployment`, because the question
    it exists to answer — "may this user_id reach this deployment" — is a lookup.
    No role column: V2's only roles are the two platform-wide ones (ADR-0002),
    neither of which is a meaningful unit to attach to one deployment's allowlist.
    """

    __tablename__ = "deployment_allowed_users"
    #: The allowlist is a set, and letting the same person onto it twice would
    #: make "remove them" a question with two answers.
    __table_args__ = (UniqueConstraint("deployment_id", "user_id", name="uq_deployment_allowed_users_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(ForeignKey("deployments.id", ondelete="CASCADE"), index=True)
    #: Cascade: a permission granted to an account that no longer exists is not
    #: audit information, it is a dangling grant.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    deployment: Mapped["Deployment"] = relationship(back_populates="allowed_users")
    user: Mapped["User"] = relationship()


class SandboxRunStatus(StrEnum):
    """Whether a trial run produced usable evidence.

    A tool that failed does not make a run `FAILED` — the model reacts to the
    failure and the run continues (ADR-0003 §4), and that failure is exactly
    what a creator needs to see in the transcript. Only a run that could not
    finish at all is `FAILED`, and only a `SUCCEEDED` run moves the version to
    `sandbox_passed`.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SandboxRun(Base):
    """One trial run of a version, and the transcript it left behind — see #9 / ADR-0007.

    The transcript is the **mandatory evidence** an approval request is built on
    (issue #2), so it is a row of its own rather than a field on the version: a
    version is trialled many times while it is being refined, and the run that
    convinced its creator is not necessarily the last one.

    Kept deliberately narrow. Budget enforcement (#10) added no columns: a run
    stopped by its ceiling is a `FAILED` run whose `error` says which ceiling,
    which the two existing columns already express (ADR-0008 §7). Nor did the
    cost figures (#11), which this docstring once expected: what a run spent
    lives in `RunCost` below, because production runs spend money too and an
    agent's bill is not only its rehearsals (ADR-0009 §4).
    """

    __tablename__ = "sandbox_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # `ondelete="CASCADE"`: a transcript is evidence *about* a version, and
    # says nothing once that version no longer exists.
    version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str] = mapped_column(doc="Thread `trial` lần chạy này diễn ra trên")
    input_message: Mapped[str] = mapped_column(doc="Dữ liệu đầu vào người tạo tự chọn")
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(JSON, doc="Ordered SandboxTranscriptEntry payloads")
    status: Mapped[SandboxRunStatus]
    error: Mapped[str | None] = mapped_column(default=None, doc="Vì sao lần chạy không hoàn tất; null nếu thành công")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    version: Mapped["AgentVersion"] = relationship()


class RunCost(Base):
    """What one run actually spent — see #11 / ADR-0009.

    One row per run, **trial and production alike**. That is the whole reason it
    is its own table rather than columns on `sandbox_runs`: the question an admin
    asks is "which agent is costing us money", and an agent's bill is mostly the
    runs nobody is watching. A figures-on-`sandbox_runs` design can only answer
    for rehearsals.

    Only the *measured* figure is stored. The estimate and the worst case are
    functions of the spec, which is already immutable (#7), so storing them would
    be caching a pure computation and inviting it to go stale against a price
    change. Measurements are the opposite: they are facts about a moment, and
    re-deriving one later from today's prices would rewrite history.
    """

    __tablename__ = "run_costs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Indexed and cascading for the same reason: this table is read by grouping
    # on the agent, and a deleted agent's bill is not a bill anyone can act on.
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str] = mapped_column(doc="Thread lần chạy này diễn ra trên")
    # Whether tools were simulated — *not* which thread namespace the run used.
    # The two come apart: `POST /chat` accepts `namespace=trial`, and such a run
    # calls real tools. Filing it as a rehearsal would let a figure measured on
    # the real world be shown as the sandbox measurement (ADR-0009 §4).
    sandbox: Mapped[bool] = mapped_column(doc="True = chạy thử, tool có tác dụng phụ chỉ chạy bản mô phỏng")
    # The other half of that distinction, stored since #55 rather than inferred.
    # `sandbox` says whether the *tools* were real; this says whether the
    # *conversation* was. The operations dashboard asks the second question — a
    # creator rehearsing on their own deployed version is not somebody using it —
    # and before this column the ledger had thrown the answer away.
    namespace: Mapped[ThreadNamespace] = mapped_column(doc="Thread lần chạy này thuộc về thế giới nào")
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost_usd: Mapped[float] = mapped_column(doc="Quy ra tiền theo bảng giá tại thời điểm chạy")
    latency_seconds: Mapped[float] = mapped_column(doc="Lần chạy kéo dài bao lâu")
    usage_by_model: Mapped[dict[str, Any]] = mapped_column(
        JSON, doc="Token và tiền tách theo từng model — chỗ thấy được bước rẻ đúng là rẻ"
    )
    # Nullable because a trace id is only useful if the trace service was
    # actually recording; the run itself never depends on it (ADR-0009 §6).
    trace_id: Mapped[str | None] = mapped_column(default=None, doc="Trace tương ứng trên Langfuse")
    # How the run ended, in the runtime's own words, or NULL if it ended well.
    # Deliberately *not* where a budget stop is recorded: a run cut off by its
    # own ceiling produced real output and said why it stopped (ADR-0008 §3), and
    # filing it here would let a dashboard report a working agent as a broken one.
    error: Mapped[str | None] = mapped_column(default=None, doc="Câu lỗi làm lần chạy này chết giữa chừng")
    # PM playbook §1: the two group-bys every PM question starts from, kept as
    # plain strings rather than a Postgres enum because the playbook says the
    # `outcome` taxonomy *will* grow (spec_error / system_error / …), and each
    # new word should be a one-line change, not an `ALTER TYPE`. `RunTrigger`
    # and `RunOutcome` below are the vocabulary; `src.agents.spend.run_cost_row`
    # is the one writer.
    trigger: Mapped[str] = mapped_column(
        default="manual",
        server_default="manual",
        doc="chat_test | manual — người tạo tự thử hay người khác thật sự gọi",
    )
    outcome: Mapped[str] = mapped_column(
        default="success", server_default="success", doc="Một từ nói lần chạy kết thúc thế nào — xem RunOutcome"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PlatformModelCall(Base):
    """One model call **the platform made on its own behalf** — the other half of the bill.

    `run_costs` above answers "what did this agent's runs cost". Nothing answered
    "what did *building* the agents cost", and the difference was not small: an
    OpenAI project hit its enforced spend limit while the platform's own cost
    board reported **$1.57 total**, because the board could only see the half
    that goes through `src.agents.runtime`. Interviewing a creator, drafting a
    spec, composing a graph, refining it from a sentence, writing eval cases and
    judging them are thirteen call sites that left **no trace of having spent
    anything** — on the platform's dearest model, with the largest prompts in the
    codebase, retried up to three times per document.

    One row per round trip, not per document. A composition that needed three
    attempts is three rows sharing a `purpose`, which is the only way the retry
    loop's cost is visible as *its own* cost rather than folded into the price of
    a graph.

    **No foreign keys, deliberately** — the lesson `agent_runs` already learned
    (ADR-0053). A bill survives the thing it was spent on: deleting an agent must
    not erase the record of what building it cost, or the ledger answers "where
    did the money go" with silence for exactly the agents somebody deleted
    because they were expensive.

    Attribution is not here yet. `purpose` answers "which job spends the money",
    which is the question that was unanswerable; "which creator's agent" needs a
    request-scoped identity threaded through thirteen call sites and is a
    separate change.
    """

    __tablename__ = "platform_model_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Indexed because every question of this table starts by grouping on it.
    # A plain string and not a Postgres enum, for `RunCost.trigger`'s reason:
    # a new kind of platform call should be a one-line change, not an ALTER TYPE.
    purpose: Mapped[str] = mapped_column(index=True, doc="Việc nền tảng đang làm — xem PlatformCallPurpose")
    model: Mapped[str] = mapped_column(doc="Model đã trả lời, id nguyên văn như lúc gọi")
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    # Kept apart from `input_tokens` rather than subtracted out of it, because
    # the provider bills a cached prefix at a quarter and the whole point of
    # storing it is to be able to ask whether caching is working at all.
    cached_input_tokens: Mapped[int] = mapped_column(
        default=0, server_default="0", doc="Phần token vào được tính giá cache"
    )
    cost_usd: Mapped[float] = mapped_column(doc="Quy ra tiền theo bảng giá tại thời điểm gọi")
    latency_seconds: Mapped[float]
    # 1-based, and it is the retry counter: `write_document` re-asks with the
    # validator's complaint appended, so attempt 3 costs the most and produces
    # the same document attempt 1 was asked for.
    attempt: Mapped[int] = mapped_column(default=1, server_default="1", doc="Lần thử thứ mấy của cùng một tài liệu")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class PlatformCallPurpose(StrEnum):
    """Which job the platform was doing when it called a model.

    The vocabulary `PlatformModelCall.purpose` is written in. Named per *job*
    rather than per module because the question it answers is a cost question —
    "composing graphs is 60% of the build bill" is actionable, "`composition.py`
    is 60%" is the same fact addressed to nobody.
    """

    INTERVIEW = "interview"
    """Reading a creator's description into the seven fields (V1 and V2)."""

    INTERVIEW_SUGGESTIONS = "interview_suggestions"
    """Proposing click-to-pick answers for the V2 interview's open question.

    Its own purpose rather than `INTERVIEW` because it is a different bill: one
    extra call per question the creator looks at, on their action, not one per
    answer they send. Folding it into `INTERVIEW` would hide how much the
    recommend affordance costs behind the reading it sits next to."""

    SOLUTION_SPEC = "solution_spec"
    """Drafting or redrafting the Agent Solution Specification."""

    COMPOSE_GRAPH = "compose_graph"
    """Drawing a workflow freehand — the largest prompt this platform sends."""

    REFINE_GRAPH = "refine_graph"
    """Editing a graph from one sentence, one-shot."""

    SELECT_TEMPLATE = "select_template"
    """Choosing which catalog shape a spec is closest to."""

    FILL_TEMPLATE = "fill_template"
    """Writing this creator's wording into a shape already chosen."""

    EVAL_CASES = "eval_cases"
    """Writing test cases from success criteria."""

    JUDGE = "judge"
    """Grading one answer against one criterion — the highest-volume purpose."""

    WORKBENCH_TURN = "workbench_turn"
    """One round trip of the meta agent's tool loop (ADR-0050)."""

    GRID_LANGUAGE = "grid_language"
    """Retired with the scenario grid (ADR-0065) — no code writes this any more.

    Kept as a member because rows already carry it: five calls on the dev
    database named the grid's axes in the creator's domain words (ADR-0055 §2).
    Deleting the value would make those bills unreadable, which is a worse
    outcome than one enum member nothing produces.
    """

    GRID_JUDGE = "grid_judge"
    """Reading one answer for meaning where a rule could only read spelling (ADR-0063).

    Named for the grid it was introduced for and **still written**: ADR-0065
    retired the grid, not `judge_asks_back`, which the ordinary suite reaches
    through `workflow_runner._result_for`. The name stays because ten rows
    already carry it and renaming an enum value is a data migration, not a
    rename. Its own purpose rather than `JUDGE` because the two bills answer
    different questions and a spike in either should be attributable without
    reading timestamps.
    """

    # Deliberately absent: the `if_else`/`while` router. It is a platform call by
    # any reading — the platform asking a model whether a condition holds — but
    # it happens *inside a creator's run*, is charged against that run's budget,
    # and already lands in `run_costs`. A purpose for it here would double-count
    # the only cost figure a creator is ever shown.
    #
    # Also absent: anything nobody writes. A purpose with no writer is a bar the
    # cost board can never draw, and a reader who takes its absence for "we
    # spend nothing on that" is reading a category that was never wired up.



class RunTrigger(StrEnum):
    """What started a run — PM playbook §1's `trigger` attribute.

    Two values, because this platform has two ways a run begins: `CHAT_TEST` is
    the creator trying their own agent, `MANUAL` is somebody else asking it for
    something. The playbook's third value (`scheduled`) is deliberately absent:
    **nothing here runs an agent on a timer** — no schedule table, no worker, no
    trigger node — and a word for something that cannot happen is a column that
    reads as a bug the first time a PM filters on it and gets nothing
    (ADR-0051).

    Not a rename of `RunCost.sandbox`. That column answers "were the tools
    simulated", and the two come apart: a creator's `trial` chat on a deployed
    version calls **real** tools and is still the creator testing. What makes a
    run a rehearsal is the world its thread lives in, so this is derived from
    the namespace too (`src.agents.spend.run_cost_row`) — the same
    two-condition cut `src.deployments.operations.served` already makes.
    """

    CHAT_TEST = "chat_test"
    MANUAL = "manual"


class RunOutcome(StrEnum):
    """How a run ended, as one word a dashboard can group by — PM playbook §1.

    The runtime can currently tell three of these apart (`success`, `error`,
    `budget_stopped` — the same three `src.services.tracing._outcome_of`
    exports to Langfuse). The finer split the playbook calls "the one metric
    that matters most" — `spec_error` (the creator can fix it themselves) vs
    `system_error` (call the platform team) — rides on the error taxonomy
    (playbook step 7) and is listed here so that work has a column to land in,
    not because anything writes it yet.
    """

    SUCCESS = "success"
    ERROR = "error"
    SPEC_ERROR = "spec_error"
    SYSTEM_ERROR = "system_error"
    GUARDRAIL_BLOCK = "guardrail_block"
    NEEDS_APPROVAL = "needs_approval"
    BUDGET_STOPPED = "budget_stopped"


class RunPipeline(StrEnum):
    """Which runtime produced an `AgentRun` — the two vocabularies ADR-0049 names."""

    V2 = "v2"
    UC1 = "uc1"


class AgentRun(Base):
    """One leg of one run, in every pipeline, with the trace it left — ADR-0053.

    The PM's ledger, as opposed to `run_costs`, which is V2's *bill* (ADR-0009)
    and stays exactly what it was. Two reasons this is its own table rather
    than more columns there. First, neutrality: `run_costs.agent_id` is a
    foreign key into V2's `agents`, and a UC-1 run (`AgentSpec.metadata.id`, a
    string, no `agents` row) cannot be written into it — which is why, until
    this table, the dashboard was blind to UC-1 by construction. `agent_key` /
    `version_key` are strings both pipelines can fill, and `agent_name` is
    copied in at write time so no widget ever joins across the two
    vocabularies. Second, evidence: the slide deck's rule is that the
    transcript is a story and the trace is the proof, and a production run
    used to keep neither — `transcript` here is the same flat
    `SandboxTranscriptEntry` shape the sandbox stores, built by the same mapper,
    so what a PM reads afterwards is what the chat window showed.

    The derived columns (`node_count` … `failure_node_type`) are computed once,
    at write time, from the transcript. They are redundant on purpose: every
    PM question is a `GROUP BY`, and a JSON column cannot be grouped on.

    `agent_id` / `version_id` / `run_cost_id` are V2-only and nullable: the
    levers that act on a V2 run (decide an approval, promote to an `eval_cases`
    row) still need the original rows, and a UC-1 run simply has none.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    pipeline: Mapped[str] = mapped_column(doc="v2 | uc1 — xem RunPipeline")
    #: Shared by every leg of one run (V2: `RunIdentity.trace_id`; UC-1: the
    #: turn's `request_id`). Indexed because thumbs from the chat window and
    #: the resumed half of a paused run both find the run by it.
    trace_id: Mapped[str] = mapped_column(index=True)
    agent_key: Mapped[str] = mapped_column(index=True, doc="V2: str(agents.id); UC-1: AgentSpec.metadata.id")
    version_key: Mapped[str] = mapped_column(index=True, doc="V2: str(agent_versions.id); UC-1: str(version)")
    agent_name: Mapped[str] = mapped_column(doc="Tên lúc ghi — không join chéo hai từ vựng")
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), default=None, index=True, doc="Chỉ V2"
    )
    version_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="CASCADE"), default=None, index=True, doc="Chỉ V2"
    )
    run_cost_id: Mapped[int | None] = mapped_column(
        ForeignKey("run_costs.id", ondelete="SET NULL"), default=None, index=True, doc="Hoá đơn V2 của leg này"
    )
    conversation_id: Mapped[str] = mapped_column(doc="Thread lần chạy này diễn ra trên")
    caller: Mapped[str | None] = mapped_column(default=None, doc="Ai gọi — user id hay tên khoá API")
    trigger: Mapped[str] = mapped_column(default="manual", doc="chat_test | manual — xem RunTrigger")
    outcome: Mapped[str] = mapped_column(default="success", doc="Xem RunOutcome; đã qua taxonomy lúc ghi")
    error: Mapped[str | None] = mapped_column(default=None, doc="Câu lỗi nguyên văn của runtime")
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    latency_seconds: Mapped[float] = mapped_column(default=0.0)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    usage_by_model: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_message: Mapped[str | None] = mapped_column(default=None, doc="Người dùng hỏi gì")
    answer: Mapped[str | None] = mapped_column(default=None, doc="Câu trả lời cuối cùng của agent")
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, doc="SandboxTranscriptEntry[] — bằng chứng; rỗng với run ghi trước ADR-0053"
    )
    node_count: Mapped[int] = mapped_column(default=0)
    tool_calls: Mapped[int] = mapped_column(default=0)
    tool_failures: Mapped[int] = mapped_column(default=0, doc="Số tool_result ok=False — lỗi chìm dưới mặt nước")
    actions: Mapped[list[str]] = mapped_column(JSON, default=list, doc="Tên tool đã gọi, theo thứ tự, không trùng")
    first_failure: Mapped[str | None] = mapped_column(
        default=None, doc="`node_id:tool` của lần tool hỏng đầu tiên, hay node_id nơi run chết"
    )
    failure_node_type: Mapped[str | None] = mapped_column(
        default=None, index=True, doc="Loại node nơi lỗi bắt đầu — slice 'loại bước nào hay hỏng'"
    )
    simulated: Mapped[bool] = mapped_column(default=False, index=True, doc="Hàng do scripts/simulate_pm_runs.py sinh")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class RunLabel(Base):
    """One expert verdict on one run — ADR-0053 §3, layer three.

    Binary (`passed`), with the team's own trace codes and a note, the way
    the deck says an expert should label: one decision per screen, domain
    words, evidence first. Its own table and not a `quality_scores` row
    because a code is categorical and a score is a number; grouping failures
    by code is the whole point of having codes.

    Append-only. A second label on the same run is a second row; the newest
    is the one the board reads, and the disagreement is itself data (slide 57:
    the rows where two people disagree are the only ones worth digging into).
    """

    __tablename__ = "run_labels"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    passed: Mapped[bool]
    codes: Mapped[list[str]] = mapped_column(JSON, default=list, doc="Trace codes — xem src.pm.taxonomy.TRACE_CODES")
    note: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ScoreSource(StrEnum):
    """Who produced a `QualityScore` — the three evaluator tiers of PM playbook §4."""

    CODE = "code"
    JUDGE = "judge"
    HUMAN = "human"


class QualityScore(Base):
    """One named judgement about one run or one version — PM playbook §3/§4.

    The same shape as a Langfuse score (`name`, numeric `value`, optional
    `comment`, a `source`), kept **in the platform's own database** so the PM
    dashboard is a query over tables the platform already owns rather than a
    feature of whichever trace backend is configured — and so a thumbs-down a
    user leaves survives a deployment with no Langfuse key at all.

    Three writers, one table, by design: `record_spec_lint` files the D1–D3
    code checks (`spec_lint_pass`, `graph_problems`); the sampled judge files
    `faithfulness` / `citation_precision`; and a person files `thumbs_up` with
    a note. The playbook's step 5 (judge must be calibrated against human
    labels before it is trusted) is a `JOIN` on this table between the last two.

    `run_cost_id` is nullable because a spec-lint score has no run — it is
    about a save. Since ADR-0053 a run-level score points at `agent_runs`
    through `run_id` instead, and `agent_id` / `version_id` are nullable too: a
    score on a UC-1 run has no V2 agent or version to name, and the version it
    *is* about is read off the run row. `run_cost_id` stays for rows written
    before the shared ledger existed; readers prefer `run_id`.
    """

    __tablename__ = "quality_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), default=None, index=True, doc="Agent V2, nếu điểm thuộc về một"
    )
    version_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="CASCADE"), default=None, index=True
    )
    run_cost_id: Mapped[int | None] = mapped_column(
        ForeignKey("run_costs.id", ondelete="CASCADE"),
        default=None,
        index=True,
        doc="Lần chạy được chấm (khoá cũ, trước ADR-0053); null nếu chấm bản nháp",
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        default=None,
        index=True,
        doc="Lần chạy được chấm trong sổ dùng chung (ADR-0053); null nếu chấm bản nháp",
    )
    name: Mapped[str] = mapped_column(
        index=True, doc="thumbs_up | faithfulness | citation_precision | spec_lint_pass | ..."
    )
    value: Mapped[float] = mapped_column(doc="Số — bool ghi 0/1, thang 1–5 ghi nguyên số")
    comment: Mapped[str | None] = mapped_column(default=None, doc="Ghi chú của người chấm, hoặc `reasoning` của judge")
    source: Mapped[ScoreSource]
    given_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), default=None, doc="Người chấm, nếu source = human"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MetaAgentGateRun(Base):
    """One execution of the meta agent's regression gate, kept — PM playbook step 3.

    `src.evals.meta_agent_gate` answers pass/fail and prints it; until this
    table nothing remembered the answer, which is why the playbook could write
    "hạ tầng xong, chưa đo lần nào" two days after the gate shipped. A row per
    run — thresholds *as they were*, per-family counts, every verdict with its
    failure classification (n8n's `builder_issue` / `case_issue` split) — is
    what lets "is the new prompt better than the old one" be a comparison of
    two rows rather than a recollection.

    `mode` is `simulated` or `real`. A simulated run drives the same grader
    over a scripted stand-in for the model, so the pipeline, the thresholds
    and the dashboard can be exercised before anyone spends tokens; it is
    labelled as such everywhere it is shown and never counts as a measurement.
    """

    __tablename__ = "meta_agent_gate_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(doc="simulated | real")
    passed: Mapped[bool]
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSON, doc="THRESHOLDS lúc chạy — {family: [wanted, out_of]}")
    per_family: Mapped[dict[str, Any]] = mapped_column(JSON, doc="{family: {passed, total, wanted}}")
    verdicts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, doc="[{case_id, family, passed, failures, classification}] — từng ca, để biết trượt vì builder hay vì đề"
    )
    triggered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EvalCaseOrigin(StrEnum):
    """Who or what put a test case into the suite — see #13 / ADR-0011, widened by ADR-0054 §1.

    `GENERATED`/`MANUAL` are #13's original two. ADR-0054 §1 adds three more
    without touching either meaning: `UPLOADED` is a row that came in through
    the importer (EVAL-2) rather than the generator or the form; `PROMOTED` is
    a case `src.pm.controls.promote_run_to_case` (ADR-0053) built from a real
    failure; `GRID` was a scenario-map cell (EVAL-4) — a case whose `cell_key`/
    `zone` are set. All five are read the same way everywhere a case is
    listed: a chip naming which of them a row is.

    `GRID` is **retired but not removed** (ADR-0065). Nothing mints one any
    more, and the nine rows that already carry it on the dev database are
    ordinary cases now: listed, run and deletable like any other. Dropping the
    value would break every one of those rows on read, which is the one thing
    retiring a feature must not do.
    """

    GENERATED = "generated"
    MANUAL = "manual"
    UPLOADED = "uploaded"
    PROMOTED = "promoted"
    GRID = "grid"


class EvalSuite(Base):
    """One agent's test set — see ADR-0054 §1.

    `eval_cases.version_id` used to be the case's scope, so a new version from
    `create_draft` started with an empty test set and the Baseline·Candidate·
    Delta report (ADR-0053) could only match cases across versions by
    `(input_message, criterion)` string equality — two independently generated
    sets almost never collide. A suite fixes both: the test set belongs to the
    **agent** and outlives every version; only a *result* is stamped to the
    version that produced it (`eval_runs`).

    `UNIQUE(agent_id)` is the MVP shape — one suite per agent, created lazily
    on first case (`src.evals.cases.get_or_create_suite`). ADR-0054 §7 notes
    the column to drop when a second suite (smoke vs full) is ever needed.

    `uc1_agent_id` (ADR-0054 §6, EVAL-7) is a second, disjoint identity: a
    UC-1 agent's id (`AgentSpec.metadata.id`, e.g. `agt_ar_assistant`) is a
    string with no row in `agents` at all (ADR-0046), so it cannot go in
    `agent_id`. Exactly one of `agent_id`/`uc1_agent_id` is set per row
    (`ck_eval_suites_exactly_one_agent`) — see
    `src.evals.uc1_suite.get_or_create_uc1_suite`.
    """

    __tablename__ = "eval_suites"
    __table_args__ = (
        CheckConstraint(
            "(agent_id IS NOT NULL) <> (uc1_agent_id IS NOT NULL)", name="ck_eval_suites_exactly_one_agent"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), unique=True)
    uc1_agent_id: Mapped[str | None] = mapped_column(unique=True, doc="UC-1's own agent id — ADR-0054 §6, EVAL-7")
    name: Mapped[str]
    #: The retired grid's vocabulary overlay (ADR-0055 §2, retired by ADR-0065).
    #: Nothing reads or writes it any more; three suites on the dev database
    #: still hold one. The column stays for the reason `EvalCaseOrigin.GRID`
    #: does — dropping it is a destructive migration bought for nothing.
    axis_language: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class EvalCase(Base):
    """One test case in an agent's suite — see #13 / ADR-0011, moved onto the suite by ADR-0054 §1.

    `criterion` is stored as a copy of the creator's own sentence rather than as
    an index into a spec. It is what a result is explained in terms of, and an
    index would make that explanation depend on a spec being re-read and
    re-parsed at display time.

    Two of the columns below are #48 / ADR-0028's, and both are nullable because
    a V1 case has neither: `AgentSpec.slots.success_criteria` is a list of bare
    sentences, so a case generated from one is graded the only way it can be —
    by the judge, against `expected`. A V2 case generated from an
    `AgentSolutionSpecification` carries the criterion's own `category`, and for
    the three rule-based categories an `expected_check` that says what to read off
    the transcript instead of what to ask a model.

    The rest are ADR-0054 §1's. `introduced_in_version_id` is what `version_id`
    was renamed to: still says when a case was written, no longer what it is
    scoped to — nullable and `SET NULL` on delete, because a suite case outlives
    the version that introduced it. `tags` is the creator's own, for filtering.
    `extra` is where an uploaded row's foreign columns land (UC-1's `principal`,
    `retrieval_context`, …) — `None` for every case that did not come through
    the importer. `cell_key`/`zone` were set only on `origin=grid` cases
    (EVAL-4) and are written by nothing since ADR-0065; every other origin
    leaves both `None`.
    """

    __tablename__ = "eval_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: The suite this case belongs to — the scope key since ADR-0054 §1.
    #: Cascades like `version_id` used to: a suite gone (its agent deleted)
    #: takes every case with it.
    suite_id: Mapped[int] = mapped_column(ForeignKey("eval_suites.id", ondelete="CASCADE"), index=True)
    introduced_in_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="SET NULL"),
        default=None,
        index=True,
        doc="Version lúc case này sinh ra — nguồn gốc, không còn là phạm vi (ADR-0054 §1)",
    )
    criterion: Mapped[str] = mapped_column(doc="Tiêu chí thành công case này kiểm — chép từ chính lời người tạo")
    input_message: Mapped[str] = mapped_column(doc="Dữ liệu đầu vào đưa cho agent")
    expected: Mapped[str] = mapped_column(doc="Thế nào là đạt, cho máy chấm đọc")
    origin: Mapped[EvalCaseOrigin] = mapped_column(default=EvalCaseOrigin.GENERATED)
    category: Mapped[SuccessCriterionCategory | None] = mapped_column(
        default=None, doc="Check tự động nào chấm case này; null với case V1 và với case sinh từ prohibitions"
    )
    expected_check: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=None, doc="ExpectedCheck đọc thẳng transcript; null = case do LLM judge chấm"
    )
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, doc="Nhãn của người tạo, để lọc — ADR-0054 §1")
    run_as: Mapped[str | None] = mapped_column(
        default=None,
        doc="Case chạy với tư cách ai (DS-05) — giá trị khớp identity.match_by của Hệ CSDL; null = không ai hỏi",
    )
    extra: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=None, doc="Cột ngoài schema khi nạp file (UC-1: principal, retrieval_context, …)"
    )
    cell_key: Mapped[str | None] = mapped_column(default=None, doc="Di sản lưới tình huống (ADR-0065) — chỉ hàng cũ")
    zone: Mapped[str | None] = mapped_column(default=None, doc="Di sản lưới tình huống (ADR-0065) — chỉ hàng cũ")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class EvalRun(Base):
    """One execution of a version's whole test set, and what it decided — see #13.

    `results` holds a **snapshot** of each case as it was when it ran, not a
    foreign key to `eval_cases`. Cases are editable and deletable by design, and
    a result whose case was rewritten afterwards would read as evidence about a
    test that never ran. The same reasoning as `sandbox_runs.transcript`: a
    record of what happened cannot be assembled from rows that changed since.

    `threshold` is likewise the number that was in force at the time. It is not
    what the approval gate compares against — that reads the live configuration
    (ADR-0011 §5) — it is what lets a creator reading an old run see the bar it
    was measured against.

    There is deliberately no `pass_rate` here. A rate is only meaningful beside
    the bar it is being judged against, and this row is not the place that knows
    which bar that is: `src.evals.results.Score` is, and it is the only place.
    """

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id", ondelete="CASCADE"), index=True)
    threshold: Mapped[float] = mapped_column(doc="Ngưỡng đang có hiệu lực lúc chạy")
    passed_count: Mapped[int]
    total_count: Mapped[int]
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, doc="Ordered EvalCaseResult payloads")
    skipped: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        default=list,
        doc="Case rule-based ungrounded với graph của version — không chạy; [{suite_case_id, reason}] (ADR-0054 §1)",
    )
    judge_model: Mapped[str] = mapped_column(
        default="",
        server_default="",
        doc=(
            "Model đã chấm lần chạy này (ADR-0054 §4) — rỗng cho run ghi trước khi cột này tồn tại. "
            "Đổi judge model là đổi baseline: src.pm.reports.version_report không so delta giữa hai lần "
            "chạy có judge_model khác nhau"
        ),
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())



class EvidenceZone(StrEnum):
    """The risk zone a scenario cell belonged to — derived, never declared (ADR-0042 §4).

    Retired with the grid (ADR-0065) and left standing: `evidence_cells` rows
    still hold these values, and `src.evals.thresholds` still records the
    pre-registered bar each zone was measured against. Nothing derives one now.
    """

    NONG = "nong"
    AM = "am"
    NGUOI = "nguoi"


class EvidenceState(StrEnum):
    """A cell's Wilson/rule position at the time it was measured (ADR-0043 §4)."""

    DU_TIN = "du_tin"
    CHUA_CHAC = "chua_chac"
    DANG_HONG = "dang_hong"


class EvidenceCell(Base):
    """The one stored result for one scenario cell under one environment snapshot.

    **Nothing writes this table since ADR-0065 retired the scenario grid.** The
    model and the two rows on the dev database are kept rather than dropped for
    the reason given there: a retirement that destroys the record of what was
    measured cannot be checked afterwards, and an empty table costs nothing.

    ADR-0044 §1 / ADR-0047 §2, imported by ADR-0054 §3. The four-column UNIQUE
    below *is* the invalidation mechanism: a changed document or environment
    means a different key, which means no row, which reads as "chưa chắc" —
    there is no `is_stale` column and no job that has to run for old evidence
    to stop counting. `state` is stored even though it is derivable, because it
    is a snapshot *at measurement time* (the zone threshold is read fresh when
    a decision is made); it exists for display and audit, never to decide.

    The cell points at its suite case (`origin=grid`, ADR-0054 §1 — a cell IS
    an eval case, ADR-0042 §2) and at the `sandbox_runs` its trials left
    behind; it never copies a transcript (ADR-0044 §5).
    """

    __tablename__ = "evidence_cells"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "cell_key",
            "spec_hash",
            "env_fingerprint",
            name="uq_evidence_cells_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id", ondelete="CASCADE"), index=True)
    cell_key: Mapped[str] = mapped_column(index=True, doc="Khóa suy tất định từ giá trị các trục, không phải serial")
    spec_hash: Mapped[str]
    env_fingerprint: Mapped[str]
    zone: Mapped[EvidenceZone]
    eval_case_id: Mapped[int] = mapped_column(ForeignKey("eval_cases.id", ondelete="CASCADE"), index=True)
    n: Mapped[int]
    k: Mapped[int]
    wilson_lower: Mapped[float]
    wilson_upper: Mapped[float]
    state: Mapped[EvidenceState]
    rule_failures: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sandbox_run_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class InterviewSession(Base):
    """One creator's slot-filling interview, kept so they can walk away — see #16.

    The whole row is the answer to *"đóng trang giữa lúc trả lời rồi quay lại thì
    tiếp tục được"*. Every turn of the interview commits, so the state a creator
    comes back to is the state they left rather than the state of the last request
    that happened to finish.

    `slots` holds **only filled slots**, which is what makes the rest derivable.
    Which question comes next, how many are left and whether the interview is over
    are all arithmetic over the slots that are absent from this column
    (`src.interview.slots`), so there is no `status`, no `current_question` and no
    counter here to drift out of step with the answers themselves. A stored status
    would be a second opinion about whether the interview is finished, and the
    ticket asks for exactly one.

    Attached to a user rather than to an agent, because an interview runs *before*
    there is an agent to attach it to: #17 is what turns a finished sheet into a
    draft version, and until then there is nothing in `agents` this could point at.

    `description` is kept beside the slots rather than folded into them. It is what
    later turns are read against — a one-word answer only makes sense next to what
    the creator originally wrote — and it is also the only record of what they said
    in their own words before anything paraphrased it.
    """

    __tablename__ = "interview_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Cascade: an interview is a half-finished thought belonging to one person and
    # is unreadable — and unresumable — once that account is gone.
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    description: Mapped[str] = mapped_column(doc="Mô tả nghiệp vụ ban đầu, nguyên văn lời người tạo")
    slots: Mapped[dict[str, Any]] = mapped_column(JSON, doc="Chỉ những slot đã điền; slot vắng mặt là slot còn trống")
    # Set on the first generation (#17) and never again: an interview describes one
    # agent, so generating a second time adds a draft *version* to this agent
    # rather than a near-duplicate agent beside it. `SET NULL` because an agent the
    # creator deleted must not take their interview with it — the sheet is still
    # theirs, and generating again is how they get a new agent out of it.
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"),
        default=None,
        doc="Agent bản phỏng vấn này đã sinh ra, nếu đã sinh",
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    agent: Mapped["Agent | None"] = relationship()


class SolutionInterviewSession(Base):
    """One creator's V2 solution-design interview, kept so they can walk away — see #42.

    The non-versioned half of ADR-0020 §3. It holds everything that is still
    *unsettled* — a half-filled draft, the contradictions the model flagged, the
    question currently open — precisely so that `AgentVersion.solution_spec` never
    has to: a spec exists only once this session has nothing left open, which is
    what lets every structure downstream assume the spec it receives is complete.

    Unlike V1's `interview_sessions` (whose docstring argues *against* storing a
    status), the model's judgement is stored here rather than re-derived. It has
    to be: ADR-0020 §3 gave up V1's deterministic stop, so "which contradiction
    is still open" is an opinion somebody formed, not arithmetic over which fields
    are empty. What *is* still arithmetic — which of the seven fields are missing,
    and therefore whether the interview may end — is derived from `draft` and has
    no column of its own.

    Every turn commits, for the reason V1 gives: an interview is a thing people
    abandon halfway, so "saved" has to mean "saved as of the last thing you said".
    """

    __tablename__ = "solution_interview_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Cascade, like V1's: a half-finished description of somebody's business is
    # unreadable — and unresumable — once that account is gone.
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    description: Mapped[str] = mapped_column(doc="Mô tả bài toán ban đầu, nguyên văn lời người tạo")
    draft: Mapped[dict[str, Any]] = mapped_column(
        JSON, doc="Chỉ những trường đã trả lời được; trường vắng mặt là trường còn phải hỏi"
    )
    conflicts: Mapped[list[str]] = mapped_column(JSON, doc="Những mâu thuẫn model đang thấy, mỗi cái một câu")
    open_question: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        default=None,
        doc="Câu hỏi đang chờ trả lời `{field, text}`; null nghĩa là phỏng vấn đã đủ",
    )
    # The conversation, verbatim. The designer is single-shot and keeps no memory
    # of its own, so this list *is* the memory — and it is also the record of what
    # the creator said before anything paraphrased it.
    turns: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, doc="Từng lượt hỏi–đáp, theo thứ tự")
    # Set on the first proposal and never again, exactly like V1's: one interview
    # describes one agent, so proposing twice adds a version to that agent rather
    # than a near-duplicate agent beside it.
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"),
        default=None,
        doc="Agent phiên phỏng vấn này đã sinh ra, nếu đã sinh",
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    agent: Mapped["Agent | None"] = relationship()


class ApprovalDecisionKind(StrEnum):
    """The three things an admin can decide about a version — see #14 / ADR-0012.

    `EVAL_OVERRIDE` sits beside the other two rather than under a flag on the
    version, because it is a decision a person made and answers for, not a
    property the version acquired. It is also the only one of the three that does
    not settle the review: it puts a version *into* the queue that the eval bar
    was holding shut, and an `APPROVED` or `REJECTED` still has to follow.
    """

    APPROVED = "approved"
    REJECTED = "rejected"
    EVAL_OVERRIDE = "eval_override"


class ApprovalDecision(Base):
    """One admin decision about one version, kept forever — see #14 / ADR-0012.

    The audit history the issue asks for is these rows read in order. They are
    append-only by construction: nothing in `src.approvals` updates or deletes
    one, and a version that goes round the loop twice (rejected, revised,
    resubmitted) accumulates rows rather than overwriting them.

    `reason` is nullable in the schema and mandatory in the domain for the two
    kinds that require it. The constraint is not expressible per-kind in a column,
    so `src.approvals.decisions` is where it is enforced — and it is enforced
    before the transition, so there is no way to reach `rejected` without one.

    `eval_score` is a snapshot for the same reason `eval_runs.results` is one:
    the gate reads the *latest* eval run, so a creator who evaluates again after
    an override would otherwise rewrite what the override was granted against.
    Without it, the one record that has to survive scrutiny — "this bar was
    waived, and here is how far short it was" — could not be read back.
    """

    __tablename__ = "approval_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Same cascade as `sandbox_runs` and `eval_runs`: a decision is a statement
    # about one immutable spec and is unreadable once that spec is gone.
    version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id", ondelete="CASCADE"), index=True)
    # No cascade: an audit record whose actor disappeared is worse than one
    # naming an account that no longer exists.
    admin_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[ApprovalDecisionKind]
    reason: Mapped[str | None] = mapped_column(default=None, doc="Bắt buộc với từ chối và vượt rào")
    eval_score: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=None, doc="Điểm eval tại đúng thời điểm quyết định — bản chụp, không đọc lại từ eval_runs"
    )
    # No `admin` relationship. The history of one version is a handful of rows,
    # and `src.api.approval_routes` looks the account up by id rather than
    # joining — a relationship nobody uses is a join waiting to be written by
    # accident on a query that did not want one.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RunApprovalStatus(StrEnum):
    """Where one paused-at-a-person moment of a run has got to.

    Not `ApprovalDecisionKind` reused: that enum decides about an
    `AgentVersion` — a document, before it may run at all (ADR-0012) — and this
    one is about a *running* graph stopped at a node. Folding them together
    would give one word two subjects, and `eval_override` means nothing here.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RunApproval(Base):
    """One `HumanApproval` node pausing one run, and how it was decided — #47 / ADR-0024.

    **The checkpoint is what resume actually reads; this table is what a person
    can query.** `interrupt()` suspends the graph and the checkpointer persists
    exactly where it stopped, but a checkpoint is opaque: nobody can ask Postgres
    "which runs are waiting on a human right now" without deserialising every
    in-flight one. These rows are written *alongside* the checkpoint, never
    instead of it (ADR-0024 §2) — the same division ADR-0012 §1 drew between the
    dossier a reviewer reads and the state machine that actually gates a run.

    **Append-only, keyed `(run_id, node_id, occurrence_index)`** (ADR-0024 §3). A
    `HumanApproval` node inside a `While` body pauses once per pass, so the same
    node on the same run accumulates rows rather than overwriting one. A row is
    written on the node's first execution and found again — not duplicated —
    when `interrupt()` replays it on resume, because the key is derived from the
    run's own state rather than minted fresh. A decision names the row by id
    rather than resolving "the newest pending one" for a pair, since a reviewer
    is acting on a row they were shown: resolving it again at decision time
    would let a second pause, opened between the two moments, receive a verdict
    passed on the first.

    **It also carries what the pause has to remember.** `interrupt()` stops a run
    in the middle of an HTTP response that is already closing, so everything the
    resumed half needs has to survive here: which thread to address
    (`agent_id`/`conversation_id`/`namespace`), what the run was asked
    (`input_message`), and — for a trial — the transcript lines so far, so the
    completed run stores *one* `sandbox_run` rather than two halves of one piece
    of evidence.

    `elapsed_seconds` and `usage_by_model` are the ledger at the moment of the
    pause. ADR-0024 §7 excludes time spent waiting on a person from
    `max_wall_seconds` — a run must not be starved of budget by a slow reviewer —
    which is only expressible if the *running* time so far is written down
    somewhere the clock cannot keep ticking.

    `approver_role` is copied off `HumanApproval.config` and shown to a reviewer,
    but does not gate who may decide — ADR-0024 §5 declines to build an
    enforcement path against a role space V2 has not defined. Who *may* decide is
    the route's business (`src.api.workflow_run_routes`), and `decided_by` records
    whoever did, whatever check let them through.
    """

    __tablename__ = "run_approvals"
    #: ADR-0024 §3's key, kept by the database rather than by whoever writes the
    #: row. The `HumanApproval` node writes its row on first execution and looks
    #: the *same* row up when `interrupt()` replays it on resume; without this,
    #: a bug in that lookup would show up as two pending approvals for one pause
    #: rather than as an error.
    __table_args__ = (UniqueConstraint("run_id", "node_id", "occurrence_index", name="uq_run_approvals_occurrence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    #: One graph execution — minted per run, not V1's conversation thread
    #: (ADR-0024 §4). Indexed because "what is this run waiting on" is the
    #: question the resume path asks.
    run_id: Mapped[str] = mapped_column(index=True, doc="Một lần chạy graph, khác với thread hội thoại")
    node_id: Mapped[str] = mapped_column(doc="Node HumanApproval nào đang dừng")
    occurrence_index: Mapped[int] = mapped_column(
        default=0, doc="Lần dừng thứ mấy của node này trong run này — While có thể đi qua nó nhiều vòng"
    )
    # Same cascade as `sandbox_runs`: an approval is a statement about one
    # version's run and says nothing once that version is gone.
    version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str] = mapped_column(doc="Thread lần chạy này diễn ra trên")
    namespace: Mapped[str] = mapped_column(doc="`trial` hay `prod` — nửa còn lại của địa chỉ thread")
    #: Ai đang nói, trên thread mà nhiều người cùng nói được (`ThreadRef.caller`).
    #: `None` trên thread `trial` — đó là không gian của riêng người tạo. Cột này
    #: sinh ra vì workflow run đã có cửa `prod`: thiếu nó, lượt chạy tiếp sau khi
    #: duyệt địa chỉ một hội thoại khác với hội thoại nó vừa dừng, và graph chạy
    #: lại từ node đầu thay vì đi tiếp từ checkpoint.
    caller: Mapped[str | None] = mapped_column(default=None, doc="Người gọi, nửa thứ ba của địa chỉ thread prod")
    sandbox: Mapped[bool] = mapped_column(doc="True = lần chạy thử; quyết định vẫn là quyết định thật")
    trace_id: Mapped[str] = mapped_column(doc="Trace của lần chạy, giữ nguyên qua chỗ dừng")
    approver_role: Mapped[str] = mapped_column(doc="Vai trò node yêu cầu — thông tin cho người duyệt, không phải quyền")
    message: Mapped[str] = mapped_column(doc="`message_template` đã dựng xong, đúng thứ người duyệt đọc")
    input_message: Mapped[str] = mapped_column(doc="Câu người dùng đã hỏi ở lần chạy này")
    status: Mapped[RunApprovalStatus] = mapped_column(default=RunApprovalStatus.PENDING)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    reason: Mapped[str | None] = mapped_column(default=None, doc="Bắt buộc khi từ chối — như ADR-0012 §2")
    run_as: Mapped[str | None] = mapped_column(
        default=None,
        doc="Người hỏi của lần chạy thử (DS-05) — để chặng tiếp sau khi duyệt đọc dữ liệu đúng tư cách đó",
    )
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, doc="Các dòng transcript trước lúc dừng, để lần chạy nối lại lưu thành một bản ghi"
    )
    #: DS-08: the calls this pause stands in front of, `{node_id, tool, args,
    #: hash}` each — what the card shows verbatim and what the action node
    #: recomputes `hash` against before calling. Written once, with the row.
    actions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, doc="Các lời gọi node duyệt này đứng trước — payload nguyên văn và hash của nó"
    )
    elapsed_seconds: Mapped[float] = mapped_column(default=0.0, doc="Thời gian *chạy* trước lúc dừng, không tính chờ")
    usage_by_model: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, doc="Token đã tiêu trước lúc dừng, để trần token vẫn tính cho cả lần chạy"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TemplateSubmission(Base):
    """One creator's "save as template" candidate, and the admin review it is under — #53 / ADR-0025 §6-8.

    The DB half of `TEMPLATE_REGISTRY` (`src.workflow.templates`), the same
    registry/DB split ADR-0023 §1 already draws for `ToolDefinition`. Only ever
    `source: creator_submitted` — the built-in seed is code and never lands a row
    here — so there is no `source` column, only the status a row moves through:
    `pending` on arrival, then `approved`/`rejected`/`disabled` by an admin.

    `{nodes, edges, start_node_id}` are a **literal copy**, stripped per §8
    (`src.workflow.templates.strip_for_submission`), taken once at submission
    time from a `workflow_confirmed` version — never a live reference back to it.
    That is what keeps rejecting or disabling a submission from touching any
    `WorkflowJSON` a Workflow Generator run already produced from it while it was
    `approved` — the same "copy, not a pointer" guarantee `TemplateSpec.skeleton`
    already gives the built-in half of the registry.

    `source_version_id` is kept only as provenance for an admin reading the
    queue — which workflow this was copied from — and is `SET NULL` on delete
    for `InterviewSession.agent_id`'s reason: the submission is the creator's own
    artifact and must survive the source agent being deleted later.
    """

    __tablename__ = "template_submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # No cascade: a submission is the creator's own artifact under review and
    # must survive their account existing to be read by, but ownership itself is
    # audit information — same reasoning as `ApprovalDecision.admin_id`.
    submitted_by: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="SET NULL"),
        default=None,
        doc="Version workflow_confirmed đã copy ra template này — chỉ để tra nguồn gốc, không phải tham chiếu sống",
    )
    name: Mapped[str] = mapped_column(doc="Tên template, người tạo tự đặt")
    description: Mapped[str] = mapped_column(
        doc="Template này hợp với bài toán nào — Generator đọc câu này để chọn, như TemplateSpec.description"
    )
    nodes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, doc="NodeInstance đã strip theo ADR-0025 §8 — nguyên văn từ workflow gốc trừ chỗ đã xoá"
    )
    edges: Mapped[list[dict[str, Any]]] = mapped_column(JSON, doc="Edge nguyên văn từ workflow gốc")
    start_node_id: Mapped[str] = mapped_column(doc="Node bắt đầu, nguyên văn từ workflow gốc")
    required_data_kinds: Mapped[list[str]] = mapped_column(
        JSON, default=list, doc="DataSourceKind template này cần — người tạo tự khai lúc nộp, như TemplateSpec"
    )
    status: Mapped[TemplateStatus] = mapped_column(default=TemplateStatus.PENDING)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    rejection_reason: Mapped[str | None] = mapped_column(default=None, doc="Bắt buộc khi từ chối, như ApprovalDecision")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ToolRegistration(Base):
    """One creator's MCP connection awaiting admin review — #52 / ADR-0023 §1, §7.

    The DB half of `TOOL_DEFINITIONS` (`src.workflow.tools`), exactly the split
    `TemplateSubmission` is for the template catalog. Only ever
    `source: creator_registered` — the built-in connections are code and never
    land a row here — so there is no `source` column, only the `status` a row
    moves through: `pending` on arrival, then `approved`/`rejected` by an admin,
    and `disabled` afterwards.

    **`actions` is introspected, and nothing but introspection writes it.**
    `src.workflow.registrations.register_tool` fills it from the server's own
    `tools/list` at submission time (ADR-0023 §7) and no endpoint on the review
    path can touch it — an admin overriding risk writes `risk_overrides`, a
    creator supplying mocks writes `mock_response_examples`, and the schema
    itself is only ever what the server said. That is what keeps a registry
    entry from drifting away from what the server actually implements.

    `risk_overrides` and `mock_response_examples` are keyed by action name
    rather than being members of each `actions` entry, so that the introspected
    document stays a *verbatim* copy of the report: a review that edited entries
    in place would leave nobody able to say which parts came from the server.

    `allowed_actions` is the third of those side tables and is this platform's
    `allowed_tools` — the field OpenAI's Agent Builder puts on its MCP node. It
    sits on the *connection* rather than on each node, because a node here names
    exactly one action (ADR-0021 §2): what "which of this server's tools may be
    used at all" needs is one answer an admin reviewed once, not the same answer
    re-entered on every node that names one. Empty means every introspected
    action, so a row written before the column existed goes on meaning what it
    always meant.
    """

    __tablename__ = "tool_registrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # No cascade, for `TemplateSubmission.submitted_by`'s reason: who registered
    # a connection is audit information about a decision an admin took.
    submitted_by: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(doc="Tên hiển thị của kết nối, người tạo tự đặt")
    server_url: Mapped[str] = mapped_column(doc="Địa chỉ MCP server — thứ `tools/list` đã được gọi tới")
    #: Agent Builder's `server_description`, and it earns its place for the same
    #: reason: a `full`-mode node hands a model several connections' actions at
    #: once, and "which of these is the ERP" is a question the id alone does not
    #: answer. Optional, because a creator with one connection has nothing to
    #: disambiguate.
    description: Mapped[str] = mapped_column(default="", doc="Mô tả kết nối, giúp model chọn đúng khi có nhiều")
    credential_kind: Mapped[CredentialKind] = mapped_column(doc="Kiểu xác thực; không bao giờ là chính bí mật")
    credential_provider: Mapped[str] = mapped_column(default="", doc="Nhà cung cấp OAuth, ví dụ 'google'")
    credential_scopes: Mapped[list[str]] = mapped_column(JSON, default=list, doc="Quyền OAuth kết nối này xin")
    credential_secret_ref: Mapped[str] = mapped_column(default="", doc="Khoá trong secret store, không phải bí mật")
    actions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, doc="`IntrospectedAction` nguyên văn từ `tools/list` — không ai sửa được sau khi đã lấy về"
    )
    allowed_actions: Mapped[list[str]] = mapped_column(
        JSON,
        default=list,
        doc="Các action của kết nối này được phép dùng; rỗng = tất cả. Agent Builder gọi là `allowed_tools`",
    )
    risk_overrides: Mapped[dict[str, str]] = mapped_column(
        JSON, default=dict, doc="Mức rủi ro admin tự đặt, khoá là tên action — ADR-0023 §7"
    )
    mock_response_examples: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, doc="Ví dụ kết quả cho sandbox, khoá là tên action — bắt buộc với action medium/high"
    )
    usage_scope: Mapped[UsageScope] = mapped_column(default=UsageScope.PUBLIC)
    status: Mapped[ToolStatus] = mapped_column(default=ToolStatus.PENDING)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    rejection_reason: Mapped[str | None] = mapped_column(default=None, doc="Bắt buộc khi từ chối, như ApprovalDecision")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SecretStoreEntry(Base):
    """One secret, encrypted at rest — RAG-19 / ADR-0045 §5.

    The store `ToolRegistration.credential_secret_ref` was always a promise to
    build: a row here holds a `key` and nothing but the AES-256-GCM ciphertext
    of the value it protects, so a dump of this table alone decrypts nothing —
    the key that opens it (`settings.secret_store_key`) never touches the
    database. `src.services.secret_store.EncryptedDbSecretStore` is the only
    code allowed to read or write this table; every other caller goes through
    its `get`/`put`/`delete`/`rotate`.

    One table for every secret this platform stores, MCP connections today and
    BYODB's external connections later (ADR-0045's whole point) — a second
    table would be the second credential mechanism the ADR rejected.
    """

    __tablename__ = "secret_store_entries"

    key: Mapped[str] = mapped_column(primary_key=True, doc="Cái mà `ToolCredential.secret_ref` trỏ tới")
    ciphertext: Mapped[bytes] = mapped_column(
        LargeBinary, doc="Nonce 12 byte nối với ciphertext AES-256-GCM — không phải bí mật gốc"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    rotated_at: Mapped[datetime | None] = mapped_column(default=None, doc="Lần `rotate` gần nhất, None nếu chưa rotate")


class ExternalConnectionProvider(StrEnum):
    """Which vector database a BYODB connection points at — RAG-20 / #106.

    Four because that is the spec's own list. Only `qdrant` has a real prober
    behind it today (`src.services.connection_probe`) — `qdrant-client` is
    already a dependency (ADR-0044 §1) and none of the other three SDKs are.
    The other three still create rows and still go through the same SSRF-gated
    `/test` endpoint; they just cannot be told `connected` yet, honestly, via
    `ProviderNotSupportedError` rather than a guess.
    """

    QDRANT = "qdrant"
    PINECONE = "pinecone"
    WEAVIATE = "weaviate"
    PGVECTOR = "pgvector"


class ExternalConnectionStatus(StrEnum):
    """Where one BYODB connection stands, per the ticket's own three states.

    `untested` is what a fresh row starts as — creating a connection never
    dials out on its own (only `/test` does), so a row nobody has tested yet
    must not claim either verdict. `connected`/`failed` are only ever written
    by `src.services.connection_probe.probe_connection`, never chosen by a
    caller — the same discipline `ToolRegistration.actions` uses for staying
    an honest report rather than a claim somebody typed in.
    """

    UNTESTED = "untested"
    CONNECTED = "connected"
    FAILED = "failed"


class VectorDistanceMetric(StrEnum):
    """The similarity function a BYODB collection was built with.

    A creator's own declaration — this platform never computes it, only
    carries it through to whatever the retrieval side (a later ticket) needs
    to know to score results the way the collection's own index does.
    """

    COSINE = "cosine"
    EUCLIDEAN = "euclidean"
    DOT = "dot"


class ExternalConnection(Base):
    """One creator's pointer at a vector database this platform does not run — RAG-20 / #106.

    The platform only ever **queries through** a row here — ADR-0045's whole
    argument for building a secret store first was that BYODB needed
    somewhere to put a credential that was not a second encrypted-column
    mechanism, and `secret_ref` is that: a key into
    `src.services.secret_store`, never a value, exactly like
    `ToolRegistration.credential_secret_ref`. Empty means the collection is
    reachable with no credential at all, which a self-hosted Qdrant with no
    API key configured genuinely can be.

    `connection_url` is the one field the ticket's own column list does not
    name but the acceptance criteria require — "Nhập `http://169.254.169.254/...`"
    has to be typed in somewhere, and it is not `secret_ref` (RAG-19 was
    explicit that reference is not config) and not `collection_name` (a name,
    not an address). It is the *only* thing about this row that is ever
    dialled: nothing reads it except `src.services.connection_probe`, and
    every read goes through `src.services.ssrf_guard` first — never on
    `POST`, which only ever writes a row, but on every `POST .../test`, which
    is the one endpoint that actually leaves this process.

    `status`/`status_detail`/`last_checked_at` are the *last* probe's verdict,
    not a live fact — a Qdrant instance can go down a minute after `test`
    says `connected`, and nothing here claims otherwise. Re-running `/test` is
    how a creator gets a fresher answer.
    """

    __tablename__ = "external_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Cascade, like `KnowledgeBase.owner_id`: a BYODB pointer is a per-creator
    # resource, unreadable once the account that owns it is gone.
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(doc="Tên kết nối, người tạo tự đặt")
    provider: Mapped[ExternalConnectionProvider]
    connection_url: Mapped[str] = mapped_column(
        doc="Địa chỉ vector DB ngoài — thứ duy nhất /test thực sự gọi tới, luôn qua ssrf_guard trước"
    )
    secret_ref: Mapped[str] = mapped_column(
        default="", doc="Khoá trong secret store cho API key/mật khẩu, nếu provider cần — không phải chính bí mật"
    )
    collection_name: Mapped[str] = mapped_column(doc="Collection/index/table đã có sẵn trên vector DB ngoài")
    embedding_dimension: Mapped[int] = mapped_column(
        doc="Dimension người tạo khai — đối chiếu với dimension thật của collection ở bước /test"
    )
    distance_metric: Mapped[VectorDistanceMetric]
    field_mapping: Mapped[dict[str, str]] = mapped_column(
        JSON,
        default=dict,
        doc="Tên field thay cho chuẩn mặc định, ví dụ {'text_field': 'content'} khi schema ngoài khác chuẩn",
    )
    status: Mapped[ExternalConnectionStatus] = mapped_column(default=ExternalConnectionStatus.UNTESTED)
    status_detail: Mapped[str | None] = mapped_column(default=None, doc="Vì sao lần /test gần nhất connected/failed")
    # A plain string column, not `Mapped[ProbeFailureReason]`: importing
    # `src.services.connection_probe` here would point the model layer at a
    # service that already imports it back. The values are that enum's, and
    # `ExternalConnectionResponse.status_reason` is typed as it — the column
    # stores what the probe decided, it does not define the vocabulary.
    status_reason: Mapped[str | None] = mapped_column(
        default=None, doc="Loại lỗi của lần /test gần nhất (ProbeFailureReason) — để UI viết lời khuyên dễ hiểu"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(default=None, doc="Lúc /test chạy lần gần nhất")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DatabaseDialect(StrEnum):
    """Which engine a Hệ CSDL runs — ADR-0059 §8."""

    POSTGRESQL = "postgresql"
    SQLSERVER = "sqlserver"
    MYSQL = "mysql"


class DatabaseSourceStatus(StrEnum):
    """Where a Hệ CSDL's last probe landed.

    `NEEDS_DSN` is a row that has no DSN in the secret store to probe with —
    every row carried over from `uc1_connections`, whose credentials lived in
    a dev-mode Vault that does not survive a restart (DS-02).
    """

    UNTESTED = "untested"
    CONNECTED = "connected"
    FAILED = "failed"
    NEEDS_DSN = "needs_dsn"


class DatabaseSource(Base):
    """One creator's Hệ CSDL — a business database an agent may read, DS-02 / ADR-0059 §2.

    Absorbs `UC1Connection`. **The DSN is never a column**: it lives in the
    secret store (`src.services.secret_store`) under `dsn_secret_ref`, and a
    dump of this table names a key, never a password — the spec's
    "DSN không bao giờ nằm trong bảng `database_sources`".

    `allowed_views` is what the creator ticked; `view_columns`/`view_row_counts`
    /`base_tables` are the *last probe's* snapshot of the database, kept so a
    tick can be checked (and a base table refused) without dialling out again,
    and so `data_query` (DS-04) can check columns against what was really
    there. Like `ExternalConnection.status`, a snapshot is not a live fact.
    """

    __tablename__ = "database_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(doc="Tên Hệ CSDL, người tạo tự đặt")
    dialect: Mapped[DatabaseDialect]
    dsn_secret_ref: Mapped[str] = mapped_column(
        default="", doc="Khoá trong secret store giữ DSN — rỗng khi chưa có DSN (NEEDS_DSN), không bao giờ là DSN"
    )
    allowed_views: Mapped[list[str]] = mapped_column(JSON, default=list, doc="Các view người tạo cho phép agent đọc")
    view_columns: Mapped[dict[str, list[str]]] = mapped_column(
        JSON, default=dict, doc="{view: [cột, …]} của lần kiểm tra gần nhất"
    )
    view_row_counts: Mapped[dict[str, int]] = mapped_column(
        JSON, default=dict, doc="{view: số dòng} của lần kiểm tra gần nhất"
    )
    base_tables: Mapped[list[str]] = mapped_column(
        JSON, default=list, doc="Bảng gốc lần kiểm tra gần nhất thấy — để từ chối khi bị tick"
    )
    last_probe_status: Mapped[DatabaseSourceStatus] = mapped_column(default=DatabaseSourceStatus.UNTESTED)
    last_probe_reason: Mapped[str | None] = mapped_column(
        default=None, doc="ProbeFailure của lần kiểm tra gần nhất — để UI viết lời khuyên, không phải lời của driver"
    )
    last_probe_detail: Mapped[str | None] = mapped_column(default=None)
    last_probed_at: Mapped[datetime | None] = mapped_column(default=None)
    access_rules: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        default=None,
        doc="Quy tắc truy cập (access.yaml: identity + rules, DS-03) — null khi chưa khai, tức cấm hết",
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UC1ConnectionProbeStatus(StrEnum):
    """Where one UC-1 connection's last dry-run probe (FR-23) landed. Not `ExternalConnectionStatus`
    on purpose — same three shapes, unrelated concern (a connection into a
    customer's own ERP, not a BYODB vector database), and reusing the V2 enum
    would couple UC-1 to a table it has nothing to do with. `UNTESTED` is a
    fresh row nobody has probed yet.
    """

    UNTESTED = "untested"
    CONNECTED = "connected"
    FAILED = "failed"


class UC1Connection(Base):
    """UC-1's own connection registry — UC1-08 (#122), spec.md §4.3/§6.2/§19.

    Distinct from `ExternalConnection` (BYODB vector databases, RAG-20): this
    row is what `AgentSpec.connections[].credential_ref` points at — a name
    like `erp_readonly` that resolves, through `vault_path`, to a secret this
    table never stores a byte of. `credential_ref` is kept as a column (rather
    than derived on every read) because it is the exact string a spec's
    `connections[].credential_ref` must equal — computing it twice in two
    modules would be two chances for the `vault://` prefix to drift.

    `allowed_objects`/`allowed_hosts`/`read_only` mirror the spec's own
    `Connection` fields (`src.agentspec.spec.Connection`) rather than being
    read out of a spec at probe time: a connection is wired up and probed
    *before* any spec references it (§8's step 5 precedes step 6), so there is
    no spec yet to read them from.
    """

    __tablename__ = "uc1_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True, doc="Cũng là đoạn cuối của credential_ref, ví dụ erp_readonly")
    type: Mapped[ConnectionType]
    allowed_objects: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed_hosts: Mapped[list[str]] = mapped_column(JSON, default=list)
    read_only: Mapped[bool] = mapped_column(default=True)
    vault_path: Mapped[str] = mapped_column(
        doc="Đường dẫn KV v2 trong Vault, không gồm mount — ví dụ conn/erp_readonly"
    )
    credential_ref: Mapped[str] = mapped_column(
        doc="vault://<vault_path> — giá trị spec.connections[].credential_ref phải khớp"
    )
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_probe_status: Mapped[UC1ConnectionProbeStatus] = mapped_column(default=UC1ConnectionProbeStatus.UNTESTED)
    last_probe_detail: Mapped[str | None] = mapped_column(default=None)
    last_probe_objects: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, doc="[{'object': 'v_ar_open', 'row_count': 1284}, ...] của lần probe gần nhất"
    )
    last_probed_at: Mapped[datetime | None] = mapped_column(default=None)


class UC1AgentSpecVersion(Base):
    """One immutable `AgentSpec` snapshot — UC1-38 (#126) is the first thing
    that needs to address a UC-1 spec by `(agent_id, version)` over HTTP
    (`POST /agents/{id}/versions/{v}/sandbox/chat`), so this is where that
    persistence starts.

    Deliberately not `AgentVersion` (V2's table): that row's `spec` column,
    `status` enum (`VersionStatus`), and `agent_id` (an int FK to V2's
    `agents`) all belong to a different pipeline — `src.agentspec.spec`'s own
    module docstring already draws this line ("This is *not*
    `src/models/agent_spec.py`"). `agent_id` here is `AgentSpec.metadata.id`
    (a string like `agt_ar_assistant`), not a foreign key, for the same
    reason: UC-1 has no `agents` table of its own yet, and `metadata.id` is
    already the stable identifier the spec, the Postgres role (FR-34), and
    the container name all key off.
    """

    __tablename__ = "uc1_agent_spec_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_uc1_agent_spec_versions_agent_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[str] = mapped_column(index=True, doc="AgentSpec.metadata.id, ví dụ agt_ar_assistant")
    version: Mapped[int]
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, doc="AgentSpec.model_dump(mode='json', by_alias=True)")
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UC1SandboxRunStatus(StrEnum):
    """How one sandbox turn (FR-36) ended. `BUDGET_EXCEEDED` is FR-39's own
    outcome, kept distinct from `COMPLETED` because the approval dossier
    (UC1-16, a later ticket) needs to tell "the agent answered" from "the
    agent hit its ceiling mid-turn" at a glance, not by parsing the transcript.
    """

    COMPLETED = "completed"
    BUDGET_EXCEEDED = "budget_exceeded"


class UC1SandboxRun(Base):
    """One sandbox chat turn's transcript — UC1-38 (#126), FR-36/FR-37/FR-38.

    `thread_id` carries the `sandbox:` namespace prefix
    (`src.compiler.sandbox.sandbox_thread_id`) so a row here is visibly never
    a production conversation, even read straight out of the table. FR-38
    ("không xin duyệt nếu chưa có ít nhất một lượt chat thử") is answered by
    *this table having any row at all* for `(agent_id, version)` — UC1-39
    (#127, not this ticket) is what wires that count into
    `submit-for-approval`.
    """

    __tablename__ = "uc1_sandbox_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[str] = mapped_column(index=True)
    version: Mapped[int]
    principal_employee_id: Mapped[str]
    thread_id: Mapped[str]
    message: Mapped[str] = mapped_column(doc="Câu người tạo gõ vào ô chat thử")
    status: Mapped[UC1SandboxRunStatus]
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, doc="[{'node','tool','args','result','tokens','latency_ms','cost_usd'}, ...] theo đúng thứ tự chạy"
    )
    total_cost_usd: Mapped[float]
    final_answer: Mapped[str | None] = mapped_column(default=None, doc="None nếu lượt này dừng vì budget_exceeded")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UC1EvalRun(Base):
    """One golden-set smoke test run — UC1-17 (#133), FR-33, §11.2.

    One row per `POST /agents/{id}/versions/{v}/golden-set/run` call: the
    aggregate `pass_rate` `submit-for-approval` reads (replacing UC1-39's own
    stand-in), and `case_results` — one entry per one of the 23 cases
    (`{case_id, group, judge, passed, detail}`) — is what UC1-17's own "log kết
    quả từng case vào hồ sơ duyệt" means: the dossier (UC1-16) renders this
    column rather than re-deriving it from a transcript nobody kept.

    Never updated in place. A version resubmitted after a fix runs the golden
    set again and gets a new row, so `submit-for-approval`'s history shows every
    attempt rather than only the last.
    """

    __tablename__ = "uc1_eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[str] = mapped_column(index=True)
    version: Mapped[int]
    passed_count: Mapped[int]
    total_count: Mapped[int]
    pass_rate: Mapped[float]
    case_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, doc="[{'case_id','group','judge','passed','detail'}, ...] — one entry per golden-set case"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UC1VersionApproval(Base):
    """One admin's `approve` decision on one version — UC1-17 (#133), FR-30/FR-32.

    `lifecycle.approve` is a pure function that needs every signer's id *at
    once* (`approver_ids: list[str]`), but HITL #2 arrives one admin, one HTTP
    call, at a time — this table is what accumulates them between calls, so
    the version transitions to `approved` the moment the count reaches
    `authority.approval_quorum`, not before.

    `UniqueConstraint(agent_id, version, admin_id)` is what makes "cùng một
    người duyệt hai lần không đủ quorum" true by construction rather than by a
    query remembering to `DISTINCT`: a second approve call from the same admin
    on the same version simply has nothing new to insert.
    """

    __tablename__ = "uc1_version_approvals"
    __table_args__ = (UniqueConstraint("agent_id", "version", "admin_id", name="uq_uc1_version_approvals_admin"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[str] = mapped_column(index=True)
    version: Mapped[int]
    admin_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UC1PendingActionStatus(StrEnum):
    """Where one write-tool call's `require_approval` decision sits — UC1-21
    (#137)/UC1-23 (#139), FR-42/43/45/46. `APPROVED` is only ever reached
    together with a real, PEP-reverified execution
    (`src.api.uc1_action_approval_routes.decide_action`) in the same call
    that sets it — there is no separate "approved but not yet executed"
    state, because nothing in this batch's tickets needs one and a row that
    could sit `approved` with no `result` yet would be a state a caller could
    mistake for "the email was sent".
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class UC1PendingAction(Base):
    """One `scope: write` tool call the PEP answered `require_approval` for —
    UC1-21 (#137), FR-42/FR-43 — plus UC1-23's (#139) own two integrity rules
    on the same row: `action_hash` is fixed at creation (FR-45, from
    `src.compiler.tools.hash_action` over the *exact* `args` the model called
    with) and never recomputed here, so a later edit to `args` (were one ever
    made) stops matching it; `requested_by_*` is where FR-46's "approver ≠
    requester" check on the decision endpoint reads the original asker's
    identity from, since that identity has to survive past the graph turn
    that created this row to be compared against whoever clicks approve,
    possibly in a later request entirely.

    Deliberately not keyed to `UC1AgentSpecVersion` by a foreign key:
    `agent_id`/`version` name a spec the same string-keyed way every other
    UC-1 table does (`UC1SandboxRun`'s own docstring gives the same reason).
    """

    __tablename__ = "uc1_pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[str] = mapped_column(index=True)
    version: Mapped[int]
    tool: Mapped[str]
    args: Mapped[dict[str, Any]] = mapped_column(JSON)
    action_hash: Mapped[str] = mapped_column(index=True, doc="FR-45 — hash_action(tool, args) at creation time, fixed")
    connection_id: Mapped[str]
    approver_roles: Mapped[list[str]] = mapped_column(
        JSON, default=list, doc="authz.rego's effective_approver_roles at request time"
    )
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    requested_by_sub: Mapped[str] = mapped_column(doc="Principal.sub of whoever asked — FR-46's own comparison target")
    requested_by_employee_id: Mapped[str]
    requested_by_department: Mapped[str]
    requested_by_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[UC1PendingActionStatus] = mapped_column(default=UC1PendingActionStatus.PENDING)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    decision_reason: Mapped[str | None] = mapped_column(default=None)
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=None, doc="The real implementation's own return value, once executed"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(default=None)


class KnowledgeBaseMode(StrEnum):
    """Where a Knowledge Base's Documents actually live — see #90 / ADR-0044 §1.

    `managed` is every Knowledge Base this ticket creates: ingested through this
    platform, chunked and embedded into the shared Qdrant collection. `byodb`
    is RAG-20's "connect your own Qdrant" (#106) — a row of this mode has no
    Documents of its own here, it names an `external_connection_id` instead.
    The column exists now so that later ticket does not have to widen a table
    that governs which agents may search which shelf.
    """

    MANAGED = "managed"
    BYODB = "byodb"


class KnowledgeBase(Base):
    """A named shelf of Documents a `KnowledgeSearch` node can be pointed at — #90 / ADR-0043.

    ADR-0043 §1 is why this table exists at all: a node addresses a Knowledge
    Base, never a single Document, so that a creator adding a document to a
    shelf is not a workflow edit. Wiring `source_ids` to actually hold these
    ids is RAG-05 (#91) — this ticket only builds the shelf and what sits on
    it, not the node that reads from one.
    """

    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Cascade, like `InterviewSession.owner_id`: a shelf is a per-creator
    # resource, and its Documents (`Document.kb_id` cascades off *this* column
    # in turn) are unreadable once the account that owns them is gone.
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(doc="Tên tủ tài liệu, người tạo tự đặt")
    description: Mapped[str] = mapped_column(default="", doc="Tủ này dùng cho việc gì — tuỳ chọn")
    mode: Mapped[KnowledgeBaseMode] = mapped_column(default=KnowledgeBaseMode.MANAGED)
    # FK landed by RAG-20 (#106), `external_connections` now existing to point
    # at. No `ondelete`: `src.workflow.external_connections.delete_external_connection`
    # refuses outright while any Knowledge Base still names it (the ticket's own
    # "chặn nếu đang được ≥1 KB dùng"), so the database is never asked to cascade
    # or null this out — a delete that reached the database at all is a bug.
    external_connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_connections.id"),
        default=None,
        doc="Kết nối vector DB ngoài, nếu mode=byodb — RAG-20 (#106)",
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    documents: Mapped[list["Document"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")
    #: The other side of `Agent.knowledge_bases` — which agents may search this
    #: shelf. Read-only in practice: writes go through `set_agent_knowledge_bases`
    #: on the agent side, the same direction the assignment endpoint edits.
    agents: Mapped[list["Agent"]] = relationship(secondary="agent_kb_links", viewonly=True)
    #: The connection behind `external_connection_id`, `None` on a `managed`
    #: shelf. Read-only from this side: a shelf's mode and its connection are
    #: chosen once, at creation (`create_knowledge_base`), and re-pointing a
    #: shelf at a different database is not an edit this platform offers —
    #: what a `KnowledgeSearch` node was confirmed against would change under
    #: it without the graph changing at all.
    external_connection: Mapped["ExternalConnection | None"] = relationship(viewonly=True, lazy="joined")

    @property
    def document_count(self) -> int:
        """How many Documents sit on this shelf — RAG-13's list screen needs this without a second round trip."""
        return len(self.documents)

    @property
    def agent_names(self) -> list[str]:
        """Which agents currently link this shelf, by name — RAG-13's "agent nào đang dùng" column."""
        return sorted(agent.name for agent in self.agents)

    @property
    def external_connection_name(self) -> str | None:
        """Which external database this shelf reads from, by the creator's own name for it — RAG-22's badge.

        The name, not the address: the shelf list is where a creator tells
        "Tự quản lý" from "Kết nối ngoài" at a glance, and `connection_url` is
        neither recognisable nor safe to scatter across a list screen.
        """
        return self.external_connection.name if self.external_connection else None

    @property
    def external_connection_status(self) -> "ExternalConnectionStatus | None":
        """The last `/test` verdict on this shelf's connection — never a live fact (`ExternalConnection.status`)."""
        return self.external_connection.status if self.external_connection else None


class DocumentStatus(StrEnum):
    """Where one Document has got to in the ingest pipeline — see #90, orchestrated by #94.

    `pending → extracting → chunking → embedding → indexed`, in the order
    ADR-0044 §4-§6 runs them: a Document is unusable for search until it
    reaches `indexed`, and this column is how a creator's upload screen (#98)
    tells "still working" from "done" from "broken". `failed` sits beside the
    pipeline rather than at the end of it — any stage can fail, and the stage
    that did is exactly what `status_detail` says.

    This ticket only defines the states; nothing here transitions a row
    through them. That state machine is RAG-08's orchestrator (#94) — until it
    lands, a freshly stored Document simply stays `pending`.
    """

    PENDING = "pending"
    EXTRACTING = "extracting"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXED = "indexed"
    FAILED = "failed"


class Document(Base):
    """One uploaded file on a Knowledge Base — see #90 / ADR-0043.

    **The identity is `id`, not `file_name`.** Before this ticket a document's
    only address was the file name it was uploaded under, which is why
    re-uploading the same name silently replaced it (`store_document`'s old
    behaviour). Now that a Document belongs to a Knowledge Base rather than to
    a flat per-creator directory, two Documents on the same shelf may share a
    name — nothing downstream reads a name as an address any more, everything
    reads this row's id.

    `content_sha256` is unique **within one `kb_id`** (`uq_documents_kb_content`)
    and not globally: the same policy PDF attached to two different shelves is
    two legitimate Documents, one per shelf it actually informs, and only a
    second copy on the *same* shelf is the accidental double-upload this
    constraint exists to catch.

    The file itself still lives on disk — `src.workflow.documents` is what
    reads and writes it — because re-indexing (ADR-0044 §5) has to start from
    the original bytes, not from whatever `chunk_count` says about the last
    pass over them.
    """

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("kb_id", "content_sha256", name="uq_documents_kb_content"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    file_name: Mapped[str] = mapped_column(doc="Tên tệp gốc lúc tải lên — không còn là địa chỉ, chỉ để hiển thị")
    content_sha256: Mapped[str] = mapped_column(doc="Băm nội dung — dùng để chống tải trùng trong cùng một KB")
    byte_size: Mapped[int]
    status: Mapped[DocumentStatus] = mapped_column(default=DocumentStatus.PENDING)
    status_detail: Mapped[str | None] = mapped_column(default=None, doc="Vì sao đang ở trạng thái này, nhất là failed")
    chunk_count: Mapped[int] = mapped_column(default=0, doc="Số Passage đã sinh ra ở lần index gần nhất")
    low_confidence: Mapped[bool] = mapped_column(
        default=False,
        doc=(
            "ExtractionResult.flagged_for_review ở lần extract gần nhất (RAG-06) — "
            "trên 30% khối trích xuất ở mức 'low'. Đủ để màn hình upload (#98) cảnh báo "
            "'độ chính xác trích dẫn có thể giảm'; không tự chặn index."
        ),
    )
    uploaded_at: Mapped[datetime] = mapped_column(server_default=func.now())
    indexed_at: Mapped[datetime | None] = mapped_column(default=None, doc="Lúc index xong lần gần nhất; null nếu chưa")

    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="documents")


class IngestionCost(Base):
    """What one fresh (non-cached) extraction of a Document billed — see #101 / ADR-0009 §4.

    Not a row on `run_costs`. `RunCost.agent_id`/`version_id`/`conversation_id`
    are all `NOT NULL` because a run always has an agent, a version and a
    conversation behind it; ingesting a Document has none of the three.
    Reusing that table would mean making its own FKs nullable, which throws
    away the exact thing ADR-0009 built it to guarantee — *"which agent is
    costing us money"*. The two cost axes are different (`run_costs` is by
    agent version, this is by Knowledge Base / Document) and stay two tables
    for the same reason `run_costs` itself split off `sandbox_runs`
    (ADR-0009 §4).

    **One row per fresh extraction only.** `src.services.extraction.extract_document`
    calls `on_vlm_spend` at most once per document, and only when a cache miss
    actually sent pages to OpenRouter — a document whose bytes already sit in
    the extraction cache (a duplicate upload, the same content re-uploaded
    under a new name) writes no new row here, which is the ticket's own
    acceptance line: *"upload trùng file → không phát sinh dòng mới"*. A
    document read through the text-layer fast path writes none either: it
    never called OpenRouter, so there is nothing to record.

    Priced through `src.models.model_catalog.usd_cost` — the same table
    `run_costs` prices a run's model calls from, so ingest cost and run cost
    can never disagree about what a model bills.
    """

    __tablename__ = "ingestion_costs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Cascade on all three: a cost row is a statement about one Document on one
    # Knowledge Base owned by somebody, and once any of the three is gone there
    # is nobody left to bill — the same call `run_costs` makes for cascading on
    # `agent_id`/`version_id`.
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(
        doc="Model OpenRouter thực sự đã gọi lúc trích xuất — settings.extraction_vlm_model tại thời điểm chạy"
    )
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost_usd: Mapped[float] = mapped_column(doc="Quy ra tiền theo MODEL_CATALOG tại thời điểm ingest")
    page_count: Mapped[int] = mapped_column(doc="Số trang đã gửi qua VLM ở lần trích xuất này")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


#: One creator's Agent may search several Knowledge Bases, and one Knowledge
#: Base may inform several Agents — see #90's acceptance criterion. A plain
#: association table rather than a mapped class: the relationship carries no
#: data of its own (unlike `DeploymentAllowedUser`, which is a grant with a
#: `created_at`), and the ticket's own schema gives it nothing but the two
#: foreign keys and their composite primary key.
agent_kb_links = Table(
    "agent_kb_links",
    Base.metadata,
    Column("agent_id", Integer, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
    Column("kb_id", Integer, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True),
)


#: Ngưỡng thấp có chủ ý — xem issue #12: ngưỡng cao trên máy chấm dao động chỉ
#: tạo bực bội, không tạo chất lượng. Seeded by the first migration; shared here
#: so the migration and the test suite never disagree about the default.
DEFAULT_EVAL_PASS_THRESHOLD = 0.6


class SystemConfig(Base):
    """Admin-configured platform settings — one row. Seeded by the first migration.

    Defaults exist from this ticket so eval (#13) and the admin config screen
    (#12) can each land independently, per issue #3.
    """

    __tablename__ = "system_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    eval_pass_threshold: Mapped[float]
    model_allowlist: Mapped[list[str]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


def default_system_config_row() -> dict[str, object]:
    """The one `system_config` row a fresh schema seeds — shared by the migration and tests.

    `SEEDED_MODEL_ALLOWLIST` and **not** `DEFAULT_MODEL_ALLOWLIST`, and the two
    were the same list until they were told apart here. This row is the
    platform's *write policy* — what a creator may put on a new agent — while
    `DEFAULT_MODEL_ALLOWLIST` is what a stored spec is *read back* against when
    nobody passes a context. Seeding one from the other meant the policy could
    never be narrowed without retroactively invalidating every agent already
    built under the wider one.
    """
    return {
        "id": 1,
        "eval_pass_threshold": DEFAULT_EVAL_PASS_THRESHOLD,
        "model_allowlist": list(SEEDED_MODEL_ALLOWLIST),
    }


class EmbeddingUsage(Base):
    """One embedding call's token cost — the ledger behind ADR-0045 §8.

    No provider will tell you how much you have spent with it: Voyage publishes
    a free allowance per model and no endpoint to ask how much is left, OpenAI
    simply bills. So this table is the only place that can answer the question.
    One row per call rather than a running total in a single row: a counter
    answers "how much", while rows also answer "since when" and "how fast",
    which are the questions asked the moment the first answer is alarming.

    Not attached to a run, an agent, or a user, on purpose. Indexing a document
    is work done for a *library*, and the same chunks then serve every agent of
    that creator and every caller of those agents — filing the cost against
    whichever run happened to trigger the indexing would bill one conversation
    for a document all the others go on using.
    """

    __tablename__ = "embedding_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Which model spent them. An allowance is per model, so a total that did
    #: not distinguish them could not answer the only question this table
    #: exists for.
    model: Mapped[str] = mapped_column(index=True, doc="Model đã tiêu số token này")
    tokens: Mapped[int] = mapped_column(doc="Số token của một lần gọi, theo usage.total_tokens")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Conversation(Base):
    """One chat window a person can find again — the index the sidebar reads.

    **Not the transcript.** What was said lives where ADR-0004 put it: LangGraph's
    checkpoint tables, addressed by `ThreadRef.checkpoint_thread_id`. This row
    only answers the question those tables cannot — *which* conversations exist
    and whose they are. A checkpoint key is one opaque string; there is no
    supported way to ask LangGraph "every thread belonging to this person on this
    agent", and reverse-engineering one by pattern-matching its keys would couple
    the sidebar to a schema the library reserves the right to migrate.

    So the two are kept as index and content, and `conversation_id` is the join
    between them. It is stored rather than derived because the derivation runs
    the other way: `ThreadRef` builds a checkpoint key *from* this id, and the
    caller part of that key comes from `_thread_caller`, not from here.

    Keyed on `(agent_id, user_id)` because that is the only question the sidebar
    asks. `user_id` and not the `caller` string of ADR-0036 §6: a Deployment's
    API key is also a caller, and an integration has no sidebar to show. A
    conversation opened by a key therefore leaves no row here, which is correct —
    nobody can open it again.
    """

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Both CASCADE: this row is a way back into a conversation, and a deleted
    # agent or a deleted account leaves nothing to go back to.
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    #: The client-visible id — the last part of the checkpoint key, and the same
    #: string the chat stream announces in its `thread` event.
    conversation_id: Mapped[str] = mapped_column(doc="Thread trong checkpointer mà hàng này trỏ tới")
    #: `prod` and `trial` are different worlds (#6), and a row that did not say
    #: which one it belongs to would offer a creator's rehearsals to an end-user.
    namespace: Mapped[ThreadNamespace]
    #: What the sidebar shows. Seeded from the first message and editable after,
    #: so a conversation is findable by what it was about rather than by a hex id.
    title: Mapped[str] = mapped_column(doc="Nhãn hiển thị trên sidebar")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    #: Ordering key. Kept as its own column rather than read from the checkpoint:
    #: the sidebar sorts every conversation on every open, and doing that from
    #: LangGraph's tables would be one query per row.
    last_message_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        # One row per thread. The upsert on every turn relies on this: without
        # it, a conversation would gain a duplicate row per message and the
        # sidebar would list the same chat once for each thing said in it.
        UniqueConstraint("agent_id", "user_id", "conversation_id", "namespace", name="uq_conversations_thread"),
        # The whole read path: this person's conversations on this agent, newest
        # first. Composite because `agent_id` alone stops being selective the
        # moment an agent has more than one user.
        Index("ix_conversations_owner_recent", "agent_id", "user_id", "last_message_at"),
    )


class WorkbenchTurnRole(StrEnum):
    """Who spoke in one workbench turn — the creator, or the meta agent answering."""

    CREATOR = "creator"
    META_AGENT = "meta_agent"


class WorkbenchTurn(Base):
    """One utterance in the conversation between a creator and the meta agent — ADR-0050.

    Keyed to the **agent**, not the version: the conversation survives a version
    fork the way a chat about a document survives the document being saved-as.
    Each row still records `version_id` — which version was on the table when
    the words were said is part of what the words meant.

    `workflow_after` is the proposal that shipped with a meta-agent turn (null
    when the turn only explained or asked). Stored so a later turn — and a later
    investigator — can see not just what was said but what each saying *did*.
    `steps_json` is the meta agent's own tool trail for the turn, in the same
    spirit as `SandboxRun.transcript`: the evidence half of "the meta agent is
    an agent".
    """

    __tablename__ = "workbench_turns"

    id: Mapped[int] = mapped_column(primary_key=True)
    # CASCADE like `sandbox_runs`: a conversation about an agent says nothing
    # once that agent no longer exists.
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id", ondelete="CASCADE"))
    role: Mapped[WorkbenchTurnRole]
    message: Mapped[str] = mapped_column(doc="Lời của lượt này — câu người tạo gõ, hoặc loi_nhan của meta agent")
    question: Mapped[str | None] = mapped_column(default=None, doc="cau_hoi meta agent hỏi lại, nếu có")
    workflow_after: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=None, doc="Bản graph đề xuất đi kèm lượt này; null nếu lượt chỉ nói"
    )
    steps_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, doc="Vết tool call của meta agent trong lượt — bằng chứng của chính nó"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UserConnectionStatus(StrEnum):
    """Whether this person's OAuth link is usable right now.

    `NEEDS_RECONNECT` is a fact Google told us (`invalid_grant` on refresh —
    password change, revocation from the account screen, six months idle), kept
    on the row so the Connections screen can say "kết nối lại" *before* the next
    run fails instead of after.
    """

    CONNECTED = "connected"
    NEEDS_RECONNECT = "needs_reconnect"


class UserConnection(Base):
    """One person's OAuth link to an outside provider — the half ADR-0023 §8 deferred.

    The row a `CredentialKind.OAUTH_PER_USER` connection resolves through at run
    time: an `MCP` node wired to `gmail`/`google_drive` runs with the **agent
    owner's** account, found here by `(user_id, provider)`. One row per pair —
    reconnecting replaces the tokens, it does not add an account.

    `secret_ref` names the *refresh token* in `src.services.secret_store`,
    following `ExternalConnection.secret_ref`'s rule exactly: a key, never a
    value. Access tokens are not stored at all — they live for an hour and are
    minted on demand from the refresh token, so there is nothing here worth
    stealing that the secret store's encryption does not already cover.
    """

    __tablename__ = "user_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    # CASCADE like `ExternalConnection.owner_id`: an OAuth grant is meaningless
    # without the account that granted it.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(doc="Nhà cung cấp OAuth — hiện chỉ 'google'")
    account_email: Mapped[str] = mapped_column(
        default="", doc="Email tài khoản Google đã nối — để màn hình Connections nói rõ đang nối tài khoản nào"
    )
    scopes: Mapped[list[str]] = mapped_column(
        JSON, default=list, doc="Các scope Google thực cấp — đối chiếu khi thêm tool cần quyền mới"
    )
    secret_ref: Mapped[str] = mapped_column(doc="Khoá trong secret store nơi giữ token — không bao giờ là chính token")
    #: Which kind of token `secret_ref` points at, and so how long it lasts.
    #:
    #: `refresh_token` is the sign-in path: Google hands one over once and the
    #: platform mints hour-long access tokens from it forever. `access_token`
    #: is the paste path — the same shape Agent Builder's dialog takes, and
    #: honest about what it costs: there is nothing to refresh from, so the
    #: connection dies when Google expires it and the person pastes again.
    #: Kept as a column rather than inferred from the stored value because the
    #: two are indistinguishable strings, and guessing wrong means either a
    #: refresh call against an access token or a dead connection nobody can
    #: explain.
    token_kind: Mapped[str] = mapped_column(default="refresh_token", doc="'refresh_token' | 'access_token'")
    #: When a pasted access token stops working, as Google said at paste time —
    #: `None` for the sign-in path, whose token is minted fresh on demand.
    #:
    #: Without this the row had no way to know its own token was dead: a paste
    #: stayed `CONNECTED` forever, every screen said "đang nối", and the first
    #: sign of trouble was a 401 in the middle of somebody's run an hour later.
    #: Naive UTC, like every other timestamp on this table.
    expires_at: Mapped[datetime | None] = mapped_column(
        default=None, doc="Lúc access token dán tay hết hạn (UTC) — NULL khi đăng nhập Google, vì token đó tự làm mới"
    )
    status: Mapped[UserConnectionStatus] = mapped_column(default=UserConnectionStatus.CONNECTED)
    connected_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # One account per provider per person — reconnect updates, never duplicates.
        UniqueConstraint("user_id", "provider", name="uq_user_connections_owner_provider"),
    )
