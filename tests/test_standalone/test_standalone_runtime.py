"""The exported package's runtime — DS-13 / ADR-0059 §10.

A real package in a temporary directory — `bundle.json`, `access.yaml`,
`runtime.yaml`, secrets in the environment — its Hệ CSDL the test Postgres
with two small views, its LLM a scripted double behind the same seam the
OpenAI-compatible client fills. The ticket's three acceptance criteria:

- chat over the API answers within the asker's scope;
- the app serves chat, approvals and health and no platform route — and the
  module never imports a platform router at all;
- a missing required secret stops it starting, with the secret's name.
"""

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import text

from src.agents.portability import BUNDLE_FORMAT
from src.db.base import engine
from src.main import app as platform_app
from src.services.llm import get_chat_model_factory
from src.standalone.__main__ import main
from src.standalone.app import create_app, openai_compatible_factory
from src.standalone.config import (
    LlmConfig,
    MissingSecretError,
    RuntimeConfigError,
    load_runtime_config,
    resolve_secrets,
    secret_source,
)
from src.standalone.package import Package, load_package, prepare_database
from tests.conftest import ScriptedRun, parse_sse

pytestmark = pytest.mark.asyncio

PREFIX = "TEST_PKG_"
SOURCE_ID = "7"

ACCESS = {
    "identity": {
        "view": "v_sa_staff",
        "match_by": "email",
        "attributes": {"employee_code": "emp_id", "department": "dept_code", "position": "position"},
    },
    "rules": [
        {"position": "Nhân viên KD", "views": {"v_sa_ar": {"where": {"sales_rep_id": "employee_code"}}}},
        {"position": "Trưởng nhóm KD", "views": {"v_sa_ar": {"where": {"department": "department"}}}},
    ],
}

SPEC = {
    "goal": "Trả lời câu hỏi công nợ theo đúng phạm vi người hỏi",
    "users": ["Nhân viên kinh doanh"],
    "data": [{"name": "ERP", "kind": "database", "ref": SOURCE_ID}],
    "actions": ["Tra công nợ"],
    "expected_outcome": ["Câu trả lời có số liệu"],
    "success_criteria": [{"text": "Đúng số liệu", "category": "correctness"}],
    "prohibitions": ["Không xem khách của người khác"],
}


