"""Publishing an agent, and everything that follows from having published it — #50 / ADR-0029.

The subject is one row per agent: which approved version it serves, whether it is
serving at all, who may reach it, and the key that opens its API door. This
module is where those four questions are answered; like `src.agents.versions` it
has no HTTP surface of its own, so the rules cannot be reached around by adding
a route.

Three of the four are the ones V1 had nowhere to put. The fourth — *which
version* — is ADR-0013's pointer, unchanged in behaviour and moved in storage:
`publish` is still one write that replaces one value, so deploying and rolling
back remain the same operation with two words on two buttons.

The gate on that write stays in `src.agents.versions` (`refuse_undeployable`)
rather than being restated here. Only an `approved` version of *this* agent may
ever be pointed at, and that is a fact about the version lifecycle — the day it
changes, it should change in the module that owns the lifecycle and be inherited
here, exactly as ADR-0012's gates are inherited by the approval routes.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.orm import Session

from src.agents.versions import refuse_undeployable
from src.db.models import (
    Agent,
    AgentVersion,
    Deployment,
    DeploymentAccessMode,
    DeploymentAllowedUser,
    DeploymentStatus,
    User,
)
from src.deployments.keys import generate_api_key, hash_api_key, verify_api_key


@dataclass(frozen=True, slots=True)
class PublishedDeployment:
    """What a publish produced: the row, and the key if this call is what minted it.

    `api_key` is plaintext and is `None` on every publish but the first, which is
    the whole shape of the credential (`src.deployments.keys`): the key exists in
    the response that created it and nowhere else. A field that was always filled
    would mean the platform could still read it back — and then it would have to
    be stored.
    """

    deployment: Deployment
    api_key: str | None


def deployment_of(db: Session, agent: Agent) -> Deployment | None:
    """`agent`'s publication, or `None` if nobody has published it.

    Through the session rather than off `agent.deployment` so a caller that has
    just written one sees it, and so the relationship's loading strategy is this
    module's business rather than every reader's.
    """
    return db.execute(select(Deployment).where(Deployment.agent_id == agent.id)).scalar_one_or_none()


def publish(db: Session, agent: Agent, version: AgentVersion, *, publisher: User) -> PublishedDeployment:
    """Point `agent`'s Deployment at `version`, creating the Deployment if this is the first time.

    One row per agent, forever: a second publish moves `agent_version_id` on the
    row that already exists (ADR-0029 §1). That is why this is not "create a
    deployment" — there is nothing to create the second time, and a history of
    publications is already in the run trace, which records the version that
    served every single run (ADR-0029 §7).

    A first publish is `active` and `private` (ADR-0036 §2), which is stricter
    than V1's "deployed means anyone signed in may chat". The narrow default is deliberate:
    `private` is the mode V1 never had, and an owner who wants an audience says so
    (`set_access`) rather than discovering they already had one.

    `status` and `access_mode` are left exactly as they were on a re-publish.
    Deploying a new version is a statement about *which* version, and silently
    re-enabling a Deployment its owner had paused — or reopening one they had
    narrowed — would make one button do two things.
    """
    refuse_undeployable(agent, version)
    deployment = deployment_of(db, agent)
    if deployment is not None:
        deployment.agent_version_id = version.id
        return PublishedDeployment(deployment=_persist(db, deployment), api_key=None)

    api_key = generate_api_key()
    deployment = Deployment(
        agent_id=agent.id,
        agent_version_id=version.id,
        status=DeploymentStatus.ACTIVE,
        access_mode=DeploymentAccessMode.PRIVATE,
        api_key_hash=hash_api_key(api_key),
        created_by=publisher.id,
    )
    db.add(deployment)
    return PublishedDeployment(deployment=_persist(db, deployment), api_key=api_key)


def set_status(db: Session, deployment: Deployment, status: DeploymentStatus) -> Deployment:
    """Turn serving on or off, without touching which version is published.

    The unpublish path ADR-0013 left open, and the reason it is a separate field
    rather than a nullable pointer (ADR-0029 §2): a paused Deployment still knows
    what it was running, so starting again is this one write and not "now choose
    a version again".
    """
    deployment.status = status
    return _persist(db, deployment)


def set_access(
    db: Session, deployment: Deployment, access_mode: DeploymentAccessMode, allowed_users: Sequence[User]
) -> Deployment:
    """Say who may reach this Deployment, and — for `selected_users` — exactly who.

    The allowlist is replaced wholesale rather than added to, because the screen
    that sends it is showing the whole list: a partial write would make removing
    somebody a different request from adding somebody, and one of the two would
    eventually be missing.

    Names given alongside `private` are still stored. Switching to `private` for
    an afternoon is a common thing to do, and a mode change that also discarded
    the list would make it an expensive one.
    """
    deployment.access_mode = access_mode
    wanted = {user.id for user in allowed_users}
    deployment.allowed_users = [existing for existing in deployment.allowed_users if existing.user_id in wanted] + [
        DeploymentAllowedUser(user_id=user_id)
        for user_id in sorted(wanted - {existing.user_id for existing in deployment.allowed_users})
    ]
    return _persist(db, deployment)


def set_rate_limit(db: Session, deployment: Deployment, rate_limit_per_user: int) -> Deployment:
    """Say how many requests one caller may make per minute — see #51 / ADR-0037.

    Only the number is written; the counting happens in
    `src.deployments.rate_limit` and reads this column on every call, so a change
    takes effect on the next request rather than on the next restart. Nothing
    already counted is refunded either — an owner raising the ceiling mid-minute
    frees the slots the new number frees, and no more.

    The range is enforced at the edge (`DeploymentRateLimitRequest`) rather than
    here, for `set_access`'s reason: a bad number is a request to fix and send
    again, not a state the domain has to have an opinion about.
    """
    deployment.rate_limit_per_user = rate_limit_per_user
    return _persist(db, deployment)


def regenerate_api_key(db: Session, deployment: Deployment) -> str:
    """Mint a new API key for `deployment` and return it — the old one stops working here.

    Both halves of the credential's life story: this is how an owner gets a key
    they can read (the plaintext is never stored, so there is no other way), and
    it is how a leaked key is revoked. One call, because they are the same call —
    a "revoke" that did not hand back a working key would leave the owner's
    integration broken with no way to fix it in the same breath.
    """
    api_key = generate_api_key()
    deployment.api_key_hash = hash_api_key(api_key)
    _persist(db, deployment)
    return api_key


def visible_to(user: User) -> ColumnElement[bool]:
    """The whole of "may this caller reach this Deployment" (ADR-0029 §2–§3), as SQL.

    A condition rather than a predicate over a loaded row (ADR-0036 §3), because the same rule
    has to answer two shapes of question — "may I address agent 7" and "which
    agents may I see on the running list" — and the second one is a query. Two
    hand-written versions of this rule is how a `selected_users` Deployment ends
    up hidden from a stranger on one screen and listed on the other.

    Must be used on a statement that joins `Agent`, since the owner's branch is a
    fact about the agent rather than about the publication.

    The owner's branch still requires `active`: a paused Deployment is not
    running for anybody, and the owner's continued access to it (to re-enable, to
    redeploy, to chat with it) comes from owning the agent — `get_addressable_agent`
    answers that before it ever asks this.
    """
    return and_(
        Deployment.status == DeploymentStatus.ACTIVE,
        or_(
            Agent.owner_id == user.id,
            and_(
                Deployment.access_mode == DeploymentAccessMode.SELECTED_USERS,
                select(DeploymentAllowedUser.id)
                .where(
                    DeploymentAllowedUser.deployment_id == Deployment.id,
                    DeploymentAllowedUser.user_id == user.id,
                )
                .exists(),
            ),
        ),
    )


def addressable_deployment(db: Session, agent: Agent, user: User) -> Deployment | None:
    """`agent`'s Deployment if `user` may talk to it right now, otherwise `None`.

    "Right now" is the point: an agent that has been published is not necessarily
    one this caller may reach, which is the entire difference between ADR-0013's
    gate and this one.
    """
    return db.execute(
        select(Deployment).join(Agent, Deployment.agent_id == Agent.id).where(Agent.id == agent.id, visible_to(user))
    ).scalar_one_or_none()


def deployment_for_api_key(db: Session, agent: Agent, api_key: str) -> Deployment | None:
    """`agent`'s Deployment if `api_key` is its key and it is serving, otherwise `None`.

    The API channel's whole authentication. It does not consult `access_mode`,
    and that is a deliberate reading of ADR-0029 §5 rather than a hole in it
    (ADR-0036 §4): `access_mode`
    names *users*, and a key is not a user — it is the owner's own door key,
    visible only to them, handed to a system they run. So it reaches exactly as
    far as the owner does and no further, which is what "the API is never more
    open than Web Chat" actually means.

    `status` it does consult. A paused Deployment serves nobody, and an
    integration that kept working through a pause would make the pause a lie.
    """
    deployment = deployment_of(db, agent)
    if deployment is None or not deployment.is_serving:
        return None
    return deployment if verify_api_key(api_key, deployment.api_key_hash) else None


def _persist(db: Session, deployment: Deployment) -> Deployment:
    """Commit and refresh — the tail every mutating function here ends with."""
    db.commit()
    db.refresh(deployment)
    return deployment
