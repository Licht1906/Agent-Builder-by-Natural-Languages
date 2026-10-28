""" "Xuất gói chạy độc lập" — DS-19 / ADR-0059 §10.

An approved agent that reads a Hệ CSDL (its DSN in the platform's secret
store), sends mail after an approval and searches a shelf of documents,
exported through `GET .../package`. The ticket's criteria at the API:

- a version not yet approved cannot be exported (the button is locked on
  the Deploy tab; the route refuses too);
- the zip, unpacked and given its secrets, loads as a package — the CI smoke
  test (`scripts/smoke_package.sh`) then runs it with `docker compose up`;
- no DSN, password or token is anywhere in it.
"""

import io
import json
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.config import get_settings
from src.datasources.access import from_yaml
from src.db.base import engine
from src.db.models import Agent, AgentVersion, DatabaseDialect, DatabaseSource, KnowledgeBase, User, VersionStatus
from src.services.secret_store import EncryptedDbSecretStore
from src.standalone.config import load_runtime_config, resolve_secrets, secret_source
from src.standalone.package import load_package
from src.workflow.documents import store_document
from src.workflow.tools import GMAIL
from tests.conftest import auth_header, discard_agent
from tests.test_standalone.test_standalone_runtime import ACCESS, SPEC, _views

pytestmark = pytest.mark.asyncio

__all__ = ["_views"]

DSN_PASSWORD = "Mat-Khau-Rat-Bi-Mat-9921"
HANDBOOK = "# Sổ tay\n\n## Điều 1. Nghỉ phép\n\nNhân viên được nghỉ phép năm 12 ngày.\n"


def _dsn() -> str:
    return engine.url.set(drivername="postgresql", password=DSN_PASSWORD).render_as_string(hide_password=False)


