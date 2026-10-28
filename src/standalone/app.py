"""The exported package's HTTP surface — chat, approvals, health, and nothing else (DS-13).

Four API routes, plus the chat page that uses them (DS-17: `/`, `/assets`,
`/ui-config`). No Designer, Builder, admin or PM route is mounted, and none
is importable from here: the platform's routers are never imported at all, so
the package cannot grow one by accident (`test_standalone_runtime` lists the
routes).

**Who is asking** comes from `runtime.yaml`'s `auth` — `trusted_header`,
`jwt`, `oidc` or `custom`, all reduced to one value or a 401 by
`src.standalone.auth` (DS-14). A request that names nobody never falls back
to a default asker.

**Approvals are decided by the person who asked** (ADR-0059 §7), so the
approval routes only ever show and decide the caller's own pauses.

**An approved mail leaves through `runtime.yaml`'s `mail`** (DS-16): the
channel stands in for the `gmail` connection's implementation and nothing
else, so the approval and its payload binding are the platform runtime's.
"""

import hashlib
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
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
from src.standalone.auth import build_authenticator
from src.standalone.config import LlmConfig
from src.standalone.mail import build_mail_sender, mail_invoker
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
from src.workflow.tools import GMAIL, ToolInvoker

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


async def _asker(request: Request) -> str:
    """Who is asking, by `runtime.yaml`'s `auth` — or a 401. Never a default asker (DS-14)."""
    asker = await request.app.state.authenticate(request)
    if asker is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Chưa xác thực được người hỏi",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return asker


def _ids(request: Request) -> PackageIds:
    return request.app.state.ids


Asker = Annotated[str, Depends(_asker)]
Ids = Annotated[PackageIds, Depends(_ids)]


def create_app(
    package: Package,
    *,
    model_factory: ChatModelFactory | None = None,
    auth_transport: httpx.BaseTransport | None = None,
    mail_transport: httpx.BaseTransport | None = None,
    ui_dir: Path | None = None,
) -> FastAPI:
    """The package as an ASGI app.

    `model_factory`, `auth_transport` (an OIDC issuer) and `mail_transport`
    (Google, for `gmail_oauth`) are test seams; `ui_dir` is the chat page's
    build (`default_ui_dir` otherwise).
    """
    authenticate = build_authenticator(
        package.config.auth, secrets=package.secrets, base=package.base, transport=auth_transport
    )
    # The bundle's `gmail.send_email` leaves through the company's channel (DS-16); the approval in front stays.
    invokers: dict[str, ToolInvoker] = {
        GMAIL: mail_invoker(
            build_mail_sender(package.config.mail, secrets=package.secrets, base=package.base, transport=mail_transport)
        )
    }
    llm_key = package.secrets.get(package.config.llm.api_key_secret or "")
    factory = model_factory or openai_compatible_factory(package.config.llm, llm_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ids = prepare_database(package)
        for warning in package.warnings:
            _log.warning(warning)
        await get_checkpointer()
        yield
        await close_checkpointer()

    app = FastAPI(title=f"{package.name} — gói agent", lifespan=lifespan)
    app.state.authenticate = authenticate
    _serve_ui(app, package, ui_dir if ui_dir is not None else default_ui_dir())

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
        return _stream(package, factory, invokers, identity, payload.message, asker=who)

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
        return _stream(
            package, factory, invokers, leg.identity, leg.input_message, asker=leg.run_as or who, resume=resumption
        )

    return app


def default_ui_dir() -> Path:
    """Where the chat page's build is: `AGENT_UI_DIR` (the image sets it), else the repo's `frontend/dist-package`."""
    configured = os.environ.get("AGENT_UI_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "frontend" / "dist-package"


def ui_config(package: Package) -> dict[str, Any]:
    """What the chat page needs to sign someone in — names and URLs, never a secret or a header the gateway sets."""
    auth = package.config.auth
    described: dict[str, Any] = {"mode": auth.mode}
    if auth.mode == "oidc" and auth.oidc is not None:
        described["oidc"] = {"issuer": auth.oidc.issuer, "client_id": auth.oidc.client_id, "scope": auth.oidc.scope}
    return {"agent": package.name, "auth": described}


def _serve_ui(app: FastAPI, package: Package, ui_dir: Path) -> None:
    """The chat page (DS-17) at `/`, its assets at `/assets`, and `/ui-config` — three routes, no API of their own.

    Without a build (`npm run build:package`) the page answers 404 saying so;
    the API still works. The page's headers keep the OIDC code out of any
    `Referer`, the page out of frames, and every script and request on this
    origin — plus the issuer the page signs in with, the one other place it
    may talk to (a company's network may have nothing else to reach, and the
    platform's web fonts are deliberately not among them).
    """
    index = ui_dir / "index.html"
    issuer = package.config.auth.oidc.issuer if package.config.auth.mode == "oidc" and package.config.auth.oidc else ""
    issuer_origin = "{0.scheme}://{0.netloc}".format(urlsplit(issuer)) if issuer else ""
    headers = {
        "Cache-Control": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            f"connect-src 'self' {issuer_origin}".rstrip()
            + "; frame-ancestors 'none'; base-uri 'none'"
        ),
    }

    @app.get("/ui-config")
    def describe() -> dict[str, Any]:
        return ui_config(package)

    @app.get("/", include_in_schema=False)
    def page() -> FileResponse:
        if not index.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Gói này không kèm giao diện chat (chưa build frontend/dist-package) — API /chat vẫn dùng được",
            )
        return FileResponse(index, headers=headers)

    if (ui_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=ui_dir / "assets"), name="assets")


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
    invokers: Mapping[str, ToolInvoker],
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
                invokers=invokers,
            ):
                yield _sse(event["event"], event["data"])
        except Exception:  # noqa: BLE001 — the response has started; an event is the only way left to say so
            _log.exception("Lượt chạy %s gặp lỗi", identity.trace_id)
            yield _sse("error", {"message": _RUN_FAILED, "trace_id": identity.trace_id})

    return StreamingResponse(events(), media_type="text/event-stream")


def _sse(event: str, data: Mapping[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


__all__ = ["ChatRequest", "DecisionRequest", "create_app", "openai_compatible_factory"]
