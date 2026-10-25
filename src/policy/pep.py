"""The one seam allowed to call the PEP sidecar (UC1-12 / #113, §6.2).

`authz.rego`'s own header calls the runtime "a dumb executor: if this policy
does not return allow, nothing happens." OPA runs as its own container
(`docker-compose.yml`'s `policy` service) evaluating that file, reached over
its REST API — `POST /v1/data/agentbuilder/authz/decision`. Every call site
that needs a decision goes through `get_pep_client`, a FastAPI dependency, the
same shape as `src.services.embedding.get_embedding_client`:
`app.dependency_overrides[get_pep_client]` is what lets a runtime's tests swap
in a fake decision without a live sidecar. `_evaluate` is the only function in
this module that reaches OPA — there is no second path to it, so a runtime
built against this module cannot skip the PEP by construction. The runtime
itself is E9 (not yet built); this module is what it will have to call
through, not a runtime of its own.

The failure mode is deliberately the opposite of `src.services.reranker`'s.
Reranking degrades gracefully because a worse ranking is a quality loss, not a
safety one. A PEP call is the opposite: unreachable, slow, or answering with
something this module cannot parse must never be mistaken for a decision, let
alone `allow`. `evaluate` never fabricates one out of a failure — it raises
`PEPUnavailableError`, same choice `src.services.embedding` makes for a
corrupted vector, and an uncaught exception is itself fail-closed: no `allow`
was ever produced, so nothing happens.

`AuthzRequest`'s fields are exactly the input shape documented at the top of
`docs/usecase_spec/platform/authz.rego` — this module does not add a field the
policy does not read or drop one it does.
"""

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from src.agentspec import AgentSpec
from src.agentspec.spec import Scope
from src.config import get_settings


class Effect(StrEnum):
    """`authz.rego`'s `effect` — the only three values `decision.effect` ever takes."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class Principal:
    """Whoever is talking to the agent right now — never `metadata.created_by` (delegated authority)."""

    sub: str
    employee_id: str
    roles: list[str]
    department: str


@dataclass(frozen=True)
class Action:
    hash: str
    tool: str
    scope: Scope
    mode: str
    connection_id: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Provenance:
    """T2's input: whether this call's arguments trace back to content the agent merely read."""

    from_untrusted_data: bool
    sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Session:
    tool_calls_used: int
    per_tool_calls: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Approval:
    """Bound to one `action_hash` — approving one payload never authorises a different one (FR-45)."""

    tool: str
    approver_sub: str
    approver_roles: list[str]
    action_hash: str


@dataclass(frozen=True)
class AuthzRequest:
    spec: AgentSpec
    principal: Principal
    action: Action
    provenance: Provenance
    session: Session
    approvals: list[Approval] = field(default_factory=list)

    def as_input(self) -> dict[str, Any]:
        """The `{"input": ...}` body OPA's REST API expects — `spec` via the same
        serialization `validate_spec` accepts back (`model_dump(mode="json", by_alias=True)`),
        everything else a plain dataclass-to-dict walk.
        """
        return {
            "spec": self.spec.model_dump(mode="json", by_alias=True),
            "principal": asdict(self.principal),
            "action": asdict(self.action),
            "provenance": asdict(self.provenance),
            "session": asdict(self.session),
            "approvals": [asdict(approval) for approval in self.approvals],
        }


@dataclass(frozen=True)
class Decision:
    """`authz.rego`'s `decision` object, unchanged in shape — this module reads it, never reshapes it."""

    effect: Effect
    reasons: list[str]
    row_filter: dict[str, Any]
    approver_roles: list[str]
    redact_fields: list[str]
    max_rows: int

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW


PEPClient = Callable[[AuthzRequest], Decision]

#: UC1-32 (#144) — how a caller records a decision to `audit_log` immediately
#: after getting it back from a `PEPClient`. `str` is the request id UC1-31's
#: structured log lines join against `audit_log` by (`src.policy.audit`'s own
#: docstring) — not part of `AuthzRequest` itself, since one request id spans
#: every tool call one HTTP request makes, while `AuthzRequest` is built fresh
#: per call.
AuditSink = Callable[[str, AuthzRequest, Decision], None]


class PEPUnavailableError(RuntimeError):
    """The sidecar could not be reached, or answered with something this module will not trust as a decision."""


def _decision_from(body: object) -> Decision:
    if not isinstance(body, Mapping):
        raise ValueError(f"expected a JSON object, got {type(body).__name__}")
    result = body["result"]
    if not isinstance(result, Mapping):
        raise ValueError(f"'result' is not a JSON object, got {type(result).__name__}")
    return Decision(
        effect=Effect(result["effect"]),
        reasons=list(result["reasons"]),
        row_filter=dict(result["row_filter"]),
        approver_roles=list(result["approver_roles"]),
        redact_fields=list(result["redact_fields"]),
        max_rows=int(result["max_rows"]),
    )


def _evaluate(request: AuthzRequest) -> Decision:
    settings = get_settings()
    try:
        response = httpx.post(
            f"{settings.opa_url}/v1/data/agentbuilder/authz/decision",
            json={"input": request.as_input()},
            timeout=settings.opa_http_timeout_seconds,
        )
        response.raise_for_status()
        return _decision_from(response.json())
    except (httpx.TransportError, httpx.HTTPStatusError, ValueError, TypeError, KeyError) as exc:
        # ValueError/TypeError/KeyError: a 200 whose body this module cannot
        # trust as a decision is exactly as dangerous as no response at all —
        # see the module docstring on why neither becomes a fabricated `Decision`.
        raise PEPUnavailableError(f"PEP sidecar unusable: {exc}") from exc


def get_pep_client() -> PEPClient:
    """FastAPI dependency: the only function in the system allowed to call the PEP sidecar."""
    return _evaluate
