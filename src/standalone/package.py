"""One exported agent, loaded and ready to run — DS-13.

`load_package` turns the three files (`bundle.json`, `access.yaml`,
`runtime.yaml`) and the resolved secrets into everything a run needs:

- the workflow, checked the way the platform's runtime checks it before a
  stream opens (`ensure_runnable`), and every `data_query` node pointed at a
  Hệ CSDL `runtime.yaml` declares;
- one in-memory `DatabaseSource` per Hệ CSDL — its access rules from
  `access.yaml`, its views and columns from probing the real database at
  start-up — read through the platform's own `PlatformDatabaseReader`, so
  scope, deny-by-default and citations are the platform's, not a copy;
- the package's own Postgres prepared: only the tables a run writes
  (approvals, audit) and what their keys point at, and one agent and one
  version standing for the bundle, so those rows have something to belong to.

Imports the ORM, so it must be imported only once `DATABASE_URL` names the
package's Postgres — see `__main__`.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Engine, Table, select, text
from sqlalchemy.orm import Session

from src.agents.portability import BUNDLE_FORMAT
from src.datasources.access import AccessRules, from_yaml
from src.datasources.dialects import DatabaseProbeError, dialect_for
from src.datasources.reader import PlatformDatabaseReader
from src.db.base import Base, SessionLocal, engine
from src.db.models import (
    Agent,
    AgentVersion,
    DatabaseDialect,
    DatabaseSource,
    DatabaseSourceStatus,
    User,
    UserRole,
    VersionStatus,
)
from src.models.node_registry import DataQueryConfig, NodeType
from src.models.solution_spec import AgentSolutionSpecification
from src.models.workflow import WorkflowJSON
from src.services.secret_store import SecretNotFoundError
from src.standalone.config import RuntimeConfig, RuntimeConfigError
from src.workflow.runtime import ensure_runnable

#: The account the package's agent belongs to. Not a person: nobody signs in as it.
PACKAGE_OWNER_EMAIL = "package-owner@agent.local"

#: What a run in the package writes, plus — added by `_with_dependencies` — whatever those rows point at.
#: `deployments` because `Agent.deployed_version_id` reads it whenever an agent is loaded.
_PACKAGE_TABLES = (
    "users",
    "agents",
    "agent_versions",
    "deployments",
    "run_approvals",
    "audit_events",
    "audit_chain_head",
)


@dataclass(frozen=True, slots=True)
class PackageIds:
    """The rows standing for the bundle in the package's Postgres."""

    owner_id: int
    agent_id: int
    version_id: int


@dataclass(frozen=True, slots=True)
class Package:
    """Everything one exported agent needs to run, loaded once at start-up."""

    config: RuntimeConfig
    name: str
    spec: AgentSolutionSpecification
    workflow: WorkflowJSON
    sources: Mapping[str, DatabaseSource]
    secrets: Mapping[str, str]
    trusted_hosts: frozenset[str]
    warnings: list[str] = field(default_factory=list)


