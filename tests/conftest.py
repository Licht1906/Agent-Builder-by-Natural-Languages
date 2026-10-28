import io
import json
import re
import smtplib
from collections.abc import Callable, Generator, Iterator, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

# ruff: noqa: E402 — the redirect below has to run before `src.db.base` binds an
# engine, so the `src` imports cannot come first. See `tests.db_target`.
from tests.db_target import redirect_to_test_database, require_disposable

#: Where this run's tables live. Set before anything imports `src.db.base`,
#: because that module builds its engine at import time and this fixture file
#: drops every table on it — against the development database, that is not a
#: test run, it is data loss that reports as green.
TEST_DATABASE_URL = redirect_to_test_database()

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langgraph.checkpoint.postgres import PostgresSaver
from pydantic import PrivateAttr
from qdrant_client import QdrantClient
from sqlalchemy import insert, inspect, text, update
from sqlalchemy.orm import Session

from src.agents.runtime import get_clock
from src.agents.versions import create_draft, mark_sandbox_passed
from src.auth.passwords import hash_password
from src.auth.seed import seed_agent_admin_row, seed_pm_row, seed_user_rows
from src.config import get_settings
from src.db.base import Base, SessionLocal, engine
from src.db.models import (
    DEFAULT_EVAL_PASS_THRESHOLD,
    Agent,
    AgentVersion,
    Deployment,
    DeploymentAccessMode,
    PlatformModelCall,
    SandboxRun,
    SandboxRunStatus,
    SystemConfig,
    User,
    UserRole,
    VersionStatus,
    default_system_config_row,
)
from src.deployments.keys import hash_api_key
from src.deployments.rate_limit import SlidingWindowLimiter
from src.evals.results import store_run
from src.main import app
from src.models.agent_spec import AgentSpec
from src.models.examples import load_example_spec
from src.models.schemas import EvalCaseResult, SandboxTranscriptEntry
from src.services.embedding import EXPECTED_DIMENSION, get_embedding_client
from src.services.extraction import ExtractedBlock, ExtractionResult, get_extraction_cache, get_vlm_page_client
from src.services.llm import get_chat_model_factory
from src.services.qdrant import ensure_collection, get_qdrant_client
from src.services.reranker import RerankResult, get_reranker_client
from src.services.secret_store import EncryptedDbSecretStore
from src.services.tracing import EventTrace, RecordedRun, get_tracer
from src.services.upload_rate_limit import get_upload_rate_limiter
from src.workflow.knowledge import DEFAULT_PASSAGE_LIMIT, QdrantKnowledgeStore
from src.workflow.tools import GMAIL

#: Tables this project does not own, so they are absent from `Base.metadata` and
#: `drop_all` neither knows nor drops them. Dropped by name instead, so a run
#: never inherits threads from the run before it (ticket #6).
#:
#: `studio_messages` is LangGraph Studio's and carries a foreign key onto
#: `agent_versions`, which is why the order below matters: a table outside the
#: metadata can still *depend* on one inside it, and Postgres refuses to drop the
#: dependency while the dependant stands. Left until after `drop_all`, as these
#: once were, that single row of DDL takes the whole suite down with a
#: `DependentObjectsStillExist` in a fixture — every test erroring at teardown
#: for a reason none of them are about.
#: `document_chunks` is not foreign, it is *former* — the pgvector retrieval
#: index ADR-0065 dropped. It is listed here only so a checkout that ran the
#: suite before that change does not fail every test at teardown: the table is
#: still standing in that developer's own test database, it holds a foreign key
#: onto `users`, and `drop_all` no longer knows it exists to drop it. Safe to
#: delete from this tuple once no working copy predates ADR-0065. The `uc1_*`
#: tables are former for the same reason since DS-21 dropped the UC-1 pipeline:
#: each holds a foreign key onto `users` in a test database that predates it.
_FOREIGN_TABLES = (
    "document_chunks",
    "uc1_pending_actions",
    "uc1_version_approvals",
    "uc1_eval_runs",
    "uc1_sandbox_runs",
    "uc1_agent_spec_versions",
    "uc1_connections",
    "studio_messages",
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
    "checkpoint_migrations",
)

#: `docs/usecase_spec/uc1-ar-assistant/schema.sql` — UC1-32 (#144)'s `audit_log`
#: lives here, not in `src.db.models`/`Base.metadata` (it is a schema.sql-owned
#: object, applied for real only by migration `a4c2c2363959_uc1_ar_data_surface`,
#: same as `crm_sales_assignment`/`fin_invoice`/the two least-privilege views).
#: The UC-1 pipeline that wrote to it is gone (DS-21); the demo ERP surface it
#: read stays, so the suite still builds it the way the migration does.
_SCHEMA_SQL_PATH = Path(__file__).resolve().parent.parent / "docs" / "usecase_spec" / "uc1-ar-assistant" / "schema.sql"

