"""The exported package's HTTP surface — chat, approvals, health, and nothing else (DS-13).

Four routes. No Designer, Builder, admin or PM route is mounted, and none is
importable from here: the platform's routers are never imported at all, so
the package cannot grow one by accident (`test_standalone_runtime` lists the
routes).

**Who is asking** comes from `runtime.yaml`'s `auth`. `trusted_header` — a
gateway in front of the package that has already signed the person in and
passes who they are in one header — is the mode served here; `oidc`, `jwt`
and `custom` are DS-14's, and asking for one now refuses to start rather than
quietly running everyone as nobody. A request that names nobody is a 401: it
never falls back to a default asker.

**Approvals are decided by the person who asked** (ADR-0059 §7), so the
approval routes only ever show and decide the caller's own pauses.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.agents.memory import close_checkpointer, get_checkpointer
from src.agents.runtime import Conversation, RunServices
from src.agents.threads import CONVERSATION_ID_PATTERN, RunIdentity, ThreadNamespace, ThreadRef
from src.db.base import SessionLocal
from src.db.models import RunApproval, RunApprovalStatus
from src.services.llm import ChatModelFactory
from src.services.tracing import NullTrace
from src.standalone.config import LlmConfig, RuntimeConfigError
from src.standalone.package import Package, PackageIds, asker_user_id, database_ready, prepare_database, reader_for
from src.workflow.approvals import (
    ApprovalDecisionError,
    approval_for_decision,
    approval_payload,
    decide_approval,
    parked_leg,
)
from src.workflow.knowledge import DEFAULT_PASSAGE_LIMIT, Passage
from src.workflow.runtime import Resumption, stream_workflow_reply

_log = logging.getLogger(__name__)

#: What a turn that broke says, instead of the exception — the log has the rest.
_RUN_FAILED = "Lượt chạy gặp lỗi và đã dừng — thử lại, hoặc báo quản trị viên kèm mã trace"


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1, max_length=8000)
    conversation_id: str | None = Field(default=None, pattern=CONVERSATION_ID_PATTERN.pattern)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    reason: str | None = Field(default=None, max_length=2000)


class _NoDocuments:
    """The package's `KnowledgeStore` until documents travel with it (DS-18): nothing to find."""

    def search(
        self, query: str, *, source_refs: Sequence[str], limit: int = DEFAULT_PASSAGE_LIMIT
    ) -> tuple[Passage, ...]:
        return ()


def openai_compatible_factory(llm: LlmConfig, api_key: str | None) -> ChatModelFactory:
    """Models from the company's OpenAI-compatible endpoint, by the map `runtime.yaml` gives.

    A node keeps the model the bundle names — the one it was evaluated with,
    and the one its cost is priced under — and this maps it to what is served
    here; anything unmapped (the platform's own internal calls included) is
    `default_model`.
    """

    def build(model_id: str) -> Any:
        return ChatOpenAI(
            model=llm.models.get(model_id, llm.default_model),
            base_url=llm.base_url,
            api_key=api_key or "not-needed",  # type: ignore[arg-type]
            stream_usage=True,
        )

    return build


def _asker(request: Request) -> str:
    """Who is asking — from the trusted header `runtime.yaml` names, or a 401. Never a default."""
    header = request.app.state.auth_header
    value = (request.headers.get(header) or "").strip()
    if not value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Thiếu header {header}")
    return value


def _ids(request: Request) -> PackageIds:
    return request.app.state.ids


Asker = Annotated[str, Depends(_asker)]
Ids = Annotated[PackageIds, Depends(_ids)]


