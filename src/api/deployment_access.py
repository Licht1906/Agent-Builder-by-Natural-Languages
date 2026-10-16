"""The doors into a published agent: who may open them, and as whom — #50 / ADR-0029.

ADR-0013 had one rule here and it was a single boolean: an agent with a version
deployed was addressable by anyone signed in. ADR-0029 replaces that boolean with
a row, and ADR-0036 §3–§4 turn it into three questions the platform could not
previously ask: is this Deployment serving, may *this* caller reach it, and is
the caller a person at all. #51 / ADR-0037 adds a fourth, asked at the same
moment and for the same reason it is asked here at all — has this caller already
asked often enough (`charge_one_run`).

The last one is what makes this a module rather than two more functions in
`src.api.agent_routes`. The API channel authenticates with the Deployment's own
key and has no `User` behind it, so "who is calling" stops being a synonym for
"which account" — and both the conversation each caller gets (`Caller.key`) and
the rate limit each caller is held to (#51) hang off that answer.

Kept out of `src.api.deployment_routes` on purpose: that module *manages* the
Deployment and imports `OwnedAgent` from `agent_routes`, so a gate living there
would make `agent_routes` import it back.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from src.agents.threads import caller_key
from src.auth.dependencies import CurrentUser, OptionalUser
from src.db.base import get_db
from src.db.models import Agent, Deployment, User
from src.deployments.publication import addressable_deployment, deployment_for_api_key, deployment_of
from src.deployments.rate_limit import RateLimitExceededError, SlidingWindowLimiter, get_rate_limiter

#: The header the API channel presents its key on. Not `Authorization`, which
#: already carries the platform's own sessions: one header holding two unrelated
#: credential formats is how a caller ends up being told their API key is a
#: malformed JWT.
API_KEY_HEADER = "X-API-Key"

#: The one refusal this module gives, whatever went wrong. A caller who may not
#: reach a Deployment must not be able to tell "no such agent" from "not for you"
#: — that distinction is what turns id-probing into a directory of other
#: people's work (ADR-0013 §3), and `access_mode` gives it more to leak than V1
#: ever did.
_NOT_FOUND = "Không tìm thấy agent"


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is on the other end of a run, as one identity — see ADR-0029 §6, ADR-0036 §6.

    One field and two named constructors, rather than a `user` and a
    `deployment_id` of which exactly one is set: the second shape can be built
    wrong, and everything downstream needs to *tell callers apart* without caring
    which kind they are. The conversation is kept separate per caller
    (ADR-0029, "nhiều người dùng chung một Deployment") and #51's limit is counted
    per caller — both of which are this string and nothing else.
    """

    key: str

    @classmethod
    def signed_in(cls, user: User) -> "Caller":
        """A person, reaching a Deployment through their browser session."""
        return cls(caller_key(user_id=user.id))

    @classmethod
    def holding_the_key(cls, deployment: Deployment) -> "Caller":
        """An integration, reaching it with the Deployment's API key — no user behind it."""
        return cls(caller_key(deployment_id=deployment.id))


@dataclass(frozen=True, slots=True)
class AddressedAgent:
    """One agent somebody is about to talk to, and who that somebody is."""

    agent: Agent
    caller: Caller