#: schema.sql's own FKs (`fin_invoice.order_id`, `crm_sales_assignment.customer_id`)
#: assume `orders`/`customers` already exist — migration `4d604eb640fb_olist_base_tables`'s
#: reason for existing. Reproduced here rather than imported (that migration's
#: `upgrade()` calls Alembic's `op.*`, which needs a real `MigrationContext`, not
#: worth wiring up for two small, stable tables) so this fast, session-scoped
#: reset can create them too.
_OLIST_BASE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id text PRIMARY KEY,
    customer_unique_id text NOT NULL,
    customer_zip_code_prefix text,
    customer_city text NOT NULL,
    customer_state text NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id text PRIMARY KEY,
    customer_id text NOT NULL REFERENCES customers (customer_id),
    order_status text NOT NULL,
    order_purchase_timestamp timestamptz NOT NULL,
    order_approved_at timestamptz,
    order_delivered_carrier_date timestamptz,
    order_delivered_customer_date timestamptz,
    order_estimated_delivery_date timestamptz
);
CREATE INDEX IF NOT EXISTS ix_orders_customer_id ON orders (customer_id);
"""

#: Dropped and rebuilt every session alongside `Base.metadata`'s own tables —
#: schema.sql's objects are otherwise idempotent to re-run (`CREATE TABLE IF
#: NOT EXISTS`/`CREATE OR REPLACE VIEW`), but a genuinely clean slate matches
#: this fixture's own "clean schema... for every test run" promise, and
#: `audit_log`'s hash chain in particular must start empty every session.
_SCHEMA_SQL_TABLES = ("audit_log", "fin_invoice", "crm_sales_assignment", "orders", "customers")


@pytest.fixture(scope="session", autouse=True)
def _fresh_postgres_schema() -> None:
    """A clean schema on the **test** Postgres for every test run.

    No SQLite fallback (see issue #3): tests run against the same engine as
    production, just reset instead of migrated, so the schema is always in
    sync with the current ORM models — and, since UC1-32 (#144), with
    schema.sql's own non-ORM objects too.

    "The same engine" once meant the same *database* too, and that was the bug.
    This fixture drops every table; pointed at `app_dev` it erases whatever the
    developer was building, and reports a green run while doing it. `db_target`
    redirects the URL long before this runs — and the check below is here, one
    statement before the `DROP`, because the engine that matters is the one
    actually bound now rather than the one configured at startup.
    """
    require_disposable(str(engine.url))
    with engine.begin() as connection:
        # `document_chunks` has a `vector` column (ADR-0045), so the type has to
        # exist before `create_all` can name it. The migration does this for a
        # real database; this fixture is the migrations' stand-in here, so it
        # owns the same statement. Fails loudly on a Postgres image without
        # pgvector, which is the right moment to find out — see docker-compose.
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        for table in _FOREIGN_TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        for table in _SCHEMA_SQL_TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        # The checkpoint tables `_FOREIGN_TABLES` already dropped above cover
        # what UC-1's own `_CHECKPOINTER_TABLES` would have — one drop, not two.
        connection.execute(text(_OLIST_BASE_TABLES_SQL))
        connection.execute(text(_SCHEMA_SQL_PATH.read_text(encoding="utf-8")))
        connection.execute(insert(SystemConfig).values(**default_system_config_row()))
        # Same seed rows the migrations write (ticket #4, #117), so tests log
        # in with the same accounts a freshly migrated database has.
        connection.execute(insert(User), seed_user_rows())
        connection.execute(insert(User).values(**seed_agent_admin_row()))
        connection.execute(insert(User).values(**seed_pm_row()))
    # The checkpoint tables were dropped above, so their migrations run again —
    # here, while nothing else holds a transaction, and not lazily in the first
    # test that reaches `get_checkpointer`. Some of them are `CREATE INDEX
    # CONCURRENTLY`, which waits for every open transaction in the database; a
    # test's own `db_session` is one, so lazily the suite hung on itself. The
    # same reason `src.main.lifespan` builds the checkpointer at startup.
    with PostgresSaver.from_conn_string(
        engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    ) as checkpointer:
        checkpointer.setup()


@pytest_asyncio.fixture
async def client():
    """Async HTTP client for testing API endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth_header(token: str) -> dict[str, str]:
    """The `Authorization` header a client sends once it holds `token`."""
    return {"Authorization": f"Bearer {token}"}


def user_id_of(token: str) -> int:
    """The `sub` claim of an already-issued token — cheaper than a lookup call
    for a test fixture's own bookkeeping (e.g. `created_by_id` on a seeded row).
    """
    import jwt as _jwt

    claims = _jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    return int(claims["sub"])


def seeded_emails() -> list[str]:
    """The accounts `_fresh_postgres_schema` seeds — never a row a test's own cleanup should delete."""
    settings = get_settings()
    return [
        settings.seed_creator_email,
        settings.seed_admin_email,
        settings.seed_agent_admin_email,
        settings.seed_pm_email,
    ]


@pytest_asyncio.fixture
async def creator_token(client: AsyncClient) -> str:
    """A live token for the seeded creator, obtained the way a client obtains one."""
    settings = get_settings()
    return await _login(client, settings.seed_creator_email, settings.seed_creator_password)


@pytest_asyncio.fixture
async def admin_token(client: AsyncClient) -> str:
    """A live token for the seeded (V2) admin."""
    settings = get_settings()
    return await _login(client, settings.seed_admin_email, settings.seed_admin_password)


@pytest_asyncio.fixture
async def agent_admin_token(client: AsyncClient) -> str:
    """A live token for the seeded UC1-01 (#117) `agent_admin` — FR-03's bootstrap account."""
    settings = get_settings()
    return await _login(client, settings.seed_agent_admin_email, settings.seed_agent_admin_password)


@pytest_asyncio.fixture
async def pm_token(client: AsyncClient) -> str:
    """A live token for the seeded `pm` account (ADR-0051) — the PM control room's own login, not an admin."""
    settings = get_settings()
    return await _login(client, settings.seed_pm_email, settings.seed_pm_password)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """A session for *arranging* rows a test needs. Assertions still go through HTTP."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def default_platform_config(db_session: Session) -> Generator[None, None, None]:
    """Put `system_config` back the way the migration seeds it, whatever the test did to it.

    The row is platform-wide and there is exactly one of it, so a test that
    narrows the allowlist would otherwise narrow it for every test that runs
    afterwards — and the symptom would be a spec rejected in a suite that never
    mentions configuration. Declare it with
    `pytestmark = pytest.mark.usefixtures("default_platform_config")` in any
    module that changes the config (#12).
    """
    yield
    defaults = default_system_config_row()
    db_session.execute(
        update(SystemConfig)
        .where(SystemConfig.id == defaults["id"])
        .values(
            eval_pass_threshold=defaults["eval_pass_threshold"],
            model_allowlist=defaults["model_allowlist"],
        )
    )
    db_session.commit()


#: The key every fixture-published Deployment answers to (#50). A fixed string
#: rather than a minted one because the plaintext of a real key exists only in
#: the response that created it — a suite testing the API channel needs a key it
#: can hold, and `hash_api_key` is how the row gets a matching hash.
DEPLOYMENT_API_KEY = "dpl_test-deployment-key"

#: The other creator's credentials, in one place because #15 gave them a second
#: reader: `second_creator` arranges the account, `second_creator_token` signs in
#: as it. A password written out twice is a fixture pair that silently stops
#: being about the same person.
SECOND_CREATOR_EMAIL = "other-creator@example.com"
SECOND_CREATOR_PASSWORD = "other-creator-password"


@pytest.fixture
def second_creator(db_session: Session) -> Generator[User, None, None]:
    """Another creator, so "only your own agents" has someone to be shielded from."""
    user = User(
        email=SECOND_CREATOR_EMAIL,
        password_hash=hash_password(SECOND_CREATOR_PASSWORD),
        role=UserRole.CREATOR,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    yield user
    db_session.delete(user)
    db_session.commit()


@pytest_asyncio.fixture
async def second_creator_token(client: AsyncClient, second_creator: User) -> str:
    """A live token for `second_creator` — somebody who is *not* the owner, holding a real session.

    What #15 needs that #4 did not: "bất kỳ ai trong platform cũng mở được cửa sổ
    chat" is a claim about a signed-in stranger, and a test can only make it with
    a stranger's token in hand.
    """
    return await _login(client, SECOND_CREATOR_EMAIL, SECOND_CREATOR_PASSWORD)


@pytest.fixture
def mock_llm():
    """Mock LLM to avoid calling OpenAI during tests.

    Usage in test:
        def test_something(mock_llm):
            # LLM calls will return mock response instead of hitting OpenAI
            ...
    """
    mock = AsyncMock()
    mock.ainvoke.return_value = AsyncMock(content="Mocked LLM response")
    return mock


@pytest.fixture
def example_agent_spec() -> AgentSpec:
    """The handwritten AgentSpec — shared starting fixture for both teams.

    Use it as the input wherever a test needs a valid, executable spec instead of
    hand-rolling one: runtime, sandbox, versioning, approval.
    """
    return load_example_spec("complaint_handler")


# --------------------------------------------------------------------------- #
# Chatting with an agent — shared by the runtime (#5) and memory (#6) suites
# --------------------------------------------------------------------------- #


#: Roomy enough that no test hits it by accident, so a run that stops on its
#: budget is a test that asked for it (#10). A test about enforcement passes its
#: own `budget` instead.
_UNRESTRICTIVE_BUDGET = {"max_total_tokens": 20000, "max_wall_seconds": 60}


def agent_spec(
    *,
    steps: list[dict[str, Any]],
    allowed_domains: list[str] | None = None,
    budget: dict[str, Any] | None = None,
    success_criteria: list[str] | None = None,
) -> AgentSpec:
    """A valid `AgentSpec` wrapped around whatever steps a test cares about.

    `allowed_domains` has to be passed in rather than set afterwards: a step
    that uses `http_request` without declared domains is not a valid spec at
    all (ADR-0001), so there is no valid object to set it on later.

    `success_criteria` is the slot eval (#13) generates its test set from, so a
    test about eval names its criteria here and the rest of the suite ignores it.
    """
    return AgentSpec.model_validate(
        {
            "name": "Agent test",
            "description": "Agent dùng riêng trong test runtime.",
            "slots": {
                "audience": "người kiểm thử",
                "inputs": "yêu cầu giả lập trong test",
                "capabilities": [],
                "success_criteria": success_criteria or [],
                "prohibitions": [],
            },
            "steps": steps,
            "guardrails": [],
            "budget": budget or _UNRESTRICTIVE_BUDGET,
            "allowed_domains": allowed_domains or [],
        }
    )


def single_step_spec() -> AgentSpec:
    """A minimal one-step, no-tool spec — for tests where the step's content doesn't matter."""
    return agent_spec(
        steps=[
            {
                "id": "s",
                "title": "S",
                "prompt": "p",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            }
        ]
    )


# --------------------------------------------------------------------------- #
# Spec generation (#17) — what the meta-agent is scripted to write
# --------------------------------------------------------------------------- #


def generated_spec_document(**overrides: Any) -> dict[str, Any]:
    """The JSON document the meta-agent returns — a *valid* one unless overridden.

    Shared by the generator's own suite and the HTTP one, because a spec literal
    written twice is two specs that drift: the day `AgentSpec` gains a required
    field, only one of them would be updated and the other suite would start
    proving that regeneration works using a document nobody meant to be invalid.

    Deliberately the shape the ticket is about — a cheap model on the extraction
    step, a strong one on the step that holds a tool.
    """
    document: dict[str, Any] = {
        "name": "Trợ lý xử lý khiếu nại",
        "description": "Đọc email khiếu nại, tra đơn hàng, soạn thư trả lời.",
        "steps": [
            {
                "id": "trich-xuat",
                "title": "Trích xuất khiếu nại",
                "prompt": "Trích mã đơn hàng và loại khiếu nại từ email.",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            },
            {
                "id": "tra-cuu",
                "title": "Tra cứu đơn hàng",
                "prompt": "Tra trạng thái đơn hàng bằng tool chỉ đọc.",
                "model": "gpt-4.1",
                "tools": ["db_query"],
                "max_iterations": 3,
                "next": None,
            },
        ],
        "guardrails": [],
        "budget": {"max_total_tokens": 60000, "max_wall_seconds": 120},
        "allowed_domains": [],
    }
    return {**document, **overrides}


def writes_spec(**overrides: Any) -> AIMessage:
    """One generation reply: the spec the meta-agent wrote, as JSON."""
    return writes_json(generated_spec_document(**overrides))


#: The slots a refinable spec carries. Written out rather than reused from
#: `generated_spec_document` (which has none — the generator's reply is stamped
#: with the interview's) because a *saved* spec always has them, and #18's
#: claim is precisely that a refinement turn cannot move them.
REFINABLE_SLOTS: dict[str, Any] = {
    "audience": "Nhân viên chăm sóc khách hàng",
    "inputs": "Email khiếu nại khách gửi vào hộp thư hỗ trợ",
    "capabilities": ["Tra cứu đơn hàng trong ERP", "Soạn thư trả lời khách"],
    "success_criteria": ["Thư nêu đúng mã đơn hàng khách nhắc tới", "Thư nêu đúng ngày giao dự kiến"],
    "prohibitions": ["Không tự ý hứa hoàn tiền", "Không gửi thư cho khách khi chưa được duyệt"],
}


def refinable_spec_document(**overrides: Any) -> dict[str, Any]:
    """A saved agent's spec as a refinement (#18) finds it: three steps, one of them with a tool.

    Three rather than two because every claim #18 makes is about *locality* —
    "bỏ bước 3", "bước 2 thêm quyền" — and a two-step spec cannot tell a change
    that stayed put from one that rewrote everything: with one step changed there
    is only one other step left to check, and it is next to the change.

    Shared with the HTTP suite for the same reason `generated_spec_document` is:
    the assertions compare a refined spec against *this* document field by field,
    and two copies of it would be two starting points that drift apart.
    """
    document: dict[str, Any] = {
        "name": "Trợ lý xử lý khiếu nại",
        "description": "Đọc email khiếu nại, tra đơn hàng, soạn thư trả lời.",
        "slots": deepcopy(REFINABLE_SLOTS),
        "steps": [
            {
                "id": "doc-email",
                "title": "Đọc email",
                "prompt": "Trích mã đơn hàng và loại khiếu nại từ email.",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            },
            {
                "id": "tra-cuu",
                "title": "Tra cứu đơn hàng",
                "prompt": "Tra trạng thái đơn hàng bằng tool chỉ đọc.",
                "model": "gpt-4.1",
                "tools": ["db_query"],
                "max_iterations": 3,
                "next": None,
            },
            {
                "id": "soan-thu",
                "title": "Soạn thư trả lời",
                "prompt": "Soạn thư trả lời khách dựa trên kết quả tra cứu.",
                "model": "gpt-4.1",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            },
        ],
        "guardrails": list(REFINABLE_SLOTS["prohibitions"]),
        "budget": {"max_total_tokens": 60000, "max_wall_seconds": 120},
        "allowed_domains": [],
    }
    return {**document, **overrides}


def refinable_spec_without(step_id: str) -> dict[str, Any]:
    """`refinable_spec_document` with one step removed and nothing else touched.

    What a well-behaved refiner replies with to *"bỏ bước 3"*. Built from the same
    document the spec under test came from, so every step the request did not name
    is identical by construction — the assertions then measure the code rather
    than a literal somebody retyped.
    """
    document = refinable_spec_document()
    document["steps"] = [step for step in document["steps"] if step["id"] != step_id]
    return document


def refinable_spec_replacing(step_id: str, **fields: Any) -> dict[str, Any]:
    """`refinable_spec_document` with `fields` written over one step — a *local* edit."""
    document = refinable_spec_document()
    document["steps"] = [{**step, **fields} if step["id"] == step_id else step for step in document["steps"]]
    return document


def writes_json(document: dict[str, Any]) -> AIMessage:
    """One meta-agent reply carrying `document` — whatever document the test means.

    Beside `writes_spec` rather than inside it because refinement (#18) answers
    with a spec that is a *modified copy of an existing one*, not a variation on
    the generator's example: overriding fields of `generated_spec_document` would
    describe the wrong starting point.
    """
    return AIMessage(content=json.dumps(document, ensure_ascii=False))


class ScriptedMetaAgent:
    """A `ChatModelFactory` that answers with `replies` in order, and remembers everything.

    Shared by the two meta-agent suites — generation (#17) and refinement (#18) —
    because both make the same claim about the same loop: that an invalid reply is
    *fed back into* the next attempt. The only place that claim can be checked is
    the text of the second prompt, which is what `prompts` exposes.

    One model for every call, unlike `scripted_chat_model_factory`'s one-per-step:
    a meta-agent call is single-shot and its retries go to the same model, so the
    replies are a queue rather than a script per step.
    """

    def __init__(self, *replies: AIMessage) -> None:
        self.replies = list(replies)
        self.requested_model_ids: list[str] = []
        self._model = ScriptedToolModel(responses=self.replies)

    def __call__(self, model_id: str) -> "ScriptedToolModel":
        self.requested_model_ids.append(model_id)
        return self._model

    @property
    def prompts(self) -> list[str]:
        return [str(batch[0].content) for batch in self._model.received_message_batches]


def parse_sse(body: str, *, event: str | None = None) -> list[dict[str, Any]]:
    """The `data:` payloads of an SSE response, in order; `event` keeps only that event type.

    Filtering by the event's own name rather than by what its payload happens to
    contain: a test that asks for `step` events must not start or stop matching
    because some other event's fields changed.
    """
    return [payload for name, payload in parse_sse_stream(body) if event is None or name == event]


def parse_sse_stream(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Every event of an SSE response as `(event name, payload)`, in arrival order.

    `parse_sse` answers "what did event X say"; this answers "in what order did
    the events arrive", which is the only way to assert that a tool call is
    announced *before* the step that made it completes (#8).
    """
    stream: list[tuple[str, dict[str, Any]]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        name = next((line[len("event: ") :] for line in lines if line.startswith("event: ")), "")
        data_line = next(line for line in lines if line.startswith("data: "))
        stream.append((name, json.loads(data_line[len("data: ") :])))
    return stream


async def chat_with_agent(client: AsyncClient, agent: Agent, message: str, token: str, **thread: Any) -> Any:
    """Send one chat turn. `thread` carries `conversation_id` / `namespace` when a test names them."""
    return await client.post(
        f"/api/v1/agents/{agent.id}/chat",
        json={"message": message, **thread},
        headers=auth_header(token),
    )


async def trial_run_version(
    client: AsyncClient, agent: Agent, version: AgentVersion, message: str, token: str, **thread: Any
) -> Any:
    """Start a trial run of `version`. `thread` carries `conversation_id` when a test names one.

    Shared rather than per-file because three suites now start trial runs — the
    sandbox's own (#9), the budget's (#10), and eval's when it lands (#13) — and
    the route is one thing to know in one place.
    """
    return await client.post(
        f"/api/v1/agents/{agent.id}/versions/{version.id}/sandbox-runs",
        json={"message": message, **thread},
        headers=auth_header(token),
    )


async def read_thread(client: AsyncClient, agent: Agent, conversation_id: str, token: str, **namespace: str) -> Any:
    """Read one thread's history back. `namespace` selects `trial` when a test wants it."""
    return await client.get(
        f"/api/v1/agents/{agent.id}/threads/{conversation_id}",
        params=namespace,
        headers=auth_header(token),
    )


class RealToolReached(BaseException):
    """The real implementation of a side-effecting tool ran inside a sandboxed run.

    A `BaseException` and **not** an `Exception`, which is the whole point. The
    runtime catches `Exception` from a tool call on purpose (ADR-0003 §4: a
    failed tool is handed back to the model, not fatal), so an ordinary
    `AssertionError` raised here is swallowed, the run carries on, and the test
    passes while the email is being sent. Only something outside that net can
    make a breach visible from a test that does not read tool events.
    """


@pytest.fixture
def smtp_that_must_never_be_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the *real* `send_email` fully live — and fatal if anything calls it.

    Without the host, the real implementation refuses early with
    `ToolUnavailableError` and a passing test would prove only that SMTP was
    unconfigured. With it, the only thing standing between a run and a real email
    is the sandbox flag.

    Shared rather than owned by the sandbox suite because eval (#13) makes the
    same claim about its own runs, and a second copy of this fixture is a second
    thing to keep true.
    """
    monkeypatch.setattr(get_settings(), "smtp_host", "smtp.example.invalid")

    def _explode(*args: object, **kwargs: object) -> None:
        raise RealToolReached("Bản thật của send_email đã chạy trong sandbox — đây là sự cố, không phải lỗi test")

    monkeypatch.setattr(smtplib, "SMTP", _explode)


@pytest.fixture(autouse=True)
def _no_live_search_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches a real search provider unless it asks to.

    `get_settings()` reads the developer's own `.env`, so the moment a real
    `SEARCH_API_KEY` is set there the suite starts spending credits, needs the
    network to pass, and — the reason this is a fixture rather than a note —
    silently inverts `test_a_web_search_node_runs_for_real_and_says_so`, whose
    entire claim is about what an *unconfigured* provider does. That test went
    green-to-red on a machine where nothing about the code had changed.

    Autouse and blanking, so the safe state is the default and opting in is
    explicit: `a_configured_search_api` sets a key back and stubs the wire
    underneath it, which is the only way a test should ever see one.
    """
    monkeypatch.setattr(get_settings(), "search_api_key", "")


@pytest.fixture(autouse=True)
def _no_live_embedding_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test embeds against a real provider unless it builds one itself.

    Same argument as `_no_live_search_provider`, and the same failure it
    prevents — `get_settings()` reads the developer's own `.env`, so a real key
    there would make every `KnowledgeSearch` test embed for real: network
    required to pass, tokens billed per assertion, and a suite that goes red the
    day the key does.

    The **model list** is what gets blanked, not the keys, and that is the point
    of doing it here rather than per provider: which provider a model belongs to
    is read off its name (`src.workflow.embeddings.embedder_for`), so blanking
    one key would only redirect the suite at the other.

    What stands in for the provider is `_no_live_ingestion_backends`, which
    overrides `get_embedding_client` outright: search reaches Qdrant either
    way, and the vectors there came from a fake rather than from nothing.
    """
    monkeypatch.setattr(get_settings(), "embedding_model", "")


# --------------------------------------------------------------------------- #
# Ingestion (RAG-08) — every Document upload now queues a background pipeline
# --------------------------------------------------------------------------- #


def _fake_dense_embed(texts: Sequence[str]) -> list[list[float]]:
    return [[0.1] * EXPECTED_DIMENSION for _ in texts]


def _refusing_vlm_client(image_bytes: bytes, page_number: int) -> list[ExtractedBlock]:
    raise AssertionError(
        "the extraction VLM ran during a test that did not ask for it — "
        "a scanned/photographed PDF needs its own fixture that overrides get_vlm_page_client"
    )


class _InMemoryExtractionCache:
    """A per-test `ExtractionCache` — real behaviour, no bytes on the developer's own disk.

    The real `FileExtractionCache` writes under `get_settings().extraction_cache_dir`,
    a real path with no test-scoped `tmp_path` behind it (unlike
    `tool_file_base_dir`, which `test_documents.py`'s own `workspace` fixture
    already redirects per test). Overriding the DI seam instead of the
    setting keeps this suite from leaving cache files on disk at all.
    """

    def __init__(self) -> None:
        self._store: dict[str, ExtractionResult] = {}

    def get(self, digest: str) -> ExtractionResult | None:
        return self._store.get(digest)

    def set(self, digest: str, result: ExtractionResult) -> None:
        self._store[digest] = result


@pytest.fixture(autouse=True)
def _no_live_ingestion_backends() -> Generator[QdrantClient, None, None]:
    """No test's Document upload reaches a real embedding provider, Qdrant, or extraction VLM.

    `POST .../documents` and `POST .../documents/{id}/reingest` both queue
    `ingest_document` as a `BackgroundTasks` job (RAG-08), and — the reason
    this fixture exists — a `BackgroundTasks` job attached to a request
    finishes before `AsyncClient.post(...)` running against `ASGITransport`
    returns: the response body is captured before the task runs, but the
    task itself is not still pending in the background once a test's `await
    client.post(...)` call gets its result back. Without this fixture,
    *every* existing document-upload test — none of which know RAG-08
    exists — would try to reach a real embedding model and a real Qdrant on
    every upload, the same failure mode `_no_live_search_provider` above
    guards against for search.

    A real, request-scoped in-memory Qdrant (`ensure_collection` run once)
    rather than a mock: the point is to prove ingestion's own write path
    works, not to assume it does. The embedding vectors are fixed rather
    than meaningful, because no test in the default suite reads relevance
    out of them — a test that wants to (hybrid retrieval, RAG-10) overrides
    this fixture's dependencies itself the same way `a_configured_search_api`
    opts back into a real search provider.

    Yields the client itself, so a test that wants to look at what ingestion
    actually wrote (`tests/test_api/test_documents.py`'s RAG-08 section) can
    take this fixture by name instead of standing up a second one.
    """
    qdrant_client = QdrantClient(":memory:")
    ensure_collection(qdrant_client)
    app.dependency_overrides[get_embedding_client] = lambda: _fake_dense_embed
    app.dependency_overrides[get_qdrant_client] = lambda: qdrant_client
    app.dependency_overrides[get_vlm_page_client] = lambda: _refusing_vlm_client
    app.dependency_overrides[get_extraction_cache] = lambda: _InMemoryExtractionCache()
    yield qdrant_client
    app.dependency_overrides.pop(get_embedding_client, None)
    app.dependency_overrides.pop(get_qdrant_client, None)
    app.dependency_overrides.pop(get_vlm_page_client, None)
    app.dependency_overrides.pop(get_extraction_cache, None)


#: What `_overlap_rerank` counts as a term. Unicode `\w`, keeping diacritics —
#: the corpus and the questions in this suite are both Vietnamese, and "hoàn"
#: and "hoan" are genuinely different words there.
_RERANK_TERMS = re.compile(r"\w+", re.UNICODE)


def _overlap_rerank(query: str, documents: Sequence[str]) -> RerankResult:
    """A deterministic stand-in for `bge-reranker-v2-m3`: rank by term overlap.

    The real reranker is a 2.2 GB cross-encoder behind an HTTP service, so a
    suite that used it would need the service up to go green. But it cannot
    just be *skipped* either: `QdrantKnowledgeStore` gives the reranker the
    final say over both order and the score threshold, so a fake that returned
    the fused order unchanged would leave every "the right passage came back"
    assertion resting on `_fake_dense_embed`'s constant vector — which encodes
    no relevance at all, and would pass whatever the retrieval did.

    Overlap is crude next to a cross-encoder and deliberately so: what these
    tests own is the *wiring* — that a question reaches the shelf it was
    pointed at, that the passage carrying the answer is the one handed to the
    model, and that a shelf with nothing on topic yields nothing. Retrieval
    quality itself is measured against real documents in `eval/`, not here.

    A zero-overlap document scores 0.0, so it falls below the default
    `knowledge_rerank_score_threshold` and is dropped — which is what lets a
    search over a shelf that does not cover the question come back empty
    rather than handing the model an unrelated passage to answer from.
    """
    terms = set(_RERANK_TERMS.findall(query.lower()))
    scores = [
        len(terms & set(_RERANK_TERMS.findall(document.lower()))) / (len(terms) or 1) for document in documents
    ]
    ranked_indices = sorted(range(len(documents)), key=lambda i: -scores[i])
    return RerankResult(ranked_indices=ranked_indices, scores=scores, reranked=True)


@pytest.fixture(autouse=True)
def _no_live_reranker() -> Generator[None, None, None]:
    """No test reaches the reranker service — `_no_live_ingestion_backends`' reason, one seam over.

    Without this every `KnowledgeSearch` in the suite would `httpx.post` to
    `localhost:8001`, and `src.services.reranker` would swallow the refusal and
    degrade to the unthresholded fused order. Green, but testing the fallback
    path on every single assertion instead of the one the product runs.
    """
    app.dependency_overrides[get_reranker_client] = lambda: _overlap_rerank
    yield
    app.dependency_overrides.pop(get_reranker_client, None)


@pytest.fixture(autouse=True)
def _fresh_upload_rate_limiter() -> Generator[SlidingWindowLimiter, None, None]:
    """Give every test its own upload counters (#101) — the process-wide ones would leak between tests.

    Lifted here from `test_documents.py` when `shelf_with` below made uploading
    something any test module can do: counters shared across a suite mean one
    module's fixtures are the next module's starting point, and the failure that
    causes — a 429 in a test that uploaded twice — reads as a bug in whatever
    ran last rather than as the leak it is.
    """
    limiter = SlidingWindowLimiter()
    app.dependency_overrides[get_upload_rate_limiter] = lambda: limiter
    yield limiter
    app.dependency_overrides.pop(get_upload_rate_limiter, None)


class EmptyKnowledgeStore:
    """A `KnowledgeStore` with nothing on it, for a run whose graph never searches.

    `RunServices` requires a store whether or not the graph under test has a
    `KnowledgeSearch` node, and a grid-cost or asks-back test's graph does not.
    Standing up a shelf for those would be ceremony around something never
    called; an empty shelf is what they actually mean, and it says so.
    """

    def search(self, query: str, *, source_refs: Sequence[str], limit: int = DEFAULT_PASSAGE_LIMIT) -> tuple[()]:
        return ()


def knowledge_store_for(db: Session, owner_id: int, qdrant_client: QdrantClient) -> QdrantKnowledgeStore:
    """The product's own store, wired to a test's in-memory Qdrant and offline fakes.

    For the tests that search *directly* rather than through a graph — asking
    what one creator's shelf makes of a question, without a workflow, a model
    or an HTTP route in the way. Everything a route would have resolved through
    `Depends` is passed here instead, so the class under test is the real one
    and only its collaborators are stand-ins.
    """
    return QdrantKnowledgeStore(
        db=db,
        owner_id=owner_id,
        qdrant_client=qdrant_client,
        embedding_client=_fake_dense_embed,
        reranker_client=_overlap_rerank,
        secret_store=EncryptedDbSecretStore(db=db),
    )


async def shelf_with(client: AsyncClient, token: str, name: str = "Tủ tài liệu", **files: str) -> str:
    """A real Knowledge Base carrying `files`, as the `source_ids` string that addresses it.

    Uploaded through the real routes rather than written to disk, because
    since the retrieval stacks were consolidated onto `QdrantKnowledgeStore`
    there is no longer a store that reads a directory: a shelf *is* its points
    in Qdrant, and the only thing that puts them there is the ingest pipeline
    (`src.services.ingestion`). `_no_live_ingestion_backends` makes that
    pipeline offline, so this stays as fast as writing a file was, and it
    exercises extraction and chunking on the way in instead of skipping them.

    Returns `str` and not `int` because that is what a `KnowledgeSearch`
    node's `source_ids` holds (ADR-0043 §1) — a caller can put the result
    straight into a graph.
    """
    created = await client.post("/api/v1/knowledge-bases", json={"name": name}, headers=auth_header(token))
    assert created.status_code == 201, created.text
    kb_id = created.json()["id"]
    for file_name, text_content in files.items():
        uploaded = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            files={"file": (file_name, io.BytesIO(text_content.encode("utf-8")), "application/octet-stream")},
            headers=auth_header(token),
        )
        assert uploaded.status_code == 201, uploaded.text
    return str(kb_id)


class FakeClock:
    """A monotonic clock a test moves by hand — see `tests/test_deployments/test_rate_limit.py`.

    `BudgetLedger` reads whatever clock `RunServices.clock` gives it (#58). A
    test asserting on `max_wall_seconds` against the real clock either sleeps
    for real past it or proves nothing about the boundary — and under a loaded
    suite, sleeping is not even reliable, because the run's own steps can eat
    the ceiling before the thing under test happens. This is what lets a test
    move time exactly as far as the claim requires and no further.
    """

    def __init__(self) -> None:
        self.seconds = 1_000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


@pytest.fixture
def fake_clock() -> Generator[FakeClock, None, None]:
    """Install a `FakeClock` behind `get_clock`, for a run whose budget a test drives by hand."""
    clock = FakeClock()
    app.dependency_overrides[get_clock] = lambda: clock
    yield clock
    app.dependency_overrides.pop(get_clock, None)


def install_chat_model_factory(build: Callable[[str], Any]) -> None:
    """Point the model-factory seam at `build` for the next request.

    The bare version of what `scripted_chat_model_factory` does, for the tests
    whose fake is not a list of scripted answers — a model that hangs, one that
    reports no usage, one that raises. Each of those was otherwise a fixture
    repeating the same three lines of override bookkeeping.

    Undone by `_reset_chat_model_factory`, so no test has to remember to.
    """
    app.dependency_overrides[get_chat_model_factory] = lambda: build


@pytest.fixture
def clean_platform_ledger() -> Generator[None, None, None]:
    """An empty `platform_model_calls`, before and after.

    `src.services.platform_spend.record` opens its own short-lived session — it
    has to, because it is called from inside a model wrapper that has no request
    and no `db` — so its rows do not roll back with `db_session`. Emptying the
    table around each test is what keeps these assertions about the test's own
    calls rather than about everything the suite happened to run first.
    """
    def _empty() -> None:
        with SessionLocal() as session:
            session.query(PlatformModelCall).delete()
            session.commit()

    _empty()
    yield
    _empty()


@pytest.fixture(autouse=True)
def _reset_chat_model_factory() -> Generator[None, None, None]:
    """Drop any installed model factory after each test, however it was installed."""
    yield
    app.dependency_overrides.pop(get_chat_model_factory, None)


class ScriptedToolModel(FakeMessagesListChatModel):
    """Cycles through scripted `AIMessage`s and records every message batch it saw.

    `received_message_batches` is how a test asks what the model was actually
    given — the only way to observe, from outside, what context a step ran with.

    `bind_tools` is overridden to return `self`: the base `BaseChatModel` raises
    `NotImplementedError`, and the runtime always binds tools for a step that
    declares any (see `src.agents.runtime`).
    """

    _received_message_batches: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    @property
    def received_message_batches(self) -> list[list[BaseMessage]]:
        return self._received_message_batches

    def bind_tools(self, tools: object, *, tool_choice: str | None = None, **kwargs: object) -> "ScriptedToolModel":
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: object = None, **kwargs: object
    ) -> ChatResult:  # type: ignore[override]
        self._received_message_batches.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _stream(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: object = None, **kwargs: object
    ) -> Iterator[ChatGenerationChunk]:  # type: ignore[override]
        """Hand the scripted answer back a piece at a time, the way a real model does.

        Without this the base class falls back to one chunk holding the whole
        answer, and "the answer appears bit by bit" (#8) would pass against a
        runtime that still waited for the full response before emitting anything.

        Delegates to `_generate` so the batch is still recorded and the response
        cursor still advances exactly once per call — a second copy of that
        bookkeeping here is a second thing to drift.
        """
        message = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs).generations[0].message
        for chunk in _scripted_chunks(message):
            yield ChatGenerationChunk(message=chunk)


def _scripted_chunks(message: BaseMessage) -> list[AIMessageChunk]:
    """`message` split the way a streaming provider would deliver it.

    A tool-calling turn arrives whole: its arguments are a JSON document that is
    meaningless in halves, and no part of it is ever shown to a user as it
    arrives. Prose is split on whitespace, which is what makes a partial answer
    observable.

    A scripted `usage_metadata` rides on the *last* chunk only, which is where a
    streaming provider puts it — it can only say what a call cost once the call
    is over. Putting it on every chunk would make the merged message report the
    spend once per word, and budget enforcement (#10) would look like it worked
    while being fed nonsense.
    """
    usage = getattr(message, "usage_metadata", None)
    tool_calls = getattr(message, "tool_calls", [])
    if tool_calls:
        return [
            AIMessageChunk(
                content=message.content,
                additional_kwargs=message.additional_kwargs,
                usage_metadata=usage,
                tool_call_chunks=[
                    tool_call_chunk(name=call["name"], args=json.dumps(call["args"]), id=call["id"], index=index)
                    for index, call in enumerate(tool_calls)
                ],
            )
        ]
    words = str(message.content).split(" ")
    last = len(words) - 1
    return [
        AIMessageChunk(content=word if index == 0 else f" {word}", usage_metadata=usage if index == last else None)
        for index, word in enumerate(words)
    ]


#: What `scripted_chat_model_factory` hands back: the model ids the runtime asked
#: for, and the models it got — both filling in as the chat request runs.
ScriptedRun = tuple[list[str], list[ScriptedToolModel]]


@pytest.fixture
def scripted_chat_model_factory() -> Callable[[list[list[AIMessage]]], ScriptedRun]:
    """Install a fake `ChatModelFactory`: one scripted model per step, in call order.

    Returns a setup function; call it with one list of `AIMessage`s per step (in
    the order the graph will execute them). Calling it again re-arms the seam for
    the next request, which is how a multi-turn test scripts each turn.
    """

    def _install(step_scripts: list[list[AIMessage]]) -> ScriptedRun:
        requested_model_ids: list[str] = []
        created_models: list[ScriptedToolModel] = []
        scripts = iter(step_scripts)

        def factory(model_id: str) -> ScriptedToolModel:
            requested_model_ids.append(model_id)
            model = ScriptedToolModel(responses=next(scripts))
            created_models.append(model)
            return model

        install_chat_model_factory(factory)
        return requested_model_ids, created_models

    return _install


# --------------------------------------------------------------------------- #
# Tracing (#11) — the seam, and a trace that keeps what it is given
# --------------------------------------------------------------------------- #


class RecordingTrace(EventTrace):
    """A trace that keeps its finished run instead of shipping it anywhere.

    Subclasses the production `EventTrace`, so what a test asserts about steps
    and tool calls is produced by the code the Langfuse path runs — not by a
    second implementation that could agree with the tests and disagree with
    production.
    """

    recorded: list[RecordedRun] = []

    def run_ended(self, run: RecordedRun) -> None:
        RecordingTrace.recorded.append(run)


class BrokenTrace:
    """A trace backend having a bad day: every call raises — including construction.

    The only honest way to test "tracing is not a blocking dependency". A backend
    that is merely slow or absent proves nothing, because the code path that
    swallows the failure is never reached.

    Failing in `__init__` is the part worth insisting on: opening a trace is
    itself a call into the backend (`LangfuseTrace` starts its root span there),
    and it happens in the endpoint before any of the run's own guards are in
    scope. A double that only failed on `record` would leave that frame
    permanently untested.
    """

    def __init__(self, identity: Any) -> None:
        raise RuntimeError("trace backend sập ngay khi mở trace")

    def record(self, name: str, data: Any) -> None:
        raise RuntimeError("trace backend sập")

    def finish(self, *, cost: Any, error: str | None) -> None:
        raise RuntimeError("trace backend sập")


class MidRunBrokenTrace:
    """A trace that opens fine and then fails on everything afterwards.

    The other half of the failure surface: a backend that accepted the trace and
    then went away mid-run, which `BrokenTrace` can no longer reach now that it
    dies in its constructor.
    """

    def __init__(self, identity: Any) -> None:
        pass

    def record(self, name: str, data: Any) -> None:
        raise RuntimeError("trace backend sập giữa chừng")

    def finish(self, *, cost: Any, error: str | None) -> None:
        raise RuntimeError("trace backend sập giữa chừng")


@pytest.fixture
def recorded_runs() -> Generator[list[RecordedRun], None, None]:
    """Point the tracing seam at `RecordingTrace` and hand back what it collects."""
    RecordingTrace.recorded = []
    app.dependency_overrides[get_tracer] = lambda: RecordingTrace
    yield RecordingTrace.recorded
    app.dependency_overrides.pop(get_tracer, None)


@pytest.fixture
def broken_tracer() -> Generator[None, None, None]:
    """Point the tracing seam at a backend that fails the moment a trace is opened."""
    app.dependency_overrides[get_tracer] = lambda: BrokenTrace
    yield
    app.dependency_overrides.pop(get_tracer, None)


@pytest.fixture
def mid_run_broken_tracer() -> Generator[None, None, None]:
    """Point the tracing seam at a backend that opens fine and then dies partway through."""
    app.dependency_overrides[get_tracer] = lambda: MidRunBrokenTrace
    yield
    app.dependency_overrides.pop(get_tracer, None)


def record_eval(db_session: Session, version: AgentVersion, *, passed: int, total: int) -> AgentVersion:
    """One eval run of `total` cases, `passed` of which passed, at the bar the platform ships with.

    The one place a "run that scored X" is built. Several suites need one, and
    three hand-rolled versions of it is three things to fix the day the score's
    shape changes.

    The bar is `DEFAULT_EVAL_PASS_THRESHOLD` rather than a typed `0.6`, for the
    reason `src/db/models.py` gives for sharing that constant at all: the
    migration and the suite must never disagree about the default.
    """
    store_run(
        db_session,
        version.id,
        threshold=DEFAULT_EVAL_PASS_THRESHOLD,
        results=[
            EvalCaseResult(
                case_id=index + 1,
                criterion="Thư trả lời nêu đúng mã đơn hàng",
                input_message="đơn DH-1 đâu?",
                expected="nêu đúng DH-1",
                answer="Đơn DH-1 đang giao",
                passed=index < passed,
                reason="đúng mã đơn" if index < passed else "không nêu mã đơn",
            )
            for index in range(total)
        ],
    )
    return version


def record_passing_eval(db_session: Session, version: AgentVersion) -> AgentVersion:
    """Give `version` an eval run it clears, so it may enter the approval queue.

    `request_approval` refuses a version with no passing eval behind it (#13), so
    any test that needs an `approved` version as scenery has to arrange one.
    """
    return record_eval(db_session, version, passed=1, total=1)


@pytest.fixture
def seeded_creator(db_session: Session) -> User:
    """The creator account the migrations seed, as an ORM row."""
    settings = get_settings()
    return db_session.query(User).filter(User.email == settings.seed_creator_email).one()


def _make_agent_with_version(
    db_session: Session,
    owner: User,
    spec: AgentSpec,
    *,
    name: str,
    status: VersionStatus,
    deploy: bool,
    access_mode: DeploymentAccessMode = DeploymentAccessMode.PRIVATE,
) -> tuple[Agent, AgentVersion]:
    """An agent owning one version of `spec`, optionally published as a Deployment.

    `access_mode` defaults to `private` because that is what publishing actually
    does since #50 (ADR-0029 §3) — a fixture that quietly opened the agent to the
    platform would let a suite prove a stranger's access that the product does
    not grant.
    """
    agent = Agent(owner_id=owner.id, name=name)
    db_session.add(agent)
    db_session.flush()
    version = AgentVersion(agent_id=agent.id, spec=spec.model_dump(mode="json"), status=status)
    db_session.add(version)
    db_session.flush()
    if deploy:
        db_session.add(
            Deployment(
                agent_id=agent.id,
                agent_version_id=version.id,
                access_mode=access_mode,
                api_key_hash=hash_api_key(DEPLOYMENT_API_KEY),
                created_by=owner.id,
            )
        )
    db_session.commit()
    db_session.refresh(agent)
    db_session.refresh(version)
    return agent, version


def discard_agent(db_session: Session, agent: Agent) -> None:
    """Delete `agent` and everything hanging off it — what a fixture's teardown does.

    The order is the reason this is shared rather than copied into each fixture:
    the Deployment holds a foreign key to the version it publishes (#50), so the
    publication goes first, then the versions, then the agent. Any `sandbox_runs`
    rows go with the versions through `ON DELETE CASCADE` — a transcript is
    evidence about a version and means nothing once that version is gone.

    Tolerant of an agent that is already gone, because a test about *deleting*
    one leaves its fixture nothing to tear down. Cleanup that fails when the test
    succeeded is cleanup that reports the wrong thing.
    """
    # `inspect(...).identity` reads the primary key out of the identity map
    # without touching the database. Plain `agent.id` would not do: the object may
    # be expired, and reading an attribute off an expired row that no longer
    # exists raises `ObjectDeletedError` — from the cleanup, about a row the test
    # deleted on purpose.
    identity = inspect(agent).identity
    if identity is None or db_session.get(Agent, identity[0]) is None:
        return
    db_session.refresh(agent)
    if agent.deployment is not None:
        db_session.delete(agent.deployment)
        db_session.commit()
    db_session.query(AgentVersion).filter(AgentVersion.agent_id == agent.id).delete()
    db_session.delete(agent)
    db_session.commit()


def _delete_agents(db_session: Session, agents: list[Agent]) -> None:
    """Undo `_make_agent_with_version` for every agent a fixture created."""
    for agent in agents:
        discard_agent(db_session, agent)


@pytest.fixture
def drafted_agent_of(db_session: Session) -> Generator[Callable[..., tuple[Agent, AgentVersion]], None, None]:
    """An `Agent` owned by `owner` with one `DRAFT` version and nothing deployed.

    What a sandbox run (#9) is actually for: a version its creator has not shown
    to anyone yet. Returns both rows because a sandbox run is addressed by
    version, not by agent.
    """
    created: list[Agent] = []

    def _make(owner: User, spec: AgentSpec, *, name: str = "Agent nháp") -> tuple[Agent, AgentVersion]:
        agent, version = _make_agent_with_version(
            db_session, owner, spec, name=name, status=VersionStatus.DRAFT, deploy=False
        )
        created.append(agent)
        return agent, version

    yield _make

    _delete_agents(db_session, created)


@pytest.fixture
def deployed_agent_of(db_session: Session) -> Generator[Callable[..., Agent], None, None]:
    """An `Agent` owned by `owner`, published as an active Deployment of one `APPROVED` version.

    `access_mode` is the fixture's one knob, because who may reach a published
    agent is exactly what #50 made a decision rather than a consequence. Its API
    key is `DEPLOYMENT_API_KEY`, so a test about the API channel has a key to
    present without reaching into the hash.
    """
    created: list[Agent] = []

    def _make(
        owner: User,
        spec: AgentSpec,
        *,
        name: str = "Agent test",
        access_mode: DeploymentAccessMode = DeploymentAccessMode.PRIVATE,
    ) -> Agent:
        agent, _ = _make_agent_with_version(
            db_session, owner, spec, name=name, status=VersionStatus.APPROVED, deploy=True, access_mode=access_mode
        )
        created.append(agent)
        return agent

    yield _make

    _delete_agents(db_session, created)


@pytest.fixture
def solution_spec_version(db_session: Session) -> Generator[Callable[..., AgentVersion], None, None]:
    """An `AgentVersion` holding a V2 solution spec, at whatever stage a test needs.

    The V2 counterpart of `drafted_agent_of`, and separate from it for the
    reason ADR-0020 §5 made the columns separate: a version at the spec stage
    has no `AgentSpec` at all, so the V1 factory has nothing to build it from.

    Returns the version rather than the agent, because every V2 stage gate is
    addressed by version — the agent exists only so the ownership checks on the
    routes have something to answer.

    `workflow` seeds the *second* V2 document on the same row, which is what a
    canvas test (#44) needs and a generator test (#43) must not have: the
    Generator's whole refusal to run twice is about a version that already holds
    one, so the two are the same factory with the column left empty or filled.
    """
    created: list[AgentVersion] = []

    def _make(
        owner: User,
        spec: dict[str, Any],
        *,
        status: VersionStatus = VersionStatus.SPEC_CONFIRMED,
        name: str = "Agent V2",
        workflow: dict[str, Any] | None = None,
    ) -> AgentVersion:
        agent = Agent(owner_id=owner.id, name=name)
        db_session.add(agent)
        db_session.flush()
        version = AgentVersion(agent_id=agent.id, solution_spec=spec, status=status, workflow=workflow)
        db_session.add(version)
        db_session.commit()
        db_session.refresh(version)
        created.append(version)
        return version

    yield _make

    _delete_agents(db_session, [version.agent for version in created])


# --------------------------------------------------------------------------- #
# Approval (#14) — scenery for a version that is asking to be reviewed
# --------------------------------------------------------------------------- #

#: Shared by `tests/test_approvals` (the service modules) and
#: `tests/test_api/test_approval_queue.py` (the HTTP seam). The gates are real,
#: so nothing here writes a version into a state the product could not reach —
#: a version arrives in the queue by having a trial run and an eval behind it,
#: the same way a creator's does.


def high_risk_spec() -> AgentSpec:
    """A spec that asks for the two things an admin is meant to stop and read.

    `send_email` has side effects and `http_request` is domain-gated, so this is
    also the only shape in which `allowed_domains` is non-empty — a spec cannot
    declare a domain-gated tool without one (ADR-0001).
    """
    return agent_spec(
        steps=[
            {
                "id": "soan",
                "title": "Soạn thư",
                "prompt": "Soạn thư trả lời khách",
                "model": "gpt-4.1",
                "tools": ["send_email", "http_request"],
                "max_iterations": 2,
            },
            {
                "id": "tom-tat",
                "title": "Tóm tắt",
                "prompt": "Tóm tắt lại việc đã làm",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 1,
            },
        ],
        allowed_domains=["api.erp.internal"],
        success_criteria=["Thư trả lời nêu đúng mã đơn hàng"],
    )


@pytest.fixture
def seeded_admin(db_session: Session) -> User:
    """The admin account the migrations seed, as an ORM row — the actor on every decision."""
    return db_session.query(User).filter(User.role == UserRole.ADMIN).one()


@pytest.fixture
def approval_agent(db_session: Session, seeded_creator: User) -> Generator[Agent, None, None]:
    """A bare agent owned by the seeded creator, torn down with all its versions."""
    row = Agent(owner_id=seeded_creator.id, name="Agent xin duyệt")
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    yield row
    discard_agent(db_session, row)


@pytest.fixture
def sandbox_passed_version(db_session: Session, approval_agent: Agent) -> Callable[..., AgentVersion]:
    """A version that has been trialled — the state a version is in before it asks to be approved."""

    def _make(spec: AgentSpec | None = None) -> AgentVersion:
        return mark_sandbox_passed(db_session, create_draft(db_session, approval_agent, spec or high_risk_spec()))

    return _make


def record_sandbox_run(db_session: Session, version: AgentVersion, *, status: SandboxRunStatus | None = None) -> int:
    """A stored trial-run transcript for `version` — the evidence the dossier attaches."""
    run = SandboxRun(
        version_id=version.id,
        conversation_id="trial-abc",
        input_message="Khách hỏi đơn DH-1",
        transcript=[
            SandboxTranscriptEntry(
                kind="tool",
                step_id="soan",
                tool="send_email",
                args={"to": "khach@example.com"},
                simulated=True,
            ).model_dump(mode="json"),
            SandboxTranscriptEntry(
                kind="step", step_id="soan", title="Soạn thư", content="Đã soạn thư cho DH-1"
            ).model_dump(mode="json"),
        ],
        status=status or SandboxRunStatus.SUCCEEDED,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run.id


# --------------------------------------------------------------------------- #
# V2 document builders. They live here rather than beside one test file because
# the API-level tests are now the platform's net (ADR-0048) and a builder owned
# by any one of them is a builder that disappears when that file does.
# --------------------------------------------------------------------------- #

#: The spec a V2 version holds where a V1 one holds an `AgentSpec`. Minimal on
#: purpose: nothing here is about the interview that produced it.
V2_SPEC: dict[str, Any] = {
    "goal": "Xét khiếu nại rồi gửi thư trả lời khi được duyệt",
    "users": ["Nhân viên chăm sóc khách hàng"],
    "data": [{"name": "Sổ tay", "kind": "uploaded_file", "ref": "so-tay.md"}],
    "actions": ["Gửi thư trả lời cho khách"],
    "expected_outcome": ["Một thư trả lời đã gửi"],
    "success_criteria": [{"text": "Chỉ gửi thư khi người duyệt đồng ý", "category": "human_approval_compliance"}],
    "prohibitions": ["Không được tự ý gửi thư cho khách"],
}


def mcp_node(node_id: str, action: str, *, tool_id: str = GMAIL) -> dict[str, Any]:
    return {"id": node_id, "type": "mcp", "config": {"tool_id": tool_id, "action": action}}


def approval_graph(*mcp_nodes: dict[str, Any]) -> dict[str, Any]:
    """A confirmed-shaped graph asking for whatever actions a test names.

    Wired rather than a bare node list because the dossier hands the whole
    document on to the screen, and a graph with no edges is not the document an
    admin would ever be shown.
    """
    nodes = [
        {"id": "approval", "type": "human_approval", "config": {"approver_role": "manager", "message_template": "?"}},
        *mcp_nodes,
        {"id": "finish", "type": "end", "config": {}},
    ]
    return {
        "start_node_id": "approval",
        "nodes": nodes,
        "edges": [
            {"id": f"e{index}", "source_node_id": "approval", "source_port": "approved", "target_node_id": node["id"]}
            for index, node in enumerate(mcp_nodes)
        ],
        "metadata": {},
    }


#: A fully drawn workflow at `workflow_confirmed` — search, answer, loop, approval, act.
CONFIRMED_WORKFLOW: dict[str, Any] = {
    "start_node_id": "search",
    "nodes": [
        {
            "id": "search",
            "type": "knowledge_search",
            "config": {"source_ids": ["blob://so-tay-noi-bo.pdf"]},
            "position": {"x": 0, "y": 0},
        },
        {
            "id": "answer",
            "type": "agent_llm",
            "config": {"prompt": "Trả lời dựa trên đúng sổ tay đã tìm được", "model": "gpt-4o-mini"},
            "position": {"x": 260, "y": 0},
        },
        {
            "id": "loop",
            "type": "while",
            "config": {"condition": "khách còn hỏi thêm", "max_iterations": 3},
            "position": {"x": 520, "y": 0},
        },
        {
            "id": "approval",
            "type": "human_approval",
            "config": {"approver_role": "manager", "message_template": "Duyệt câu trả lời cho khách hàng X"},
            "position": {"x": 780, "y": 0},
        },
        {
            "id": "act",
            "type": "mcp",
            "config": {"tool_id": "conn-of-nguyen-van-a", "action": "send_email"},
            "position": {"x": 1040, "y": 0},
        },
        {"id": "finish", "type": "end", "config": {}, "position": {"x": 1300, "y": 0}},
    ],
    "edges": [
        {"id": "search:found->answer", "source_node_id": "search", "source_port": "found", "target_node_id": "answer"},
        {"id": "answer:done->finish", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"},
    ],
}
