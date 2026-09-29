"""Endpoints only an admin may call.

The first of them is the cost board (#11): every agent on the platform, dearest
first. It lives here rather than under `/agents` because it deliberately crosses
ownership — `src.api.agent_routes` is scoped to the caller's own agents by
construction, and the whole value of this view is that it is not.

That crossing is the reason it is admin-only. Knowing what another creator's
agent spends is a governance question, and the answer belongs to the person who
has to pay for it.

The config screen (#12) sits beside it for the same reason: the eval threshold
and the model allowlist govern every creator's agents at once, so they belong to
whoever answers for the platform rather than to whoever built the agent. #14's
approval queue joins them here.
"""

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.admin.config import ConfigValidationError, PlatformConfig, read_config, update_config
from src.agents.spend import agent_spend
from src.api.workflow_routes import template_submission_response, tool_registration_response
from src.auth.dependencies import AdminUser
from src.config import get_settings
from src.db.base import get_db
from src.db.models import Agent, TemplateSubmission, ToolRegistration, User, UserStatus
from src.models.model_catalog import ASSIGNABLE_MODELS
from src.models.schemas import (
    ActivateUserResponse,
    AdminUserResponse,
    AgentCostBoardEntry,
    ApprovalReasonRequest,
    AssignableModelResponse,
    EmbeddingModelUsageResponse,
    EmbeddingUsageResponse,
    IngestionSpendEntryResponse,
    PlatformConfigResponse,
    PlatformConfigUpdateRequest,
    SecretRefResponse,
    SecretValueRequest,
    TemplateSubmissionResponse,
    ToolRegistrationResponse,
    ToolRiskOverrideRequest,
)
from src.services.ingestion_costs import GroupBy, ingestion_spend
from src.services.secret_store import SecretNotFoundError, SecretStore, get_secret_store
from src.workflow.embedding_budget import usage_report
from src.workflow.registrations import (
    ToolRegistrationError,
    approve_registration,
    disable_registration,
    override_risk,
    pending_registrations,
    reject_registration,
)
from src.workflow.submissions import (
    TemplateSubmissionError,
    approve_submission,
    disable_submission,
    pending_submissions,
    reject_submission,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/costs", response_model=list[AgentCostBoardEntry])
def read_agent_cost_board(_admin: AdminUser, db: Annotated[Session, Depends(get_db)]) -> list[AgentCostBoardEntry]:
    """Accumulated actual spend per agent, dearest first — "agent nào đang tốn tiền".

    Only agents that have actually run appear. An agent nobody has run has spent
    nothing, and a page of zeroes is a page an admin has to read past to reach
    the four rows that matter.

    Names are joined on afterwards rather than in the aggregate query: the
    aggregation is over `run_costs` and reads no other table, which is what keeps
    it one indexed scan however many agents exist.
    """
    spend = agent_spend(db)
    names = _names_of(db, [entry.agent_id for entry in spend])
    return [
        AgentCostBoardEntry(
            agent_id=entry.agent_id,
            name=names.get(entry.agent_id, ""),
            runs=entry.runs,
            tokens=entry.tokens,
            usd=entry.usd,
            trial_usd=entry.trial_usd,
            production_usd=entry.production_usd,
        )
        for entry in spend
    ]


@router.get("/ingestion-costs", response_model=list[IngestionSpendEntryResponse])
def read_ingestion_cost_board(
    _admin: AdminUser,
    db: Annotated[Session, Depends(get_db)],
    group_by: Annotated[
        GroupBy, Query(description="Trục tổng hợp: theo ngày, theo tháng, theo owner, hoặc theo Knowledge Base")
    ] = "day",
) -> list[IngestionSpendEntryResponse]:
    """Ingest spend totalled along one axis — see #101.

    Same crossing-ownership reasoning as `/costs`: this is money spent across
    every owner's Knowledge Bases, and only the person who answers for the
    platform's OpenRouter bill has a reason to see it grouped that way.
    """
    return [
        IngestionSpendEntryResponse(
            key=entry.key,
            document_count=entry.document_count,
            page_count=entry.page_count,
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            usd=entry.usd,
        )
        for entry in ingestion_spend(db, group_by=group_by)
    ]


def _names_of(db: Session, agent_ids: Sequence[int]) -> dict[int, str]:
    """The names of `agent_ids`, in one query."""
    if not agent_ids:
        return {}
    rows = db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids))).all()
    return {agent_id: name for agent_id, name in rows}


