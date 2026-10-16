"""Managing an agent's publication — see #15 / ADR-0013, widened by #50 / ADR-0029.

Seven endpoints over one row. `PUT /deployed-version` is ADR-0013's, unchanged in
meaning: the resource is *the pointer*, `PUT` replaces it, and rolling back is
not a second operation but a deploy whose target happens to be older — giving it
a route of its own would be two names for one write, with two places for the
rules to be stated and one of them to be stated wrongly.

The others are the ones V1 had no concept for, and they are separate endpoints
rather than one `PATCH` because they are separate decisions with separate
audiences of consequence: *which version* serves, *whether* it serves, *who* may
reach it, *how often* any one caller may call it (#51), and *what key* opens the
API door. A single partial-update body would let a request that meant to pause a
Deployment also widen it, by sending a field it did not think about.

The seventh reads rather than writes: `GET /deployment/operations` (#55) is what
the publication has *done*, assembled from the run ledger ADR-0009 §4 already
keeps. It sits here rather than beside the cost endpoints because the question is
about the Deployment — what an agent is serving — and the owner-only rule below
is the rule it needs.

No gate is re-checked here. `publish` refuses anything that is not an `approved`
version of this very agent — through `refuse_undeployable`, which belongs to the
version lifecycle — and this module turns that refusal into a 409 carrying its
sentence. A route that also checked the status would be the second statement of a
rule that already has one, and the day the rule changes is the day the screen and
the button start disagreeing.

Everything here is `OwnedAgent`. Deciding what an agent serves, and to whom, is
the owner's alone however many people are using it — admins reach other people's
work through the approval surface (#14), where their access is deliberate and
audited, not by being admins here.
"""

from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.versions import VersionLifecycleError
from src.api.agent_routes import OwnedAgent, get_agent_version_or_404
from src.auth.dependencies import CurrentUser
from src.db.base import get_db
from src.db.models import Agent, Deployment, User
from src.deployments.operations import operations_of
from src.deployments.publication import (
    deployment_of,
    publish,
    regenerate_api_key,
    set_access,
    set_rate_limit,
    set_status,
)
from src.models.schemas import (
    AllowedUserResponse,
    DeploymentAccessRequest,
    DeploymentApiKeyResponse,
    DeploymentExecutionResponse,
    DeploymentOperationsResponse,
    DeploymentRateLimitRequest,
    DeploymentResponse,
    DeploymentStatusRequest,
    DeployVersionRequest,
    PublishedDeploymentResponse,
)

router = APIRouter(prefix="/agents", tags=["deployment"])


def _fields(deployment: Deployment, db: Session) -> dict[str, Any]:
    """One Deployment's fields, with its allowlist resolved to people.

    A dict rather than a built response, because two shapes are built from it:
    the settings read, and the publish that also carries a freshly minted key. A
    `PublishedDeploymentResponse` assembled by dumping a `DeploymentResponse`
    back apart would be building the same object twice to add one field.

    The emails are joined in rather than read off each `DeploymentAllowedUser`
    row's relationship: an allowlist is short, but a lazy load per member is a
    query per member on a screen whose whole purpose is to show the list.
    """
    return {
        "id": deployment.id,
        "agent_id": deployment.agent_id,
        "agent_version_id": deployment.agent_version_id,
        "status": deployment.status,
        "access_mode": deployment.access_mode,
        "allowed_users": _allowed_users(deployment, db),
        "rate_limit_per_user": deployment.rate_limit_per_user,
        "created_by": deployment.created_by,
        "created_at": deployment.created_at,
        "updated_at": deployment.updated_at,
    }


def _response(deployment: Deployment, db: Session) -> DeploymentResponse:
    """One Deployment on the wire — what every endpoint here but the key answers with."""
    return DeploymentResponse(**_fields(deployment, db))


def _allowed_users(deployment: Deployment, db: Session) -> list[AllowedUserResponse]:
    """Everyone on `deployment`'s allowlist, by email, in a stable order."""
    if not deployment.allowed_users:
        return []
    rows = db.execute(
        select(User.id, User.email)
        .where(User.id.in_([member.user_id for member in deployment.allowed_users]))
        .order_by(User.email)
    ).all()
    return [AllowedUserResponse(id=user_id, email=email) for user_id, email in rows]


