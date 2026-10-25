"""UC1-38 (#126) — `/agents/{id}/versions/{v}/sandbox/...`, spec.md §19.

`POST .../sandbox/chat` runs one turn for real: compiles the stored spec
(UC1-10 / #124), runs it on a `sandbox:`-namespaced thread (FR-36), and
streams what happened as SSE. Blocking end-to-end rather than streamed
node-by-node — `run_sandbox_turn` calls `CompiledStateGraph.invoke`, not
`.stream` — a scope line, not an oversight: FR-36/37/39 ask for a transcript
a creator can see and an admin can review later, not incremental token
rendering (that belongs to the same guardrail/streaming work E9's chat
endpoint will need, UC1-19..23, not this ticket).
"""

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.agentspec.spec import AgentSpec
from src.agentspec.store import SpecVersionNotFoundError, get_spec_version
from src.agentspec.views import DisallowedBaseTableError
from src.auth.dependencies import AdminUser
from src.compiler.graph import AGENT_MODEL, CompiledAgent, CompileDependencies, UnboundCapabilityError, compile_spec
from src.compiler.sandbox import SandboxRunStatus, run_sandbox_turn, sandbox_thread_id
from src.compiler.tools import default_tool_implementations
from src.db.base import get_db
from src.db.models import User
from src.policy.audit import audit_sink
from src.policy.pep import PEPClient, Principal, get_pep_client
from src.services.llm import ChatModelFactory, get_chat_model_factory
from src.services.uc1_tracing import export_uc1_trace
from src.services.vault_secret_store import VaultKvStore, get_vault_store, resolve_credential_via
from src.workflow.uc1_connections import verified_objects_for_spec
from src.workflow.uc1_pending_actions import PendingActionBatch, record_pending_actions
from src.workflow.uc1_sandbox_runs import list_sandbox_runs, record_sandbox_run

router = APIRouter(prefix="/agents", tags=["sandbox"])

#: One process-wide, in-memory checkpointer for every sandbox thread. Not
#: `src.agents.memory`'s Postgres-backed one: that saver is async-only
#: (`AsyncPostgresSaver`), and `run_sandbox_turn` calls the graph's sync
#: `.invoke` — mixing the two raises at runtime. In-memory is also the more
#: honest choice for a *sandbox*: FR-36's isolation requirement is "not
#: production history", not "survives a restart", and a rehearsal thread
#: outliving this process is not a promise this endpoint makes.
_SANDBOX_CHECKPOINTER = InMemorySaver()


class SandboxChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    #: A creator may run several independent sandbox conversations against the
    #: same version; omitting it continues (or starts) that creator's default
    #: one, the common case in the demo script (§15).
    conversation_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,64}$")


class SandboxTranscriptStepResponse(BaseModel):
    node: str
    tool: str | None
    args: dict[str, Any] | None
    result: str | None
    tokens: int | None
    latency_ms: float
    cost_usd: float


class SandboxRunResponse(BaseModel):
    id: int
    thread_id: str
    message: str
    status: SandboxRunStatus
    transcript: list[SandboxTranscriptStepResponse]
    total_cost_usd: float
    final_answer: str | None

    model_config = {"from_attributes": True}


@dataclass(frozen=True, slots=True)
class SandboxRuntimeDeps:
    """Every FastAPI-resolved dependency the sandbox flow needs, bundled — the same
    "one argument, not four" fix `CompileDependencies` (`src.compiler.graph`)
    already applies, for the same reason: `_compile_for_sandbox` fixes all
    four for the whole of one request.
    """

    db: Session
    model_factory: ChatModelFactory
    pep_client: PEPClient
    vault: VaultKvStore


@dataclass(frozen=True, slots=True)
class SandboxTurnRequest:
    """Which turn this is and who is asking — everything `_sandbox_chat_stream`
    needs about *this specific call*, separate from `SandboxRuntimeDeps`'s
    "how to run one at all".
    """

    agent_id: str
    version: int
    thread_id: str
    principal: Principal
    payload: SandboxChatRequest


