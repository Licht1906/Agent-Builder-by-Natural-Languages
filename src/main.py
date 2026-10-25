import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from src.agents.memory import close_checkpointer, get_checkpointer
from src.api.admin_routes import router as admin_router
from src.api.agent_routes import router as agent_router
from src.api.approval_routes import creator_router as approval_creator_router
from src.api.approval_routes import router as approval_router
from src.api.auth_routes import router as auth_router
from src.api.connection_routes import router as connection_router
from src.api.control_plane_routes import router as control_plane_router
from src.api.database_source_routes import router as database_source_router
from src.api.deployment_routes import router as deployment_router
from src.api.document_routes import router as document_router
from src.api.eval_routes import router as eval_router
from src.api.external_connection_routes import router as external_connection_router
from src.api.interview_routes import router as interview_router
from src.api.knowledge_base_routes import agent_links_router as knowledge_base_agent_links_router
from src.api.knowledge_base_routes import router as knowledge_base_router
from src.api.metaagent_routes import router as metaagent_router
from src.api.pm_routes import router as pm_router
from src.api.run_feedback_routes import router as run_feedback_router
from src.api.sandbox_routes import router as sandbox_router
from src.api.solution_routes import router as solution_router
from src.api.solution_routes import spec_router as solution_spec_router
from src.api.uc1_action_approval_routes import router as uc1_action_approval_router
from src.api.uc1_agent_chat_routes import available_router as uc1_agent_available_router
from src.api.uc1_agent_chat_routes import router as uc1_agent_chat_router
from src.api.uc1_agent_routes import catalog_router as uc1_agent_catalog_router
from src.api.uc1_agent_routes import router as uc1_agent_router
from src.api.uc1_approval_routes import queue_router as uc1_approval_queue_router
from src.api.uc1_approval_routes import router as uc1_approval_router
from src.api.user_connection_routes import router as user_connection_router
from src.api.workflow_routes import registry_router as node_type_router
from src.api.workflow_routes import router as workflow_router
from src.api.workflow_run_routes import router as workflow_run_router
from src.config import get_settings
from src.db.base import get_db
from src.services.ingestion import recover_interrupted_ingestions
from src.services.llm import PROVIDER_ERROR_TYPES, PROVIDER_REFUSAL_UNKNOWN, provider_failure_of
from src.services.tracing import shutdown_tracing


def _route_platform_logs_to_stderr() -> None:
    """Make `src.*` warnings visible beside uvicorn's own lines.

    Uvicorn configures its own loggers and nothing else; the platform's
    `logging.getLogger(__name__)` calls had no handler, so a warning like
    *"Không soạn được workflow, quay về mẫu gần nhất"* went nowhere — the
    composer fell back to a template three builds in a row and the only way to
    learn why was to re-run the composer by hand. One handler on the `src`
    logger, added once (a reload must not stack them), in uvicorn's format so
    the two streams read as one.
    """
    platform = logging.getLogger("src")
    if any(getattr(handler, "name", "") == "platform-stderr" for handler in platform.handlers):
        return
    handler = logging.StreamHandler()
    handler.name = "platform-stderr"
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    platform.addHandler(handler)
    if platform.level == logging.NOTSET or platform.level > logging.WARNING:
        platform.setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    print(f"Starting {settings.app_name} in {settings.app_env} mode")
    _route_platform_logs_to_stderr()
    # `BackgroundTasks` jobs live entirely inside the process that accepted
    # the request — nothing survives this process having been restarted, so
    # any Document a previous run left mid-pipeline needs to be marked
    # `failed` here rather than silently claim work is still underway
    # (RAG-08 / ADR-0044 §4; see `src.services.ingestion` for why this does
    # not just resume the pipeline on its own).
    recovered = recover_interrupted_ingestions()
    if recovered:
        print(f"Marked {recovered} interrupted ingestion(s) as failed — reingest to resume")
    # Built here, not on the first chat: `setup()` runs LangGraph's migrations,
    # some of which are `CREATE INDEX CONCURRENTLY` — and that waits for every
    # open transaction in the database to end. Lazily, the first request had
    # already opened one (`OwnedVersion`'s SELECT, idle until the response) and
    # then waited on its own transaction forever.
    await get_checkpointer()
    yield
    # The conversation checkpointer holds its own Postgres pool, opened at
    # startup above. Nothing else owns it, so shutdown is where it closes.
    await close_checkpointer()
    # Traces are batched and never flushed during a request (#11) — this is the
    # only place the last batch gets sent, and the only place it is safe to wait.
    await shutdown_tracing()
    print("Shutting down...")


app = FastAPI(
    title="Agent Builder",
    description="AI Agent built with LangGraph",
    version="1.0.0",
    lifespan=lifespan,
)