def _deployment_or_404(db: Session, agent: Agent) -> Deployment:
    """`agent`'s Deployment, or a 404 saying it has never been published.

    404 and not 409, unlike the chat door's "no version deployed": there is no
    row here to act on, and every one of these endpoints addresses that row. The
    caller owns the agent, so nothing is being hidden from them — they are being
    told the thing they named does not exist yet.
    """
    deployment = deployment_of(db, agent)
    if deployment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent chưa được publish — deploy một version đã duyệt để tạo Deployment",
        )
    return deployment


@router.put("/{agent_id}/deployed-version", response_model=PublishedDeploymentResponse)
def deploy_version(
    agent: OwnedAgent,
    user: CurrentUser,
    payload: DeployVersionRequest,
    db: Annotated[Session, Depends(get_db)],
) -> PublishedDeploymentResponse:
    """Publish one of this agent's approved versions — a deploy, or a rollback.

    The first call creates the Deployment; every one after it moves the same
    row's pointer (ADR-0029 §1). Neither `status` nor `access_mode` is touched by
    a re-publish: choosing a version is not also a decision to un-pause or widen,
    and one button that quietly did three things is one nobody would dare press
    on a live agent.

    The version is resolved through the agent in the path, so naming another
    agent's version is a 404 rather than a 409: a version is addressed *through*
    the agent that owns it, and answering "that one is not approved" about
    somebody else's row would say more than the caller is entitled to know. What
    is left for `publish` to refuse is the status, and that refusal is the
    ticket's "deploy một version chưa duyệt bị từ chối" — inherited, not
    re-implemented.

    The response carries `api_key` exactly once, on the call that created the
    Deployment. That is the only moment the plaintext exists anywhere, so a
    creator publishing for the first time is also the moment they are handed
    their integration credential — and losing it means regenerating, not looking
    it up.
    """
    version = get_agent_version_or_404(db, agent, payload.version_id)
    try:
        published = publish(db, agent, version, publisher=user)
    except VersionLifecycleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return PublishedDeploymentResponse(**_fields(published.deployment, db), api_key=published.api_key)


@router.get("/{agent_id}/deployment", response_model=DeploymentResponse)
def read_my_agents_deployment(agent: OwnedAgent, db: Annotated[Session, Depends(get_db)]) -> DeploymentResponse:
    """This agent's publication and every setting on it — owner only.

    Not on `AgentResponse`, where `is_deployed` and `deployed_version_id` already
    live. Those two are what a *list* of agents needs to offer the right link;
    this is a screen's worth of settings that only their owner may see, and
    folding them into the shape strangers also read would put an access list on
    an endpoint that strangers can reach.
    """
    return _response(_deployment_or_404(db, agent), db)


@router.get("/{agent_id}/deployment/operations", response_model=DeploymentOperationsResponse)
def read_my_agents_deployment_operations(
    agent: OwnedAgent, db: Annotated[Session, Depends(get_db)]
) -> DeploymentOperationsResponse:
    """How this Deployment has been serving: runs, failures, spend, and the last few executions — see #55.

    Read off `run_costs` (`src.deployments.operations`), which ADR-0009 §4 already
    made the durable record every run leaves behind. Nothing here instruments
    anything: ADR-0006 refused a second execution-tracking mechanism beside the
    run's own event stream, and a dashboard that started writing its own rows
    would be a third account of one run with no way to settle a disagreement.

    Owner-only, like every other endpoint in this module. Being allowed to *talk*
    to a published agent is not being allowed to see how often it is talked to,
    what that costs its owner, or which conversations it has had — the same line
    ADR-0036 §6 draws around an owner reading threads.

    Zeroes rather than a 404 for a Deployment nobody has used yet. The 404 here is
    the one every endpoint in this module gives: no Deployment at all.
    """
    deployment = _deployment_or_404(db, agent)
    board = operations_of(db, deployment)
    return DeploymentOperationsResponse(
        deployment_id=deployment.id,
        agent_id=deployment.agent_id,
        agent_version_id=deployment.agent_version_id,
        runs=board.runs,
        errors=board.errors,
        tokens=board.tokens,
        cost_usd=board.cost_usd,
        recent_executions=[
            DeploymentExecutionResponse(
                id=execution.id,
                version_id=execution.version_id,
                conversation_id=execution.conversation_id,
                tokens=execution.tokens,
                cost_usd=execution.cost_usd,
                latency_seconds=execution.latency_seconds,
                error=execution.error,
                at=execution.at,
            )
            for execution in board.recent
        ],
    )