def _workflow(*, gated: bool = False) -> dict[str, Any]:
    nodes = [
        {"id": "query", "type": "data_query", "config": {"source_id": SOURCE_ID, "views": ["v_sa_ar"]}},
        {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời về công nợ.", "model": "gpt-4o-mini"}},
        {"id": "finish", "type": "end", "config": {}},
    ]
    edges = [
        {"id": "e1", "source_node_id": "query", "source_port": "done", "target_node_id": "answer"},
        {"id": "e2", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"},
    ]
    if gated:
        nodes.insert(
            2,
            {
                "id": "approval",
                "type": "human_approval",
                "config": {"approver_role": "manager", "message_template": "Gửi câu trả lời này?"},
            },
        )
        nodes.insert(3, {"id": "report", "type": "agent_llm", "config": {"prompt": "Báo lại.", "model": "gpt-4o-mini"}})
        edges = [
            edges[0],
            {"id": "e2", "source_node_id": "answer", "source_port": "done", "target_node_id": "approval"},
            {"id": "e3", "source_node_id": "approval", "source_port": "approved", "target_node_id": "report"},
            {"id": "e4", "source_node_id": "approval", "source_port": "rejected", "target_node_id": "report"},
            {"id": "e5", "source_node_id": "report", "source_port": "done", "target_node_id": "finish"},
        ]
    return {"start_node_id": "query", "nodes": nodes, "edges": edges, "metadata": {}}


@pytest.fixture(autouse=True)
def _views() -> Iterator[None]:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE OR REPLACE VIEW v_sa_staff AS SELECT * FROM (VALUES "
                "('an@cty.vn', 'E102', 'SALES', 'Nhân viên KD'), "
                "('binh@cty.vn', 'E200', 'SALES', 'Trưởng nhóm KD')"
                ") AS t(email, emp_id, dept_code, position)"
            )
        )
        connection.execute(
            text(
                "CREATE OR REPLACE VIEW v_sa_ar AS SELECT * FROM (VALUES "
                "('C1', 'E102', 'SALES', 45), ('C2', 'E102', 'SALES', 10), ('C3', 'E200', 'SALES', 60)"
                ") AS t(customer_id, sales_rep_id, department, days_overdue)"
            )
        )
    yield
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW IF EXISTS v_sa_staff"))
        connection.execute(text("DROP VIEW IF EXISTS v_sa_ar"))


def _runtime(**overrides: Any) -> dict[str, Any]:
    return {
        "bundle": "bundle.json",
        "database": {"url_secret": "runtime_db"},
        "data_sources": [{"id": SOURCE_ID, "name": "ERP mẫu", "dialect": "postgresql", "dsn_secret": "erp_dsn"}],
        "llm": {"base_url": "http://llm.internal/v1", "api_key_secret": "llm_key", "default_model": "local-model"},
        "auth": {"mode": "trusted_header", "header": "X-Asker"},
        "secrets": {"backend": "env", "env_prefix": PREFIX},
        "trusted_hosts": [str(engine.url.host)],
        **overrides,
    }


@pytest.fixture
def package_dir(tmp_path: Path) -> Callable[..., Path]:
    """Write a package into `tmp_path` — the three files a company receives."""

    def _write(*, workflow: dict[str, Any] | None = None, **runtime: Any) -> Path:
        bundle = {
            "format": BUNDLE_FORMAT,
            "name": "Trợ lý công nợ",
            "solution_spec": SPEC,
            "workflow": workflow or _workflow(),
        }
        (tmp_path / "bundle.json").write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        (tmp_path / "access.yaml").write_text(yaml.safe_dump(ACCESS, allow_unicode=True), encoding="utf-8")
        (tmp_path / "runtime.yaml").write_text(
            yaml.safe_dump(_runtime(**runtime), allow_unicode=True), encoding="utf-8"
        )
        return tmp_path

    return _write


@pytest.fixture
def secrets_env(monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    monkeypatch.setenv(f"{PREFIX}RUNTIME_DB", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv(f"{PREFIX}ERP_DSN", dsn)
    monkeypatch.setenv(f"{PREFIX}LLM_KEY", "sk-test")


def _loaded(directory: Path) -> Package:
    config = load_runtime_config(directory / "runtime.yaml")
    secrets = resolve_secrets(config, secret_source(config.secrets, base=directory))
    return load_package(config, secrets, base=directory)


def _platform_factory() -> Callable[[str], Any]:
    """The scripted model `scripted_chat_model_factory` installed, read at call time so re-arming works."""
    return lambda model_id: platform_app.dependency_overrides[get_chat_model_factory]()(model_id)


async def _client(package: Package) -> AsyncClient:
    app = create_app(package, model_factory=_platform_factory())
    app.state.ids = prepare_database(package)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://package")


def _writes(query: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "data_query", "args": query, "id": "q1"}])


OVERDUE = {
    "view": "v_sa_ar",
    "columns": ["customer_id"],
    "filters": [{"column": "days_overdue", "op": ">", "value": 30}],
    "order_by": [{"column": "customer_id", "dir": "asc"}],
}


def _rows_read(response: Any) -> str:
    [result] = [line for line in parse_sse(response.text, event="tool_result") if line["tool"] == "data_query"]
    return str(result["result"])


# --------------------------------------------------------------------------- #
# Chat answers within the asker's scope
# --------------------------------------------------------------------------- #


async def test_chat_over_the_api_reads_within_each_askers_scope(
    package_dir: Callable[..., Path],
    secrets_env: None,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    package = _loaded(package_dir())
    async with await _client(package) as client:
        scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="C1")]])
        rep = await client.post("/chat", json={"message": "khách nào quá hạn?"}, headers={"X-Asker": "an@cty.vn"})
        scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="C1, C3")]])
        lead = await client.post("/chat", json={"message": "khách nào quá hạn?"}, headers={"X-Asker": "binh@cty.vn"})

    assert rep.status_code == 200, rep.text
    assert "C1" in _rows_read(rep) and "C3" not in _rows_read(rep)
    assert "C1" in _rows_read(lead) and "C3" in _rows_read(lead)