def create_app(package: Package, *, model_factory: ChatModelFactory | None = None) -> FastAPI:
    """The package as an ASGI app. `model_factory` is a test seam; left out, it is `runtime.yaml`'s LLM."""
    if package.config.auth.mode != "trusted_header":
        raise RuntimeConfigError(
            f"auth.mode {package.config.auth.mode!r} chưa được hỗ trợ trong bản này — dùng 'trusted_header' (DS-14 thêm oidc, jwt, custom)"
        )
    llm_key = package.secrets.get(package.config.llm.api_key_secret or "")
    factory = model_factory or openai_compatible_factory(package.config.llm, llm_key)
    header = package.config.auth.header

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ids = prepare_database(package)
        for warning in package.warnings:
            _log.warning(warning)
        await get_checkpointer()
        yield
        await close_checkpointer()

    app = FastAPI(title=f"{package.name} — gói agent", lifespan=lifespan)
    app.state.auth_header = header

    @app.get("/health")
    def health() -> dict[str, Any]:
        ready = database_ready()
        return {"status": "ok" if ready else "degraded", "agent": package.name, "database": ready}

    @app.post("/chat")
    async def chat(payload: ChatRequest, who: Asker, ids: Ids) -> StreamingResponse:
        """One turn, as server-sent events — the platform's own event names."""
        thread = ThreadRef(
            agent_id=ids.agent_id,
            conversation_id=payload.conversation_id or uuid.uuid4().hex,
            namespace=ThreadNamespace.PRODUCTION,
            caller=_caller(who),
        )
        identity = RunIdentity.for_run(thread, ids.version_id)
        return _stream(package, factory, identity, payload.message, asker=who)

    @app.get("/approvals")
    def my_pending_approvals(who: Asker, ids: Ids) -> list[dict[str, Any]]:
        """The caller's own runs waiting on them, oldest first."""
        db = SessionLocal()
        try:
            rows = db.scalars(
                select(RunApproval)
                .where(
                    RunApproval.agent_id == ids.agent_id,
                    RunApproval.caller == _caller(who),
                    RunApproval.status == RunApprovalStatus.PENDING,
                )
                .order_by(RunApproval.created_at, RunApproval.id)
            )
            return [approval_payload(row) for row in rows]
        finally:
            db.close()

    @app.post("/approvals/{approval_id}")
    async def decide(approval_id: int, payload: DecisionRequest, who: Asker, ids: Ids) -> StreamingResponse:
        """Decide one of the caller's own pauses, and stream the rest of that run."""
        db = SessionLocal()
        try:
            row = db.get(RunApproval, approval_id)
            if row is None or row.agent_id != ids.agent_id or row.caller != _caller(who):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=f"Không tìm thấy yêu cầu duyệt {approval_id}"
                )
            try:
                row = approval_for_decision(db, approval_id, version_id=row.version_id)
                decision = decide_approval(
                    db, row, decided_by=asker_user_id(who), approved=payload.approved, reason=payload.reason
                )
            except ApprovalDecisionError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
            leg = parked_leg(db, row)
        finally:
            db.close()
        resumption = Resumption(
            run_id=leg.run_id,
            decision=decision,
            carried_seconds=leg.carried_seconds,
            carried_usage=leg.carried_usage,
        )
        return _stream(package, factory, leg.identity, leg.input_message, asker=leg.run_as or who, resume=resumption)

    return app


def _caller(asker: str) -> str:
    """The thread's caller for one asker: a stable digest, since a thread key holds only `[A-Za-z0-9_-]`.

    Prefixed `x` so it never meets the platform's `u…`/`k…` keys. A digest
    rather than the email itself, which a key cannot hold — and which a
    checkpoint key need not carry either.
    """
    return "x" + hashlib.sha256(asker.encode("utf-8")).hexdigest()[:40]


def _stream(
    package: Package,
    factory: ChatModelFactory,
    identity: RunIdentity,
    message: str,
    *,
    asker: str,
    resume: Resumption | None = None,
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        conversation = Conversation(thread=identity.thread, checkpointer=await get_checkpointer())
        yield _sse(
            "thread",
            {
                "conversation_id": identity.thread.conversation_id,
                "namespace": identity.thread.namespace.value,
                "trace_id": identity.trace_id,
            },
        )
        try:
            async for event in stream_workflow_reply(
                package.workflow,
                message,
                services=RunServices(model_factory=factory, trace=NullTrace()),
                conversation=conversation,
                knowledge=_NoDocuments(),
                databases=reader_for(package, asker),
                sandbox=False,
                identity=identity,
                resume=resume,
            ):
                yield _sse(event["event"], event["data"])
        except Exception:  # noqa: BLE001 — the response has started; an event is the only way left to say so
            _log.exception("Lượt chạy %s gặp lỗi", identity.trace_id)
            yield _sse("error", {"message": _RUN_FAILED, "trace_id": identity.trace_id})

    return StreamingResponse(events(), media_type="text/event-stream")


def _sse(event: str, data: Mapping[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


__all__ = ["ChatRequest", "DecisionRequest", "create_app", "openai_compatible_factory"]