async def _model_provider_unavailable(_request: Request, exc: Exception) -> JSONResponse:
    """Turn a refusal from the model provider into a sentence, and a 503.

    Every screen in this platform that does anything interesting ends in a model
    call, and until now not one of them could say so when the call failed: the
    provider's exception escaped the whole stack and became
    `500 Internal Server Error` with no `detail`, which the frontend renders as
    *"Yêu cầu thất bại (HTTP 500)"*. Measured, not supposed — a creator who
    confirmed a spec and pressed *Tạo workflow* against a capped OpenAI project
    got exactly that, on an empty canvas, with nothing on screen connecting the
    two.

    **503 rather than 500**, and the difference is the whole point: 500 says the
    platform has a bug, 503 says a dependency is down and the request is worth
    repeating. `Retry-After` is set for the same reason — this is the one failure
    where "press it again in a minute" is real advice rather than a shrug.

    One handler for the whole app rather than a `try` in each route, because
    there is one way to reach a model and there should be one way to report not
    reaching it. Streaming endpoints are the exception it cannot cover: an SSE
    response whose headers have already gone out cannot become a 503, and those
    paths report a failed run through the event stream instead.
    """
    reason = provider_failure_of(exc) or PROVIDER_REFUSAL_UNKNOWN
    # The provider's own words, in English and naming an OpenAI settings URL, are
    # for whoever reads the logs — never for the creator's screen.
    logging.getLogger("src.main").warning("Nhà cung cấp model từ chối: %s: %s", type(exc).__name__, exc)
    return JSONResponse(status_code=503, content={"detail": reason}, headers={"Retry-After": "60"})


async def _validation_error_without_input(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's 422, minus the `input` it would echo back — DS-02's "DSN không xuất hiện trong bất kỳ response".

    The default body repeats what was sent, and for a missing field that is
    the *whole* object: a Hệ CSDL created without a name would have its DSN
    handed straight back. Where an error is and what was wrong with it is
    still said in full; only the caller's own value is left out, for every
    route at once, since any form can carry a secret.
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    detail = [{key: value for key, value in error.items() if key != "input"} for error in errors]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(detail)})


settings = get_settings()
app.add_exception_handler(RequestValidationError, _validation_error_without_input)
for provider_error in PROVIDER_ERROR_TYPES:
    app.add_exception_handler(provider_error, _model_provider_unavailable)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router, prefix="/api/v1")
app.include_router(agent_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(eval_router, prefix="/api/v1")
app.include_router(approval_router, prefix="/api/v1")
app.include_router(approval_creator_router, prefix="/api/v1")
app.include_router(deployment_router, prefix="/api/v1")
app.include_router(document_router, prefix="/api/v1")
app.include_router(interview_router, prefix="/api/v1")
app.include_router(knowledge_base_router, prefix="/api/v1")
app.include_router(knowledge_base_agent_links_router, prefix="/api/v1")
app.include_router(external_connection_router, prefix="/api/v1")
app.include_router(database_source_router, prefix="/api/v1")
app.include_router(connection_router, prefix="/api/v1")
app.include_router(user_connection_router, prefix="/api/v1")
app.include_router(metaagent_router, prefix="/api/v1")
app.include_router(sandbox_router, prefix="/api/v1")
app.include_router(uc1_approval_router, prefix="/api/v1")
app.include_router(uc1_approval_queue_router, prefix="/api/v1")
app.include_router(uc1_action_approval_router, prefix="/api/v1")
app.include_router(uc1_agent_router, prefix="/api/v1")
app.include_router(uc1_agent_catalog_router, prefix="/api/v1")
app.include_router(uc1_agent_available_router, prefix="/api/v1")
app.include_router(uc1_agent_chat_router, prefix="/api/v1")
app.include_router(control_plane_router, prefix="/api/v1")
app.include_router(solution_router, prefix="/api/v1")
app.include_router(solution_spec_router, prefix="/api/v1")
app.include_router(workflow_router, prefix="/api/v1")
app.include_router(workflow_run_router, prefix="/api/v1")
app.include_router(node_type_router, prefix="/api/v1")
app.include_router(pm_router, prefix="/api/v1")
app.include_router(run_feedback_router, prefix="/api/v1")


@app.get("/health")
async def health(db: Session = Depends(get_db)):
    """Liveness check that also reports whether the database is reachable."""
    try:
        db.execute(text("SELECT 1"))
        database_status = "ok"
    except SQLAlchemyError:
        database_status = "error"

    payload = {
        "status": "ok" if database_status == "ok" else "degraded",
        "env": settings.app_env,
        "database": database_status,
    }
    if database_status == "error":
        return JSONResponse(status_code=503, content=payload)
    return payload