async def test_someone_the_employee_view_does_not_know_is_told_no_permission(
    package_dir: Callable[..., Path],
    secrets_env: None,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    package = _loaded(package_dir())
    async with await _client(package) as client:
        scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="Không có quyền.")]])
        response = await client.post("/chat", json={"message": "cho xem hết"}, headers={"X-Asker": "la@cty.vn"})

    assert "không có quyền" in _rows_read(response).lower()


async def test_a_request_naming_nobody_is_a_401_never_a_default_asker(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    async with await _client(_loaded(package_dir())) as client:
        response = await client.post("/chat", json={"message": "xin chào"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Chưa xác thực được người hỏi"


async def test_an_approval_is_shown_to_and_decided_by_the_asker_alone(
    package_dir: Callable[..., Path],
    secrets_env: None,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    package = _loaded(package_dir(workflow=_workflow(gated=True)))
    async with await _client(package) as client:
        scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="Bản trả lời.")]])
        paused = await client.post("/chat", json={"message": "khách quá hạn"}, headers={"X-Asker": "an@cty.vn"})
        assert parse_sse(paused.text, event="awaiting_approval"), paused.text

        mine = (await client.get("/approvals", headers={"X-Asker": "an@cty.vn"})).json()
        theirs = (await client.get("/approvals", headers={"X-Asker": "binh@cty.vn"})).json()
        assert len(mine) == 1 and theirs == []

        refused = await client.post(
            f"/approvals/{mine[0]['approval_id']}", json={"approved": True}, headers={"X-Asker": "binh@cty.vn"}
        )
        assert refused.status_code == 404

        scripted_chat_model_factory([[AIMessage(content="Đã xong.")]])
        resumed = await client.post(
            f"/approvals/{mine[0]['approval_id']}", json={"approved": True}, headers={"X-Asker": "an@cty.vn"}
        )

    assert resumed.status_code == 200, resumed.text
    assert [line["node_id"] for line in parse_sse(resumed.text, event="node")] == ["approval", "report", "finish"]


# --------------------------------------------------------------------------- #
# No platform route, and no platform router imported at all
# --------------------------------------------------------------------------- #


async def test_the_package_serves_chat_approvals_and_health_and_nothing_else(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    app = create_app(_loaded(package_dir()), model_factory=_platform_factory())

    served = {route.path for route in app.routes if not route.path.startswith(("/docs", "/redoc", "/openapi"))}

    assert served == {"/health", "/chat", "/approvals", "/approvals/{approval_id}"}


def test_the_package_never_imports_a_platform_router() -> None:
    """Designer, Builder, admin and PM live in `src.api`; the package's app must not pull any of it in."""
    probe = "import sys, src.standalone.app, src.standalone.__main__; print(sorted(m for m in sys.modules if m.startswith('src.api')))"
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=root, check=True)

    assert result.stdout.strip() == "[]", result.stdout