def _sse(event: str, data: Mapping[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _spec_for(agent_id: str, version: int, db: Session) -> AgentSpec:
    try:
        return get_spec_version(db, agent_id, version)
    except SpecVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None


def _principal_of(user: User) -> Principal:
    return Principal(
        sub=str(user.id),
        employee_id=user.employee_id or str(user.id),
        roles=[user.role.value],
        department=user.department or "",
    )


def _compile_for_sandbox(spec: AgentSpec, deps: SandboxRuntimeDeps) -> CompiledAgent:
    """Compile `spec` for a sandbox turn, or refuse with a 4xx before any streaming starts.

    Runs before `StreamingResponse` opens — like `_spec_for`'s 404 — so a
    spec that fails to compile (an unbound capability, FR-24's unverified
    view) never leaves headers already sent with no way to change the status.
    """
    try:
        return compile_spec(
            spec,
            CompileDependencies(
                model=deps.model_factory(AGENT_MODEL),
                tool_implementations=default_tool_implementations(resolve_credential=resolve_credential_via(deps.vault)),
                pep_client=deps.pep_client,
                audit_sink=audit_sink(),
                checkpointer=_SANDBOX_CHECKPOINTER,
                verified_connection_objects=verified_objects_for_spec(deps.db, spec),
            ),
        )
    except (UnboundCapabilityError, DisallowedBaseTableError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None


async def _sandbox_chat_stream(compiled: CompiledAgent, request: SandboxTurnRequest, db: Session) -> AsyncIterator[str]:
    yield _sse("thread", {"thread_id": request.thread_id})

    result = run_sandbox_turn(
        compiled, message=request.payload.message, principal=request.principal, thread_id=request.thread_id
    )
    record_sandbox_run(
        db,
        agent_id=request.agent_id,
        version=request.version,
        principal_employee_id=request.principal.employee_id,
        message=request.payload.message,
        result=result,
    )
    if result.pending_actions:
        # UC1-21 (#137): a `require_approval` write this turn hit goes into
        # the action queue right away — the sandbox's own thread namespace
        # (FR-36) does not defer this, since an admin rehearsing a version is
        # exactly who spec.md §15's demo script shows triggering it.
        record_pending_actions(
            db,
            PendingActionBatch(
                agent_id=request.agent_id, version=request.version, requester=request.principal, pending_actions=result.pending_actions
            ),
        )
    # UC1-31 (#143): the same `request_id` this turn's own PEP decisions were
    # logged/audited under (`src.compiler.tools.execute_tool_call`) — never
    # load-bearing, see `export_uc1_trace`'s own docstring.
    export_uc1_trace(
        result.transcript,
        spec=compiled.policy_spec,
        principal=request.principal,
        thread_id=request.thread_id,
        request_id=result.request_id,
    )

    for step in result.transcript:
        yield _sse("step", dict(step))

    if result.status is SandboxRunStatus.BUDGET_EXCEEDED:
        yield _sse("error", result.budget_error or {"code": "budget_exceeded"})
    else:
        yield _sse("done", {"final_answer": result.final_answer, "total_cost_usd": result.total_cost_usd})


@router.post("/{agent_id}/versions/{version}/sandbox/chat")
async def sandbox_chat(
    agent_id: str,
    version: int,
    payload: SandboxChatRequest,
    user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
    model_factory: Annotated[ChatModelFactory, Depends(get_chat_model_factory)],
    pep_client: Annotated[PEPClient, Depends(get_pep_client)],
    vault: Annotated[VaultKvStore, Depends(get_vault_store)],
) -> StreamingResponse:
    """FR-36: chat with a compiled-but-unapproved version, by the caller's own identity, on its own thread."""
    spec = _spec_for(agent_id, version, db)
    deps = SandboxRuntimeDeps(db=db, model_factory=model_factory, pep_client=pep_client, vault=vault)
    compiled = _compile_for_sandbox(spec, deps)
    request = SandboxTurnRequest(
        agent_id=agent_id,
        version=version,
        thread_id=sandbox_thread_id(agent_id, version, payload.conversation_id),
        principal=_principal_of(user),
        payload=payload,
    )
    return StreamingResponse(_sandbox_chat_stream(compiled, request, db), media_type="text/event-stream")


@router.get("/{agent_id}/versions/{version}/sandbox/runs", response_model=list[SandboxRunResponse])
def list_my_sandbox_runs(
    agent_id: str, version: int, user: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> list[SandboxRunResponse]:
    """FR-37: every sandbox turn recorded for this version, newest first — the approval dossier's evidence (UC1-16)."""
    return [SandboxRunResponse.model_validate(row) for row in list_sandbox_runs(db, agent_id, version)]