def _workflow(source_id: str, shelf_id: str) -> dict[str, Any]:
    def edge(index: int, source: str, port: str, target: str) -> dict[str, Any]:
        return {"id": f"e{index}", "source_node_id": source, "source_port": port, "target_node_id": target}

    return {
        "start_node_id": "search",
        "nodes": [
            {"id": "search", "type": "knowledge_search", "config": {"source_ids": [shelf_id]}},
            {"id": "query", "type": "data_query", "config": {"source_id": source_id, "views": ["v_sa_ar"]}},
            {"id": "draft", "type": "agent_llm", "config": {"prompt": "Soạn mail nhắc nợ.", "model": "gpt-4o-mini"}},
            {
                "id": "approval",
                "type": "human_approval",
                "config": {"approver_role": "manager", "message_template": "Gửi mail này?"},
            },
            {"id": "send", "type": "mcp", "config": {"tool_id": GMAIL, "action": "send_email"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            edge(1, "search", "found", "query"),
            edge(2, "query", "done", "draft"),
            edge(3, "draft", "done", "approval"),
            edge(4, "approval", "approved", "send"),
            edge(5, "approval", "rejected", "finish"),
            edge(6, "send", "done", "finish"),
        ],
        "metadata": {},
    }


@pytest.fixture
def exported_agent(db_session: Session, seeded_creator: User) -> Iterator[Callable[..., AgentVersion]]:
    """An agent of the creator's reading a Hệ CSDL whose DSN sits in the secret store, at the status asked for."""
    made: list[tuple[Agent, DatabaseSource, KnowledgeBase]] = []

    def _make(
        status: VersionStatus = VersionStatus.APPROVED, *, rules: bool = True, dialect=DatabaseDialect.POSTGRESQL
    ) -> AgentVersion:
        source = DatabaseSource(
            owner_id=seeded_creator.id,
            name="ERP mẫu",
            dialect=dialect,
            dsn_secret_ref=f"ds19-dsn-{len(made)}",
            allowed_views=["v_sa_ar", "v_sa_staff"],
            view_columns={
                "v_sa_ar": ["customer_id", "sales_rep_id", "department", "days_overdue"],
                "v_sa_staff": ["email", "emp_id", "dept_code", "position"],
            },
            access_rules=ACCESS if rules else None,
        )
        shelf = KnowledgeBase(owner_id=seeded_creator.id, name="Sổ tay DS-19")
        db_session.add_all([source, shelf])
        db_session.flush()
        EncryptedDbSecretStore(db=db_session).put(source.dsn_secret_ref, _dsn())
        store_document(db_session, shelf, "so-tay.md", HANDBOOK.encode())
        agent = Agent(owner_id=seeded_creator.id, name="Trợ lý công nợ")
        db_session.add(agent)
        db_session.flush()
        version = AgentVersion(
            agent_id=agent.id, solution_spec=SPEC, workflow=_workflow(str(source.id), str(shelf.id)), status=status
        )
        db_session.add(version)
        db_session.commit()
        made.append((agent, source, shelf))
        return version

    yield _make
    for agent, source, shelf in made:
        discard_agent(db_session, agent)
        EncryptedDbSecretStore(db=db_session).delete(source.dsn_secret_ref)
        db_session.delete(db_session.merge(source))
        db_session.delete(db_session.merge(shelf))
        db_session.commit()


async def _package(client: AsyncClient, version: AgentVersion, token: str, **params: Any) -> Any:
    return await client.get(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/package", params=params, headers=auth_header(token)
    )


def _files(response: Any) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# --------------------------------------------------------------------------- #
# Only an approved version
# --------------------------------------------------------------------------- #


async def test_a_version_not_yet_approved_cannot_be_exported(
    client: AsyncClient, creator_token: str, exported_agent: Callable[..., AgentVersion]
) -> None:
    version = exported_agent(VersionStatus.PENDING_APPROVAL)

    options = await client.get(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/package/options", headers=auth_header(creator_token)
    )
    refused = await _package(client, version, creator_token)

    assert options.json() == {"approved": False, "airgap_available": False}
    assert refused.status_code == 409 and "đã duyệt" in refused.json()["detail"]


async def test_someone_elses_agent_is_not_theirs_to_export(
    client: AsyncClient, admin_token: str, exported_agent: Callable[..., AgentVersion]
) -> None:
    refused = await _package(client, exported_agent(), admin_token)

    assert refused.status_code in (403, 404)


# --------------------------------------------------------------------------- #
# What the zip holds
# --------------------------------------------------------------------------- #


async def test_the_zip_holds_everything_a_company_needs_and_nothing_secret(
    client: AsyncClient, creator_token: str, exported_agent: Callable[..., AgentVersion]
) -> None:
    version = exported_agent()

    response = await _package(client, version, creator_token)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    files = _files(response)
    shelf = json.loads(files["bundle.json"])["workflow"]["nodes"][0]["config"]["source_ids"][0]
    assert {"bundle.json", "runtime.yaml", "access.yaml", "docker-compose.yml", ".env.example", "README.md"} <= set(
        files
    )
    assert f"documents/{shelf}/so-tay.md" in files
    assert files[f"documents/{shelf}/so-tay.md"].decode() == HANDBOOK

    everything = b"\n".join(files.values())
    for secret in (DSN_PASSWORD.encode(), _dsn().encode()):
        assert secret not in everything, "a DSN or its password left the platform"
    assert b"AGENT_SECRET_DSN_" in files[".env.example"]
    assert all(
        not line.split(b"=", 1)[1].strip() for line in files[".env.example"].splitlines() if line.startswith(b"AGENT_")
    )


async def test_the_written_runtime_yaml_and_access_yaml_are_this_agents(
    client: AsyncClient, creator_token: str, exported_agent: Callable[..., AgentVersion], tmp_path: Path
) -> None:
    version = exported_agent()
    files = _files(await _package(client, version, creator_token))
    (tmp_path / "runtime.yaml").write_bytes(files["runtime.yaml"])

    config = load_runtime_config(tmp_path / "runtime.yaml")
    bundle = json.loads(files["bundle.json"])
    [source] = config.data_sources

    assert source.id == bundle["workflow"]["nodes"][1]["config"]["source_id"]
    assert (source.dialect, source.access, source.dsn_secret) == ("postgresql", "access.yaml", f"dsn_{source.id}")
    assert from_yaml(files["access.yaml"].decode()).model_dump(mode="json") == from_yaml(
        yaml.safe_dump(ACCESS)
    ).model_dump(mode="json")
    assert config.mail is not None and config.mail.mode == "smtp", "the agent sends mail, so the channel is written"
    assert config.embedding is not None and config.qdrant is not None, "the agent searches documents"
    assert "gpt-4o-mini" in config.llm.models
    assert "CẢNH BÁO" in files["runtime.yaml"].decode()
    assert bundle["database_schema"][source.id]["v_sa_ar"] == [
        "customer_id",
        "sales_rep_id",
        "department",
        "days_overdue",
    ]


async def test_unpacked_and_given_its_secrets_the_zip_loads_as_a_package(
    client: AsyncClient,
    creator_token: str,
    exported_agent: Callable[..., AgentVersion],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What `docker compose up` does first, done here: read runtime.yaml, resolve secrets, load and check the bundle."""
    version = exported_agent()
    with zipfile.ZipFile(io.BytesIO((await _package(client, version, creator_token)).content)) as archive:
        archive.extractall(tmp_path)
    config = load_runtime_config(tmp_path / "runtime.yaml")
    monkeypatch.setenv("AGENT_SECRET_RUNTIME_DB", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv(
        f"AGENT_SECRET_DSN_{config.data_sources[0].id}",
        engine.url.set(drivername="postgresql").render_as_string(hide_password=False),
    )
    monkeypatch.setenv("AGENT_SECRET_LLM_KEY", "khong-can")
    monkeypatch.setenv("AGENT_SECRET_SMTP_PASS", "mat-khau-smtp")
    config = config.model_copy(update={"trusted_hosts": [str(engine.url.host)]})

    secrets = resolve_secrets(config, secret_source(config.secrets, base=tmp_path))
    package = load_package(config, secrets, base=tmp_path)

    assert package.name == "Trợ lý công nợ"
    assert set(package.sources) == {config.data_sources[0].id}
    assert package.shelves == (
        json.loads((tmp_path / "bundle.json").read_text(encoding="utf-8"))["workflow"]["nodes"][0]["config"][
            "source_ids"
        ][0],
    )


async def test_a_mysql_source_gets_its_notes_and_a_postgres_one_does_not(
    client: AsyncClient, creator_token: str, exported_agent: Callable[..., AgentVersion]
) -> None:
    postgres = _files(await _package(client, exported_agent(), creator_token))["README.md"].decode()
    mysql = _files(await _package(client, exported_agent(dialect=DatabaseDialect.MYSQL), creator_token))[
        "README.md"
    ].decode()

    assert "Lưu ý MySQL" in mysql and "Lưu ý MySQL" not in postgres
    for step in ("Secret", "Trỏ LLM nội bộ", "Nối SSO"):
        assert step in postgres


async def test_a_source_without_access_rules_cannot_be_packaged(
    client: AsyncClient, creator_token: str, exported_agent: Callable[..., AgentVersion]
) -> None:
    refused = await _package(client, exported_agent(rules=False), creator_token)

    assert refused.status_code == 409 and "quy tắc truy cập" in refused.json()["detail"]


# --------------------------------------------------------------------------- #
# Kèm images cho air-gap
# --------------------------------------------------------------------------- #


async def test_air_gap_images_go_in_only_when_an_admin_provided_them(
    client: AsyncClient,
    creator_token: str,
    exported_agent: Callable[..., AgentVersion],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = exported_agent()
    refused = await _package(client, version, creator_token, airgap="true")
    assert refused.status_code == 409 and "package_runtime_images.sh" in refused.json()["detail"]

    images = tmp_path / "agent-runtime-images-abc123.tar"
    images.write_bytes(b"docker save output")
    images.with_name(images.name + ".sig").write_bytes(b"signature")
    monkeypatch.setattr(get_settings(), "package_images_path", str(images))
    options = await client.get(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/package/options", headers=auth_header(creator_token)
    )
    response = await _package(client, version, creator_token, airgap="true")

    assert options.json() == {"approved": True, "airgap_available": True}
    files = _files(response)
    assert files["images/agent-runtime-images-abc123.tar"] == b"docker save output"
    assert "images/agent-runtime-images-abc123.tar.sig" in files
    assert b"verify-blob" in files["images/load_images.sh"]
    assert "load_images.sh" in files["README.md"].decode()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.getinfo("images/agent-runtime-images-abc123.tar").compress_type == zipfile.ZIP_STORED