async def test_health_says_whether_the_packages_database_answers(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    async with await _client(_loaded(package_dir())) as client:
        response = await client.get("/health")

    assert response.json() == {"status": "ok", "agent": "Trợ lý công nợ", "database": True}


# --------------------------------------------------------------------------- #
# Refusing to start, by name
# --------------------------------------------------------------------------- #


def test_a_missing_required_secret_is_named_with_where_it_was_looked_for(
    package_dir: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = package_dir()
    monkeypatch.setenv(f"{PREFIX}RUNTIME_DB", "postgresql://x")
    config = load_runtime_config(directory / "runtime.yaml")

    with pytest.raises(MissingSecretError) as caught:
        resolve_secrets(config, secret_source(config.secrets, base=directory))

    assert set(caught.value.missing) == {"erp_dsn", "llm_key"}
    assert f"{PREFIX}ERP_DSN" in str(caught.value)
    assert f"{PREFIX}LLM_KEY" in str(caught.value)


def test_the_entrypoint_exits_before_opening_a_port_when_a_secret_is_missing(
    package_dir: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = package_dir()
    for name in ("RUNTIME_DB", "ERP_DSN", "LLM_KEY"):
        monkeypatch.delenv(f"{PREFIX}{name}", raising=False)

    status = main(["--config", str(directory / "runtime.yaml")])

    assert status == 2
    error = capsys.readouterr().err
    assert "runtime_db" in error and "erp_dsn" in error and "llm_key" in error


def test_secrets_can_come_from_files(package_dir: Callable[..., Path], tmp_path: Path) -> None:
    directory = package_dir(secrets={"backend": "file", "directory": "secrets"})
    (directory / "secrets").mkdir()
    for name, value in (("runtime_db", "postgresql://x"), ("erp_dsn", "postgresql://y"), ("llm_key", "sk")):
        (directory / "secrets" / name).write_text(value + "\n", encoding="utf-8")
    config = load_runtime_config(directory / "runtime.yaml")

    assert resolve_secrets(config, secret_source(config.secrets, base=directory)) == {
        "runtime_db": "postgresql://x",
        "erp_dsn": "postgresql://y",
        "llm_key": "sk",
    }


def test_a_workflow_reading_an_undeclared_database_does_not_start(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    directory = package_dir(data_sources=[])
    config = load_runtime_config(directory / "runtime.yaml")
    secrets = resolve_secrets(config, secret_source(config.secrets, base=directory))

    with pytest.raises(RuntimeConfigError) as caught:
        load_package(config, secrets, base=directory)

    assert SOURCE_ID in str(caught.value)


def test_an_auth_mode_named_without_its_settings_refuses_to_start(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    """`mode: jwt` with no `auth.jwt` block is a package nobody could sign in to — refused, by name (DS-14)."""
    package = _loaded(package_dir(auth={"mode": "jwt"}))

    with pytest.raises(RuntimeConfigError) as caught:
        create_app(package)

    assert "auth.jwt" in str(caught.value)


def test_the_llm_is_the_companys_endpoint_mapped_from_the_models_the_bundle_names() -> None:
    llm = LlmConfig(base_url="http://llm.internal/v1", default_model="local-small", models={"gpt-4.1": "local-large"})
    build = openai_compatible_factory(llm, "sk-test")

    large, fallback = build("gpt-4.1"), build("gpt-4o-mini")

    assert (large.model_name, fallback.model_name) == ("local-large", "local-small")
    assert str(large.openai_api_base) == "http://llm.internal/v1"


def test_the_sample_runtime_yaml_shipped_for_it_is_a_valid_one() -> None:
    root = Path(__file__).resolve().parents[2]

    config = load_runtime_config(root / "deploy" / "package" / "runtime.yaml")

    assert config.auth.mode == "trusted_header"
    assert config.data_sources and config.llm.base_url


@pytest.fixture
def empty_database() -> Iterator[Any]:
    """A database with nothing in it — what a company hands the package, unlike the suite's full schema."""
    import sqlalchemy

    name = "ds13_empty_package_test"
    admin = sqlalchemy.create_engine(engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        connection.execute(text(f"CREATE DATABASE {name}"))
    fresh = sqlalchemy.create_engine(engine.url.set(database=name))
    yield fresh
    fresh.dispose()
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {name}"))
    admin.dispose()


def test_an_empty_database_gets_the_packages_tables_and_its_agent_once(
    package_dir: Callable[..., Path], secrets_env: None, empty_database: Any
) -> None:
    from sqlalchemy.orm import Session

    from src.db.models import Agent

    package = _loaded(package_dir())

    first = prepare_database(package, bind=empty_database)
    again = prepare_database(package, bind=empty_database)

    assert first == again, "a restart must find the bundle's agent, not make a second one"
    with Session(bind=empty_database) as db:
        agent = db.get(Agent, first.agent_id)
        assert agent is not None and agent.name == "Trợ lý công nợ"
        assert agent.deployed_version_id is None  # reads `deployments` — the table an empty database lacked