@router.get("/config", response_model=PlatformConfigResponse)
def read_platform_config(_admin: AdminUser, db: Annotated[Session, Depends(get_db)]) -> PlatformConfigResponse:
    """The eval threshold and the model allowlist, plus what the allowlist may contain.

    Behind `AdminUser` like everything in this module: these two settings decide
    how good every agent on the platform has to be and what it may be built out
    of, so a creator has no business reading them, let alone changing them.
    """
    return _config_response(read_config(db))


@router.patch("/config", response_model=PlatformConfigResponse)
def change_platform_config(
    _admin: AdminUser,
    payload: PlatformConfigUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
) -> PlatformConfigResponse:
    """Change either setting. Omit a field to leave it as it is.

    A value the platform refuses comes back as a 422 carrying the reason from
    `src.admin.config` — which model was not vetted, why an empty allowlist is
    not a policy — rather than a bare status. The screen renders that sentence,
    so "bị từ chối kèm giải thích" (#12) is one message the whole way out.
    """
    try:
        config = update_config(
            db,
            eval_pass_threshold=payload.eval_pass_threshold,
            model_allowlist=payload.model_allowlist,
        )
    except ConfigValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return _config_response(config)


def _config_response(config: PlatformConfig) -> PlatformConfigResponse:
    """One configuration, plus the catalog the allowlist is chosen from.

    The catalog is code, not configuration, so it is attached on the way out
    rather than stored: it can only ever be what this build vetted.
    """
    return PlatformConfigResponse(
        eval_pass_threshold=config.eval_pass_threshold,
        model_allowlist=list(config.model_allowlist),
        available_models=[
            AssignableModelResponse(name=name, tier=info.tier) for name, info in ASSIGNABLE_MODELS.items()
        ],
        updated_at=config.updated_at,
    )


# --------------------------------------------------------------------------- #
# The template review queue — #53, ADR-0025 §6-8
# --------------------------------------------------------------------------- #