def get_addressable_agent(agent_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> Agent:
    """The agent at `agent_id` if this caller may address it: theirs, or one published to them.

    ADR-0013's widening, narrowed by ADR-0029. A published agent is still a
    product other people are meant to use, but *which* other people is now the
    owner's decision rather than the platform's default: an active Deployment is
    reachable by its `access_mode`, and one that is `private`, paused, or has
    never been published is a 404 to everyone but the owner — the same 404 an
    agent nobody deployed always gave.

    The owner is answered before the Deployment is even read. Their access does
    not come from the publication and must survive pausing it, or "disable" would
    also mean "lock yourself out of the thing you just paused".
    """
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise _not_found()
    if agent.owner_id == user.id:
        return agent
    if addressable_deployment(db, agent, user) is None:
        raise _not_found()
    return agent


#: Declare this type on an endpoint that talks *to* an agent through a browser
#: session, and the "yours, or published to you" rule comes with it.
AddressableAgent = Annotated[Agent, Depends(get_addressable_agent)]


def get_addressed_agent(
    agent_id: int,
    user: OptionalUser,
    db: Annotated[Session, Depends(get_db)],
    api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> AddressedAgent:
    """The same agent, reached by **either** channel — a session, or the Deployment's key.

    ADR-0029 §5: every active Deployment serves both channels at once, so this is
    one door with two locks rather than two doors. The key is tried first, and
    deliberately so — a request that presented one is asking to be the
    integration, not whichever account happens to also be signed in on that
    connection.

    Both failures land on the same 404 as `get_addressable_agent`, including a
    key that is simply wrong. A distinct "bad key" answer would tell an
    unauthenticated prober that this agent has a Deployment worth guessing at.
    """
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise _not_found()

    if api_key is not None:
        deployment = deployment_for_api_key(db, agent, api_key)
        if deployment is None:
            raise _not_found()
        return AddressedAgent(agent=agent, caller=Caller.holding_the_key(deployment))

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Cần đăng nhập hoặc gửi API key của deployment ở header {API_KEY_HEADER}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return AddressedAgent(agent=get_addressable_agent(agent_id, user, db), caller=Caller.signed_in(user))


#: Declare this type on an endpoint an integration may call, and both locks come
#: with it — along with the identity the run is filed under.
AddressedByAnyChannel = Annotated[AddressedAgent, Depends(get_addressed_agent)]


def charge_one_run(db: Session, agent: Agent, caller: Caller, limiter: SlidingWindowLimiter) -> None:
    """Count one run against `caller`'s allowance on this agent's Deployment — #51 / ADR-0037.

    Here rather than in a middleware because the ceiling is a *Deployment's*
    number and the caller is whoever the locks above just decided they are:
    middleware sees a URL and a header, and reconstructing both from the path
    would be a second copy of the access gate running before the real one.

    Charged before the endpoint body, which is the last moment a refusal is free
    — nothing has been streamed and no model has been called, so the answer can
    still be an HTTP status rather than an event buried in an SSE body.

    An agent with **no** Deployment is not metered. The limit is a field on the
    publication, so there is no number to enforce; the only caller who can reach
    an unpublished agent is its owner, and inventing a ceiling for them would
    throttle a creator against a figure they never set.
    """
    deployment = deployment_of(db, agent)
    if deployment is None:
        return
    try:
        limiter.charge(deployment_id=deployment.id, caller=caller.key, limit=deployment.rate_limit_per_user)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.message,
            # The machine-readable half of the refusal. `detail` stays a sentence
            # because that is what the frontend renders (ADR-0037 §5), so the two
            # numbers an integration acts on travel in headers instead: the one
            # HTTP already has for "when", and the conventional one for "what the
            # ceiling was". A browser reads neither and loses nothing.
            headers={
                "Retry-After": str(exc.retry_after_seconds),
                "X-RateLimit-Limit": str(exc.limit),
            },
        ) from None


def get_metered_agent(
    agent: AddressableAgent,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    limiter: Annotated[SlidingWindowLimiter, Depends(get_rate_limiter)],
) -> Agent:
    """The Web Chat door, with this caller's request counted against their limit.

    The caller is `Caller.signed_in`, whatever thread the request goes on to
    address. A turn in the `trial` namespace runs the deployed version through
    this very door, so exempting it would make the ceiling something a caller
    opts out of by sending one extra field.
    """
    charge_one_run(db, agent, Caller.signed_in(user), limiter)
    return agent


#: Declare this instead of `AddressableAgent` on an endpoint that *runs* the
#: agent: the caller's allowance is checked along with their access.
MeteredAgent = Annotated[Agent, Depends(get_metered_agent)]


def get_metered_addressed_agent(
    addressed: AddressedByAnyChannel,
    db: Annotated[Session, Depends(get_db)],
    limiter: Annotated[SlidingWindowLimiter, Depends(get_rate_limiter)],
) -> AddressedAgent:
    """The same for the two-lock door, counting whichever caller the locks admitted.

    One function for both channels is the whole of #51's "the limit applies
    uniformly": an integration and a browser are counted by the same code against
    the same column, and each has its own allowance because each is its own
    `Caller.key`.
    """
    charge_one_run(db, addressed.agent, addressed.caller, limiter)
    return addressed


#: The metered counterpart of `AddressedByAnyChannel`, for the endpoint that runs.
MeteredByAnyChannel = Annotated[AddressedAgent, Depends(get_metered_addressed_agent)]
