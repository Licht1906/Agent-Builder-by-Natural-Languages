"""Thread identity — which conversation a chat turn belongs to.

A chat window is a **thread**. Everything the agent remembers within a session
hangs off the one string this module produces, so the rule that keeps one
conversation out of another's memory lives here rather than at each call site.

Four things separate threads, and all four are baked into that string:

- the **namespace** — a trial run must never write into production history
  (issue #6), which is what makes the sandbox (#9) safe to hand a real agent;
- the **agent** — two agents are two conversations even under one chat window;
- the **caller**, on a production thread — one Deployment serves many end-users
  and none of them may land in another's conversation (#50, ADR-0036 §6);
- the **conversation** — a new chat window is a new conversation, hence empty.

The conversation id is client-supplied, so it is constrained to a charset that
cannot contain the separator. Without that, a caller could pass
``prod:1:something`` as their conversation id and address a thread in a
namespace or an agent that is not theirs.

`RunIdentity` sits at the bottom of the file for the same reason `ThreadRef` is
here at all: a run is filed under its thread plus the version that executed, and
the cost ledger and the trace (#11) must file it under the *same* thing or
neither can be joined to the other.
"""

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum

#: The separator between the three parts of a checkpointer thread id. Excluded
#: from `CONVERSATION_ID_PATTERN` so no conversation id can contain one.
_PART_SEPARATOR = ":"

#: What a conversation id may look like. Shared with the HTTP layer
#: (`src.models.schemas`) so a malformed id is a 422 rather than a 500.
CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ThreadNamespace(StrEnum):
    """Which world a thread lives in.

    `TRIAL` is what a creator's own test run writes to while building an agent;
    `PRODUCTION` is real conversation history. Keeping them apart is a hard
    requirement of issue #6 — test chatter must not become the agent's memory.
    """

    PRODUCTION = "prod"
    TRIAL = "trial"


class InvalidConversationIdError(ValueError):
    """A conversation id that could address a thread other than its own."""


@dataclass(frozen=True, slots=True)
class ThreadRef:
    """One conversation, addressable in the checkpointer.

    Frozen: a thread ref is passed down into the runtime, and a run that could
    retarget its own thread mid-flight is exactly the bug this type prevents.
    """

    agent_id: int
    conversation_id: str
    namespace: ThreadNamespace = ThreadNamespace.PRODUCTION
    #: Who is talking, on threads where more than one person can be — see
    #: `caller_key` below. `None` on a rehearsal: the `trial` namespace is the
    #: creator's own space (#6), and stamping an owner onto it would fork the
    #: sandbox's threads away from the ones their own chat window writes.
    caller: str | None = None

    def __post_init__(self) -> None:
        if not CONVERSATION_ID_PATTERN.fullmatch(self.conversation_id):
            raise InvalidConversationIdError(
                f"conversation_id không hợp lệ: {self.conversation_id!r} "
                f"(chỉ chấp nhận {CONVERSATION_ID_PATTERN.pattern})"
            )
        if self.caller is not None and not CONVERSATION_ID_PATTERN.fullmatch(self.caller):
            # Same rule for the same reason: a caller key containing the
            # separator could address a thread belonging to another agent.
            raise InvalidConversationIdError(f"caller không hợp lệ: {self.caller!r}")

    @classmethod
    def new(
        cls, agent_id: int, *, namespace: ThreadNamespace = ThreadNamespace.PRODUCTION, caller: str | None = None
    ) -> "ThreadRef":
        """A fresh conversation — which is to say, one that starts with no history."""
        return cls(agent_id=agent_id, conversation_id=uuid.uuid4().hex, namespace=namespace, caller=caller)

    @property
    def checkpoint_thread_id(self) -> str:
        """The key this conversation's checkpoints are stored under.

        The caller is a *part* of the key rather than a check made beside it, so
        two end-users of one Deployment cannot land in the same conversation even
        if one of them sends the other's `conversation_id` (#50). Isolation that
        depends on ids being unguessable is isolation that stops holding the day
        an id is guessed, logged or pasted.

        A thread with **no** caller keeps its old three-part key, so everything
        that was addressing one before #50 — a trial run, a resumed workflow —
        still finds it. Production conversations from before #50 are the other
        side of that coin: they were written without a caller and are now
        addressed with one, so they read as empty (ADR-0036 §6).
        """
        parts = (self.namespace.value, str(self.agent_id), self.conversation_id)
        if self.caller is None:
            return _PART_SEPARATOR.join(parts)
        return _PART_SEPARATOR.join((self.namespace.value, str(self.agent_id), self.caller, self.conversation_id))


def caller_key(*, user_id: int | None = None, deployment_id: int | None = None) -> str:
    """The `ThreadRef.caller` for one end-user, or for one Deployment's API key.

    Two shapes because ADR-0029 §6 names two caller identities: `user_id` over
    Web Chat, and the key's own identity over the API — where there is no user at
    all, only the integration the owner handed the key to. Prefixed so the two
    can never collide on a number, and built here rather than at either call site
    so the cost ledger, the rate limiter (#51) and the checkpointer all mean the
    same thing by "the same caller".
    """
    if user_id is not None:
        return f"u{user_id}"
    if deployment_id is not None:
        return f"k{deployment_id}"
    raise ValueError("caller_key cần user_id hoặc deployment_id")


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Which run this is — everything the outside world files a run under.

    A thread already says *whose* conversation and *which world* (#6); a run adds
    the version that executed and the trace it can be looked up by. Both the cost
    ledger (#11) and the trace need exactly these, so they are one type rather
    than four arguments repeated at two call sites.

    Built here rather than in either consumer because `trace_id` has to be the
    same string in both: a `run_costs` row saying "this run cost $0.02" is only
    actionable if the trace it names is the trace of that run. Minting it up
    front, before anything executes, is what makes that true without either side
    telling the other what it chose.
    """

    thread: ThreadRef
    version_id: int
    trace_id: str

    @classmethod
    def for_run(cls, thread: ThreadRef, version_id: int) -> "RunIdentity":
        """A fresh identity for a run about to start.

        32 hex characters because that is a W3C trace id, which is what the trace
        backend stores — generating it here rather than letting the backend
        assign one means the id exists even when tracing is switched off, so a
        cost row never has a hole where its trace reference should be.
        """
        return cls(thread=thread, version_id=version_id, trace_id=uuid.uuid4().hex)

    @property
    def is_trial(self) -> bool:
        """Whether this run is a rehearsal rather than the real thing."""
        return self.thread.namespace is ThreadNamespace.TRIAL