def get_template_submission_under_review(
    template_id: int, db: Annotated[Session, Depends(get_db)]
) -> TemplateSubmission:
    """Any submission at all, by id — the crossing-ownership seam `get_version_under_review` also is.

    404 rather than 403 for the same reason: reaching across creators is what
    this whole module is for, and there is no ownership check to fail here.
    """
    row = db.get(TemplateSubmission, template_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy template")
    return row


TemplateSubmissionUnderReview = Annotated[TemplateSubmission, Depends(get_template_submission_under_review)]


def _template_refused(exc: TemplateSubmissionError) -> HTTPException:
    """A stage refusal on the wire — 409, the sentence carried through untouched.

    Same reasoning `approval_routes._refused` gives: nothing is wrong with the
    request, the submission is simply not in the state the action needs it in.
    """
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/templates", response_model=list[TemplateSubmissionResponse])
def read_template_review_queue(
    _admin: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> list[TemplateSubmissionResponse]:
    """Every creator-submitted template waiting on a person, oldest first — criterion 4.

    `pending` only. An admin reviewing what to approve next has no use for the
    already-decided rows here — those are read back through the creator's own
    submission response, the same split `read_approval_queue` draws around
    `pending_approval` versions.
    """
    return [template_submission_response(row) for row in pending_submissions(db)]


@router.post("/templates/{template_id}/approve", response_model=TemplateSubmissionResponse)
def approve_template_submission(
    submission: TemplateSubmissionUnderReview, admin: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> TemplateSubmissionResponse:
    """`pending -> approved`. From this moment the Workflow Generator may propose it — criterion 5."""
    try:
        approved = approve_submission(db, submission, admin_id=admin.id)
    except TemplateSubmissionError as exc:
        raise _template_refused(exc) from None
    return template_submission_response(approved)


@router.post("/templates/{template_id}/reject", response_model=TemplateSubmissionResponse)
def reject_template_submission(
    submission: TemplateSubmissionUnderReview,
    admin: AdminUser,
    payload: ApprovalReasonRequest,
    db: Annotated[Session, Depends(get_db)],
) -> TemplateSubmissionResponse:
    """`pending -> rejected`, with a reason mandatory for the same reason a version's is."""
    try:
        rejected = reject_submission(db, submission, admin_id=admin.id, reason=payload.reason)
    except TemplateSubmissionError as exc:
        raise _template_refused(exc) from None
    return template_submission_response(rejected)


@router.post("/templates/{template_id}/disable", response_model=TemplateSubmissionResponse)
def disable_template_submission(
    submission: TemplateSubmissionUnderReview, admin: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> TemplateSubmissionResponse:
    """`approved -> disabled`. Removes it from future selection; touches no `WorkflowJSON` already produced.

    ADR-0025's Consequences make this the whole point of copying a template's
    graph rather than referencing it: a workflow the Generator already built from
    this row while it was `approved` was built from a literal copy, so nothing
    about it changes when the row this returns says `disabled`.
    """
    try:
        disabled = disable_submission(db, submission, admin_id=admin.id)
    except TemplateSubmissionError as exc:
        raise _template_refused(exc) from None
    return template_submission_response(disabled)


# --------------------------------------------------------------------------- #
# The tool registration review queue — #52, ADR-0023 §7, §9
# --------------------------------------------------------------------------- #


def get_tool_registration_under_review(
    registration_id: int, db: Annotated[Session, Depends(get_db)]
) -> ToolRegistration:
    """Any registration at all, by id — the same crossing-ownership seam its template sibling is."""
    row = db.get(ToolRegistration, registration_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy kết nối")
    return row


ToolRegistrationUnderReview = Annotated[ToolRegistration, Depends(get_tool_registration_under_review)]


def _tool_refused(exc: ToolRegistrationError) -> HTTPException:
    """A refusal on the wire — 409, the sentence carried through untouched.

    Including "you have not supplied the mocks yet", which is a 409 rather than
    a 422 for `_template_refused`'s reason: nothing is wrong with the admin's
    request, the connection is simply not yet in a state that can be approved.
    """
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/tools", response_model=list[ToolRegistrationResponse])
def read_tool_review_queue(
    _admin: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> list[ToolRegistrationResponse]:
    """Every creator-registered connection waiting on a person, oldest first — criterion 3.

    Each row carries the introspected `actions[]` with the `risk_level` the
    system computed for each — which is the whole of what there is to review.
    The admin is not being asked to check a schema somebody typed; they are
    being asked whether this connection belongs on the platform and whether the
    computed risk is right.
    """
    return [tool_registration_response(row) for row in pending_registrations(db)]


@router.post("/tools/{registration_id}/risk", response_model=ToolRegistrationResponse)
def override_tool_action_risk(
    registration: ToolRegistrationUnderReview,
    admin: AdminUser,
    payload: ToolRiskOverrideRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ToolRegistrationResponse:
    """Re-grade one action's risk — criterion 4, and the only edit a review may make.

    Note what is missing from this router: any endpoint that writes `actions`.
    ADR-0023 §7 lets an admin override the risk and not the introspected schema,
    and the way that holds is that there is nowhere to send a schema to.

    The override reaches everything derived from risk — whether a sandbox mocks
    the action, whether the graph validator demands a Human Approval node in
    front of it — because those are read off `risk_level` rather than stored
    beside it (ADR-0023 §4-5).
    """
    try:
        updated = override_risk(
            db, registration, admin_id=admin.id, action_name=payload.action, risk=payload.risk_level
        )
    except ToolRegistrationError as exc:
        raise _tool_refused(exc) from None
    return tool_registration_response(updated)


@router.post("/tools/{registration_id}/approve", response_model=ToolRegistrationResponse)
def approve_tool_registration(
    registration: ToolRegistrationUnderReview, admin: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> ToolRegistrationResponse:
    """`pending -> approved`. From this moment an `MCP` node may name this connection — criterion 5."""
    try:
        approved = approve_registration(db, registration, admin_id=admin.id)
    except ToolRegistrationError as exc:
        raise _tool_refused(exc) from None
    return tool_registration_response(approved)


@router.post("/tools/{registration_id}/reject", response_model=ToolRegistrationResponse)
def reject_tool_registration(
    registration: ToolRegistrationUnderReview,
    admin: AdminUser,
    payload: ApprovalReasonRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ToolRegistrationResponse:
    """`pending -> rejected`, with a reason its creator will read. Never becomes selectable."""
    try:
        rejected = reject_registration(db, registration, admin_id=admin.id, reason=payload.reason)
    except ToolRegistrationError as exc:
        raise _tool_refused(exc) from None
    return tool_registration_response(rejected)


@router.post("/tools/{registration_id}/disable", response_model=ToolRegistrationResponse)
def disable_tool_registration(
    registration: ToolRegistrationUnderReview, admin: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> ToolRegistrationResponse:
    """`approved -> disabled`. Blocks new runs from invoking it; touches no saved workflow — criterion 6.

    ADR-0023 §9's whole point. A workflow built against this connection keeps its
    `tool_id`, keeps loading on the canvas and keeps saving; what changes is that
    the runtime finds `status: disabled` immediately before invoking and records
    a controlled execution error in the trace instead. Re-validating every draft
    against an admin action their creators do not control is the alternative the
    ADR rejected.
    """
    try:
        disabled = disable_registration(db, registration, admin_id=admin.id)
    except ToolRegistrationError as exc:
        raise _tool_refused(exc) from None
    return tool_registration_response(disabled)


@router.get("/embedding-usage", response_model=EmbeddingUsageResponse)
def read_embedding_usage(_: AdminUser, db: Annotated[Session, Depends(get_db)]) -> EmbeddingUsageResponse:
    """How much of each embedding model's allowance has been spent.

    No provider offers such an endpoint (ADR-0045 §8), so this reads the
    platform's own ledger — one row per embedding call — and reports it against
    the configured ceiling.

    Admin-only for the reason the cost board is: it is a question about what the
    platform is spending, not about any one creator's agent. It is also the
    screen that answers "why did answers get worse" — `active_model: null` means
    every allowance is gone and search has fallen back to keyword ranking.
    """
    settings = get_settings()
    models = settings.embedding_models
    report = usage_report(db, models, allowance=settings.embedding_token_allowance)
    active = next((spend.model for spend in report if not spend.exhausted), None)
    return EmbeddingUsageResponse(
        active_model=active,
        models=[
            EmbeddingModelUsageResponse(
                model_name=spend.model,
                tokens=spend.tokens,
                allowance=spend.allowance,
                remaining=spend.remaining,
                used_percent=round(spend.used_fraction * 100, 2),
                exhausted=spend.exhausted,
            )
            for spend in report
        ],
    )


# --------------------------------------------------------------------------- #
# The secret store — RAG-19 (#105), ADR-0045
# --------------------------------------------------------------------------- #
#
# Admin-only, `require_admin`'s reason for every other route in this file:
# putting a value under a `secret_ref` a creator's registration named is a
# governance action, not something the creator who typed the reference gets to
# do themselves. Every response here is `SecretRefResponse` — the key, never
# the value — which is what makes "no response path can return a secret"
# (ADR-0045's own acceptance line) true of this router by construction rather
# than by care taken per endpoint.


@router.put("/secrets/{key:path}", response_model=SecretRefResponse)
def set_secret(
    key: str,
    payload: SecretValueRequest,
    _admin: AdminUser,
    store: Annotated[SecretStore, Depends(get_secret_store)],
) -> SecretRefResponse:
    """Create `key`, or overwrite it if it already names a value.

    The one endpoint a creator's `secret_ref` needs something to point at
    before their `SECRET_REFERENCE` connection can run — everything ADR-0045
    unblocks is an admin having called this once.
    """
    store.put(key, payload.value)
    return SecretRefResponse(key=key)


@router.post("/secrets/{key:path}/rotate", response_model=SecretRefResponse)
def rotate_secret(
    key: str,
    payload: SecretValueRequest,
    _admin: AdminUser,
    store: Annotated[SecretStore, Depends(get_secret_store)],
) -> SecretRefResponse:
    """Replace the value `key` already names — the ticket's "rotate without deleting the connection".

    404 rather than silently creating `key`, for `EncryptedDbSecretStore.rotate`'s
    reason: an admin who reaches for rotate on a key nobody `put` yet almost
    always meant `put`, and creating one quietly would hide that mistake.
    """
    try:
        store.rotate(key, payload.value)
    except SecretNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không có khoá {key!r} trong secret store — dùng PUT để tạo mới trước khi rotate",
        ) from None
    return SecretRefResponse(key=key)


@router.delete("/secrets/{key:path}", status_code=status.HTTP_204_NO_CONTENT)
def delete_secret(key: str, _admin: AdminUser, store: Annotated[SecretStore, Depends(get_secret_store)]) -> None:
    """Remove `key`. Idempotent — deleting a key twice, or one nobody ever `put`, is not an error."""
    store.delete(key)


# --------------------------------------------------------------------------- #
# UC1-01 (#117) — activating a pending account. FR-03: every account after
# the installation's first starts `pending` and is unusable until this fires.
# --------------------------------------------------------------------------- #


@router.get("/users", response_model=list[AdminUserResponse])
def read_user_directory(
    _admin: AdminUser,
    db: Annotated[Session, Depends(get_db)],
    status_filter: Annotated[
        UserStatus | None,
        Query(alias="status", description="Lọc theo trạng thái; bỏ trống = mọi tài khoản"),
    ] = None,
) -> Sequence[User]:
    """Accounts on the platform, oldest first — `?status=pending` is the activation queue.

    The endpoint `_PENDING_ACCOUNT` sends people to. A refused login tells the
    person to "liên hệ agent_admin", and until this existed the admin they
    contacted had nowhere to look: `POST /users/{id}/activate` below needs a
    `user_id`, and nothing handed one out. Reading the queue and acting on it
    are the two halves of one job, which is why they sit together here rather
    than beside `GET /auth/users` (ADR-0002's, unfiltered, still serving its
    own tests).

    Oldest first because that is the order the work should be done in — the
    account that has been locked out longest is the one to look at next. An
    unfiltered call is the honest default for a directory; the queue is the
    *filtered* case, and asking for it explicitly is what keeps a screen that
    wants pending accounts from silently rendering everybody.
    """
    query = select(User).order_by(User.created_at, User.id)
    if status_filter is not None:
        query = query.where(User.status == status_filter)
    return db.execute(query).scalars().all()


@router.post("/users/{user_id}/activate", response_model=ActivateUserResponse)
def activate_user(user_id: int, _admin: AdminUser, db: Annotated[Session, Depends(get_db)]) -> User:
    """Move `user_id` from `pending` to `active` — the only door into the platform for account #2 onward.

    Idempotent on an already-active account rather than an error: an admin
    who double-clicks activate, or activates an account a second admin just
    activated, gets the same account back in the same state, not a 4xx to
    puzzle over.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy tài khoản")
    if user.status != UserStatus.ACTIVE:
        user.status = UserStatus.ACTIVE
        db.commit()
        db.refresh(user)
    return user