class ResolvedSecrets:
    """`SecretStore`'s read half over the secrets resolved at start-up — what `dsn_of` reads a DSN through."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def get(self, key: str) -> str:
        if key not in self._values:
            raise SecretNotFoundError(key)
        return self._values[key]

    def put(self, key: str, value: str) -> None:  # pragma: no cover - the package never writes secrets
        raise RuntimeError("Gói không ghi secret")

    def delete(self, key: str) -> None:  # pragma: no cover
        raise RuntimeError("Gói không xoá secret")

    def rotate(self, key: str, value: str) -> None:  # pragma: no cover
        raise RuntimeError("Gói không đổi secret")


@dataclass(frozen=True, slots=True)
class PackageDatabaseReader(PlatformDatabaseReader):
    """The platform's reader over the package's in-memory sources instead of `database_sources` rows.

    Every gate is `PlatformDatabaseReader`'s own — the query check, the closed
    deny, the asker lookup, `scope_on` — because only where a source comes from
    differs. `asked_by` keeps `sources` (it is a `dataclasses.replace`).
    """

    sources: Mapping[str, DatabaseSource] = field(default_factory=dict)

    def _source(self, source_id: str) -> DatabaseSource | None:
        return self.sources.get(source_id)


def load_package(config: RuntimeConfig, secrets: Mapping[str, str], *, base: Path) -> Package:
    """The bundle, its Hệ CSDL and its rules, checked — `RuntimeConfigError` naming every problem otherwise."""
    name, spec, workflow = _bundle(_path(config.bundle, base))
    trusted = frozenset(config.trusted_hosts)
    declared = {source.id: source for source in config.data_sources}
    read_by = _data_query_views(workflow)
    unknown = sorted(set(read_by) - set(declared))
    if unknown:
        raise RuntimeConfigError(
            "Workflow đọc Hệ CSDL chưa khai trong runtime.yaml (data_sources[].id): " + ", ".join(unknown)
        )
    sources: dict[str, DatabaseSource] = {}
    warnings: list[str] = []
    for source_id, declared_source in declared.items():
        rules = _access_rules(_path(declared_source.access, base))
        dsn = secrets[declared_source.dsn_secret]
        dialect = DatabaseDialect(declared_source.dialect)
        try:
            found = dialect_for(dialect).introspect(dsn, trusted_hosts=trusted)
        except DatabaseProbeError as exc:
            raise RuntimeConfigError(f"Không đọc được Hệ CSDL {declared_source.name}: {exc}") from None
        wanted = {*read_by.get(source_id, ()), rules.identity.view}
        present = {view.name: list(view.columns) for view in found.views}
        missing = sorted(wanted - set(present))
        if missing:
            # DS-15 turns this into a refusal to start; until then the read is refused, by name.
            warnings.append(f"Hệ CSDL {declared_source.name} không có view: {', '.join(missing)}")
        allowed = sorted(view for view in wanted if view in present)
        sources[source_id] = DatabaseSource(
            id=int(source_id) if source_id.isdigit() else 0,
            owner_id=0,
            name=declared_source.name,
            dialect=dialect,
            dsn_secret_ref=declared_source.dsn_secret,
            allowed_views=allowed,
            view_columns={view: present[view] for view in allowed},
            view_row_counts={},
            base_tables=list(found.base_tables),
            last_probe_status=DatabaseSourceStatus.CONNECTED,
            access_rules=rules.model_dump(mode="json"),
        )
    if any(node.type is NodeType.KNOWLEDGE_SEARCH for node in workflow.nodes):
        warnings.append(
            "Workflow có node knowledge_search — gói chưa mang tài liệu theo (DS-18), node đó sẽ không tìm thấy gì"
        )
    return Package(
        config=config,
        name=name,
        spec=spec,
        workflow=workflow,
        sources=sources,
        secrets=dict(secrets),
        trusted_hosts=trusted,
        warnings=warnings,
    )


def reader_for(package: Package, asker: str | None) -> PackageDatabaseReader:
    """The reader one run reads through — scoped to `asker`, as on the platform."""
    return PackageDatabaseReader(
        db=None,  # type: ignore[arg-type] — `_source` is overridden; the reader never queries the ORM here
        owner_id=0,
        asker=asker,
        secret_store=ResolvedSecrets(package.secrets),
        trusted_hosts=package.trusted_hosts,
        sources=package.sources,
    )


def prepare_database(package: Package, *, bind: Engine | None = None) -> PackageIds:
    """The package's tables created if missing, and the rows standing for the bundle found or written.

    `bind` is the package's Postgres — `src.db.base.engine` unless a test hands it an empty one.
    """
    target = bind if bind is not None else engine
    Base.metadata.create_all(target, tables=package_tables())
    db: Session = Session(bind=target)
    try:
        owner = db.scalar(select(User).where(User.email == PACKAGE_OWNER_EMAIL))
        if owner is None:
            owner = User(email=PACKAGE_OWNER_EMAIL, password_hash="!", role=UserRole.CREATOR)
            db.add(owner)
            db.flush()
        agent = db.scalar(select(Agent).where(Agent.owner_id == owner.id, Agent.name == package.name))
        if agent is None:
            agent = Agent(owner_id=owner.id, name=package.name)
            db.add(agent)
            db.flush()
        workflow = package.workflow.model_dump(mode="json")
        version = next(
            (
                row
                for row in db.scalars(select(AgentVersion).where(AgentVersion.agent_id == agent.id))
                if row.workflow == workflow
            ),
            None,
        )
        if version is None:
            version = AgentVersion(
                agent_id=agent.id,
                solution_spec=package.spec.model_dump(mode="json"),
                workflow=workflow,
                status=VersionStatus.APPROVED,
            )
            db.add(version)
        db.commit()
        return PackageIds(owner_id=owner.id, agent_id=agent.id, version_id=version.id)
    finally:
        db.close()


def asker_user_id(asker: str) -> int:
    """The package's row for `asker` — who an approval was decided by, in the audit chain and on the row."""
    db: Session = SessionLocal()
    try:
        email = f"asker:{asker}"[:320]
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, password_hash="!", role=UserRole.CREATOR)
            db.add(user)
            db.commit()
        return user.id
    finally:
        db.close()


def database_ready() -> bool:
    """Whether the package's Postgres answers — what `/health` reports."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — health says no, it does not raise
        return False


def _bundle(path: Path) -> tuple[str, AgentSolutionSpecification, WorkflowJSON]:
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeConfigError(f"Không đọc được bundle {path}: {exc}") from None
    if document.get("format") != BUNDLE_FORMAT:
        raise RuntimeConfigError(f"Bundle {path} không phải định dạng {BUNDLE_FORMAT} (có: {document.get('format')!r})")
    try:
        spec = AgentSolutionSpecification.model_validate(document.get("solution_spec"))
        workflow = WorkflowJSON.model_validate(document.get("workflow"))
    except ValidationError as exc:
        raise RuntimeConfigError(f"Bundle {path} sai định dạng: {exc}") from None
    try:
        ensure_runnable(workflow)
    except ValueError as exc:
        raise RuntimeConfigError(f"Workflow trong bundle chưa chạy được: {exc}") from None
    return str(document.get("name") or "Agent"), spec, workflow


def _access_rules(path: Path) -> AccessRules:
    try:
        return from_yaml(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeConfigError(f"Không đọc được {path}: {exc}") from None
    except ValueError as exc:
        raise RuntimeConfigError(f"{path} sai định dạng: {exc}") from None


def _data_query_views(workflow: WorkflowJSON) -> dict[str, set[str]]:
    """`{source_id: views}` the workflow's `data_query` nodes read."""
    views: dict[str, set[str]] = {}
    for node in workflow.nodes_of_type(NodeType.DATA_QUERY):
        config = DataQueryConfig.model_validate(node.config)
        views.setdefault(config.source_id, set()).update(config.views)
    return views


def _path(value: str, base: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def package_tables() -> list[Table]:
    """Every table the package creates in its own Postgres."""
    return _with_dependencies(_PACKAGE_TABLES)


def _with_dependencies(names: Sequence[str]) -> list[Table]:
    """`names`' tables and every table their foreign keys reach, so `create_all` can create them alone."""
    tables = Base.metadata.tables
    wanted: dict[str, Table] = {}
    pending = list(names)
    while pending:
        table = tables[pending.pop()]
        if table.name in wanted:
            continue
        wanted[table.name] = table
        pending.extend(key.column.table.name for key in table.foreign_keys)
    return list(wanted.values())
