"""UC1-36 (#148) — `POST /agents/{id}/deployed/chat` (SSE): chat with whichever
version of `agent_id` is currently `deployed`, spec.md §19 ("Vận hành"),
FR-40/FR-41.

Not `POST /agents/{id}/chat`, spec.md's own literal path: `src.api.agent_routes`
already owns that exact shape for V2's unrelated `AgentSpec`/compiler,
registered first in `src.main`, and Starlette dispatches on path shape, not a
path param's declared Python type — the identical collision
`src.api.uc1_agent_routes`' own module docstring already names for
`/spec-versions` vs V2's `/versions`.

Reuses `src.compiler.sandbox.run_sandbox_turn` for the turn itself — the
function is generic despite its name (compiled agent + message + principal +
thread id in, a transcript out); the only things that make a turn "sandbox" or
"production" are which spec resolves it, whose identity the principal carries,
and which thread namespace the id is minted under (`ThreadNamespace.PRODUCTION`
here vs `.SANDBOX` there) — `authority.budget` applies identically either way
(spec.md §15's own "áp cùng ngân sách runtime thật dù chạy ở thread nào").

Closes the `_ERP_READONLY_ROLE` gap `src.compiler.tools` flagged: this is the
one caller of `default_tool_implementations` that passes the deployed spec's
own `metadata.id` as `erp_readonly_role`, so `POST /agents/{id}/suspend`
(FR-35) revoking that role actually stops this door's own reads the instant
it runs, not only the shared `agt_ar_assistant_ro` every pre-deploy caller
still uses.

Open to any signed-in user (`CurrentUser`, not `AdminUser`) — the same
reasoning `agent_routes.chat_with_agent`'s own docstring gives for V2's
`/chat`: a deployed agent is a product, and the point of deploying is that
other people can use it. FR-41's RowScope (sourced from the *caller's own*
JWT inside `default_tool_implementations`/`execute_tool_call`, never from
whoever compiled the spec) is what keeps that safe.
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.agents.threads import caller_key
from src.agentspec.spec import AgentSpec
from src.agentspec.store import get_deployed_spec_version, list_deployed_specs
from src.agentspec.views import DisallowedBaseTableError
from src.auth.dependencies import CurrentUser
from src.compiler.graph import AGENT_MODEL, CompiledAgent, CompileDependencies, UnboundCapabilityError, compile_spec
from src.compiler.sandbox import SandboxRunStatus, SandboxTurnResult, production_thread_id, run_sandbox_turn
from src.compiler.tools import default_tool_implementations
from src.db.base import SessionLocal, get_db
from src.db.models import User
from src.pm.ledger import record_agent_run, uc1_record
from src.policy.audit import audit_sink
from src.policy.pep import PEPClient, Principal, get_pep_client
from src.services.llm import ChatModelFactory, get_chat_model_factory
from src.services.secret_store import SecretStore, get_secret_store, resolve_credential_via
from src.services.uc1_tracing import export_uc1_trace
from src.workflow.uc1_connections import verified_objects_for_spec
from src.workflow.uc1_pending_actions import PendingActionBatch, record_pending_actions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["uc1-chat"])

#: The catalog an end user sees, on its own prefix for the reason
#: `uc1_agent_routes.catalog_router` already spells out: `/agents` is V2's
#: `int`-keyed surface, registered first, and a literal `/agents/available`
#: would only ever be reachable by being careful about registration order
#: forever.
#:
#: Separate from that admin catalog rather than a role-branch inside it,
#: because the two answer different questions. `GET /uc1-agents` lists every
#: agent that has *any* version, at whatever status, so an agent_admin can go
#: on working on it; this lists only what is `deployed` and only what the
#: caller is inside the audience of, because an end user has nothing to do
#: with a draft and cannot be helped by seeing one.
available_router = APIRouter(prefix="/uc1-agents", tags=["uc1-chat"])

#: One process-wide, in-memory checkpointer for every `prod:`-namespaced
#: thread — same shape and same reasoning as `sandbox_routes`' own
#: `_SANDBOX_CHECKPOINTER`, a *separate* instance so a restart's amnesia and a
#: sandbox rehearsal's own thread history are each contained to their own
#: saver, never mixed by sharing one.
_PRODUCTION_CHECKPOINTER = InMemorySaver()


class DeployedChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    #: Per-caller by default (the thread id below already includes the
    #: caller's own address via `_thread_id_for`), so two different people
    #: asking with the same `conversation_id` still land on two different
    #: threads — see that function's own docstring.
    conversation_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,64}$")


def _deployed_spec_or_404(agent_id: str, db: Session) -> AgentSpec:
    spec = get_deployed_spec_version(db, agent_id)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"agent {agent_id!r} không có version nào đang deployed"
        )
    return spec


def _principal_of(user: User) -> Principal:
    return Principal(
        sub=str(user.id),
        employee_id=user.employee_id or str(user.id),
        roles=[user.role.value],
        department=user.department or "",
    )


def _thread_id_for(agent_id: str, version: int, user: User, conversation_id: str) -> str:
    """`production_thread_id`'s own `conversation_id`, namespaced per caller so
    two different signed-in users asking with the same `conversation_id`
    (the common case: neither one ever picks a value, both get the default)
    never share a thread — the same "no caller can land in someone else's
    conversation" guarantee `agent_routes._addressed_namespace` gives V2's
    own `/chat`.
    """
    return production_thread_id(agent_id, version, f"{user.id}:{conversation_id}")


@dataclass(frozen=True, slots=True)
class ProductionRuntimeDeps:
    """Every FastAPI-resolved dependency the production chat flow needs,
    bundled — the same move `sandbox_routes.SandboxRuntimeDeps` already makes
    for the identical reason: `_compile_for_production` fixes all four for
    the whole of one request.
    """

    db: Session
    model_factory: ChatModelFactory
    pep_client: PEPClient
    secrets: SecretStore


@dataclass(frozen=True, slots=True)
class ProductionTurnRequest:
    """Which turn this is and who is asking — `sandbox_routes.SandboxTurnRequest`'s
    own counterpart: everything `_chat_stream` needs about *this specific
    call*, separate from `ProductionRuntimeDeps`'s "how to run one at all".
    """

    agent_id: str
    version: int
    thread_id: str
    principal: Principal
    message: str


def _compile_for_production(spec: AgentSpec, deps: ProductionRuntimeDeps) -> CompiledAgent:
    try:
        return compile_spec(
            spec,
            CompileDependencies(
                model=deps.model_factory(AGENT_MODEL),
                tool_implementations=default_tool_implementations(
                    resolve_credential=resolve_credential_via(deps.secrets), erp_readonly_role=spec.metadata.id
                ),
                pep_client=deps.pep_client,
                audit_sink=audit_sink(),
                checkpointer=_PRODUCTION_CHECKPOINTER,
                verified_connection_objects=verified_objects_for_spec(deps.db, spec),
            ),
        )
    except (UnboundCapabilityError, DisallowedBaseTableError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _chat_stream(compiled: CompiledAgent, request: ProductionTurnRequest, db: Session) -> AsyncIterator[str]:
    yield _sse("thread", {"thread_id": request.thread_id})

    result = run_sandbox_turn(
        compiled, message=request.message, principal=request.principal, thread_id=request.thread_id
    )

    if result.pending_actions:
        record_pending_actions(
            db,
            PendingActionBatch(
                agent_id=request.agent_id,
                version=request.version,
                requester=request.principal,
                pending_actions=result.pending_actions,
            ),
        )
    export_uc1_trace(
        result.transcript,
        spec=compiled.policy_spec,
        principal=request.principal,
        thread_id=request.thread_id,
        request_id=result.request_id,
    )
    _record_in_ledger(compiled, request, result)

    for step in result.transcript:
        yield _sse("step", dict(step))

    if result.status is SandboxRunStatus.BUDGET_EXCEEDED:
        yield _sse("error", result.budget_error or {"code": "budget_exceeded"})
    else:
        yield _sse(
            "done",
            {"final_answer": result.final_answer, "total_cost_usd": result.total_cost_usd, "run_id": result.request_id},
        )


def _caller_key_of(principal: Principal) -> str:
    """`caller_key(user_id=…)` when the principal is a signed-in user — the key V2 files its callers under."""
    return caller_key(user_id=int(principal.sub)) if str(principal.sub).isdigit() else str(principal.sub)


def _record_in_ledger(compiled: CompiledAgent, request: ProductionTurnRequest, result: SandboxTurnResult) -> None:
    """The turn in the shared run ledger (`agent_runs`, ADR-0053) — the row the PM board reads.

    Until this call UC-1 production traffic left no row any dashboard could
    count: `run_costs` is keyed to V2's `agents`, which a UC-1 spec has no row
    in. Its own session, like every writer that runs inside a streaming
    response, and never load-bearing: a ledger that cannot be written is a
    warning in the log, not a turn that failed.
    """
    budget = result.status is SandboxRunStatus.BUDGET_EXCEEDED
    try:
        db = SessionLocal()
        try:
            record_agent_run(
                db,
                uc1_record(
                    request_id=result.request_id,
                    agent_key=request.agent_id,
                    version=request.version,
                    agent_name=compiled.policy_spec.metadata.name,
                    thread_id=request.thread_id,
                    caller=_caller_key_of(request.principal),
                    input_message=request.message,
                    steps=result.transcript,
                    final_answer=result.final_answer,
                    total_cost_usd=result.total_cost_usd,
                    budget_exceeded=budget,
                    budget_detail=(result.budget_error or {}).get("detail") if budget else None,
                ),
            )
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — the ledger is observability, never the answer
        logger.warning("Không ghi được lượt UC-1 vào agent_runs", exc_info=True)


class AvailableAgentResponse(BaseModel):
    """One deployed agent as the person who will actually talk to it sees it."""

    agent_id: str
    name: str
    description: str
    version: int
    #: Shown so a caller understands *why* two people get different answers from
    #: the same agent before they think it is broken — a rep sees their own book,
    #: their manager sees the team's. Descriptive only; the row filter that
    #: really applies is the PEP's, decided per tool call.
    data_scope: str


def _is_in_audience(spec: AgentSpec, user: User) -> bool:
    """Whether `user` falls inside `spec.audience` — the same two conditions
    `authz.rego`'s audience block denies on (a role in `audience.roles`, and a
    department in `audience.departments` when that list is non-empty).

    **A visibility filter, not an authorization decision**, and the distinction
    matters: nothing here grants anything. Every tool call this agent makes
    still goes to the PEP, which re-derives both conditions from the signed spec
    and refuses on its own. What this function prevents is a catalog that lists
    agents the caller would only be refused by — an end user reading "you have
    no access" from an agent they were invited to open learns nothing except
    that the product is broken.

    Duplicating the rule in Python is therefore a duplication of a *display*
    rule, not of the policy. If the two ever disagree the PEP wins, and the
    visible symptom is a listed agent that refuses — never an unlisted agent
    that would have worked, and never access the policy did not grant.
    """
    if user.role.value not in spec.audience.roles:
        return False
    departments = list(spec.audience.departments)
    return not departments or (user.department or "") in departments


@available_router.get("/available", response_model=list[AvailableAgentResponse])
def list_available_agents(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> list[AvailableAgentResponse]:
    """The deployed agents this caller may actually use — UC-1's missing front door.

    `POST /agents/{id}/deployed/chat` below has always been open to any signed-in
    user, deliberately (see the module docstring: "a deployed agent is a product,
    and the point of deploying is that other people can use it"). Nothing handed
    out the ids, though: the only catalog was `GET /uc1-agents`, which is
    `AdminUser`, so in practice the door was open onto a corridor nobody could
    find. A `sales_rep` could hold a perfectly valid session and still have no
    way to learn that an agent existed for them.
    """
    return [
        AvailableAgentResponse(
            agent_id=spec.metadata.id,
            name=spec.metadata.name,
            description=spec.metadata.description or "",
            version=spec.metadata.version,
            data_scope=spec.audience.data_scope.value,
        )
        for spec in list_deployed_specs(db)
        if _is_in_audience(spec, user)
    ]


@router.post("/{agent_id}/deployed/chat")
async def chat_with_deployed_agent(
    agent_id: str,
    payload: DeployedChatRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    model_factory: Annotated[ChatModelFactory, Depends(get_chat_model_factory)],
    pep_client: Annotated[PEPClient, Depends(get_pep_client)],
    secrets: Annotated[SecretStore, Depends(get_secret_store)],
) -> StreamingResponse:
    """FR-40/FR-41: chat with the deployed version, by the caller's own identity.

    404 before any streaming starts, both when `agent_id` has never existed
    and when it exists but has nothing `deployed` right now (suspended,
    still draft, rolled back off) — indistinguishable from the caller's side,
    the same "no version deployed" shape `deployment_routes` already treats
    as a 404 rather than a 409 for the identical reason (nothing to point at).
    """
    spec = _deployed_spec_or_404(agent_id, db)
    deps = ProductionRuntimeDeps(db=db, model_factory=model_factory, pep_client=pep_client, secrets=secrets)
    compiled = _compile_for_production(spec, deps)
    request = ProductionTurnRequest(
        agent_id=agent_id,
        version=spec.metadata.version,
        thread_id=_thread_id_for(agent_id, spec.metadata.version, user, payload.conversation_id),
        principal=_principal_of(user),
        message=payload.message,
    )
    return StreamingResponse(_chat_stream(compiled, request, db), media_type="text/event-stream")