@router.put("/{agent_id}/deployment/status", response_model=DeploymentResponse)
def set_my_agents_deployment_status(
    agent: OwnedAgent,
    payload: DeploymentStatusRequest,
    db: Annotated[Session, Depends(get_db)],
) -> DeploymentResponse:
    """Pause this Deployment, or start it serving again — ADR-0029 §2's unpublish path.

    The version stays exactly where it was pointed, which is the whole reason
    `status` is a field instead of a nullable pointer: coming back is this one
    call, not "and now choose a version again". A paused Deployment is a 404 to
    everyone but its owner, so `disabled` is as close to unpublished as anything
    in this platform gets — without losing what was published.
    """
    return _response(set_status(db, _deployment_or_404(db, agent), payload.status), db)


@router.put("/{agent_id}/deployment/access", response_model=DeploymentResponse)
def set_my_agents_deployment_access(
    agent: OwnedAgent,
    payload: DeploymentAccessRequest,
    db: Annotated[Session, Depends(get_db)],
) -> DeploymentResponse:
    """Say who may reach this Deployment: the owner alone, or the owner plus a named list.

    The list is resolved from emails here rather than in the service layer,
    because "which account is this address" is a question about the HTTP request
    — a typo is a 422 the caller fixes and sends again, not a domain rule. Every
    address is resolved before anything is written, so a list with one bad
    address changes nothing at all: a partial write would leave the owner
    believing they had granted access they had not.
    """
    return _response(
        set_access(
            db, _deployment_or_404(db, agent), payload.access_mode, _users_by_email(db, payload.allowed_user_emails)
        ),
        db,
    )


@router.put("/{agent_id}/deployment/rate-limit", response_model=DeploymentResponse)
def set_my_agents_deployment_rate_limit(
    agent: OwnedAgent,
    payload: DeploymentRateLimitRequest,
    db: Annotated[Session, Depends(get_db)],
) -> DeploymentResponse:
    """Set how many requests each caller may make per minute — see #51 / ADR-0037 §6.

    Per *caller*, not for the Deployment as a whole: on an agent shared with a
    list of people, one number for everybody would let the heaviest caller spend
    the quota the others were promised (ADR-0029 §6).

    Takes effect on the very next request, since the counters read the column
    rather than a copy of it. Lowering it does not retroactively refuse calls
    already made — a caller who is over the new number simply waits out the
    window they are already in.
    """
    return _response(set_rate_limit(db, _deployment_or_404(db, agent), payload.rate_limit_per_user), db)


@router.post("/{agent_id}/deployment/api-key", response_model=DeploymentApiKeyResponse)
def regenerate_my_agents_deployment_api_key(
    agent: OwnedAgent, db: Annotated[Session, Depends(get_db)]
) -> DeploymentApiKeyResponse:
    """Mint a new API key for this Deployment. The old one stops working immediately.

    `POST` and not `PUT`: the caller is not supplying a value, they are asking
    for one to be created, and the response is the only copy — nothing stores the
    plaintext (`src.deployments.keys`).

    One endpoint for two needs that turn out to be the same need. "I lost my key"
    and "my key leaked" both end here, because a hash cannot be un-hashed: there
    is no reading a key back, only replacing it. That is also why this is the
    honest answer to "let me view my key" — what an owner can have is a key that
    works, not the one they were given last month.
    """
    return DeploymentApiKeyResponse(api_key=regenerate_api_key(db, _deployment_or_404(db, agent)))


def _users_by_email(db: Session, emails: Sequence[str]) -> list[User]:
    """The accounts behind `emails`, or a 422 naming the ones that do not exist.

    Refusing by name rather than dropping the unknown ones silently: an owner who
    mistypes a colleague's address should be told, not left with a shorter
    allowlist than the one they think they wrote.
    """
    wanted = [email.strip() for email in emails if email.strip()]
    if not wanted:
        return []
    found = list(db.execute(select(User).where(User.email.in_(wanted))).scalars())
    missing = sorted(set(wanted) - {user.email for user in found})
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Không tìm thấy tài khoản: {', '.join(missing)}",
        )
    return found
