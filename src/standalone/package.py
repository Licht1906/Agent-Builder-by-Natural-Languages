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
- the real database checked against what the agent uses (DS-15): every view
  a `data_query` node reads, with the columns it had when the agent was built
  (the bundle's `database_schema`), and every view and column `access.yaml`
  names. Anything missing refuses the start, every gap named at once; a column
  the agent was not built with is a warning, and stays unread;
- the package's own Postgres prepared: only the tables a run writes
  (approvals, audit) and what their keys point at, and one agent and one
  version standing for the bundle, so those rows have something to belong to.

Imports the ORM, so it must be imported only once `DATABASE_URL` names the
package's Postgres — see `__main__`.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine, Table, select, text
from sqlalchemy.orm import Session

from src.agents.portability import BUNDLE_FORMAT
from src.datasources.access import AccessRules, WhereGrant, from_yaml
from src.datasources.dialects import DatabaseProbeError, Introspection, dialect_for, schema_problems
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
from src.models.node_registry import DataQueryConfig, KnowledgeSearchConfig, NodeType
from src.models.solution_spec import AgentSolutionSpecification
from src.models.workflow import WorkflowJSON
from src.services.secret_store import SecretNotFoundError
from src.standalone.config import RuntimeConfig, RuntimeConfigError
from src.standalone.documents import missing_shelves
from src.workflow.runtime import ensure_runnable, mcp_action_names, wire_name
from src.workflow.tools import GMAIL

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


#: The bundle's one action that leaves through the package's mail channel (DS-16).
MAIL_SEND = wire_name(GMAIL, "send_email")

#: `{source_id: {view: columns}}` — the views each Hệ CSDL had when the agent was built, as the bundle carries them.
DatabaseSchema = dict[str, dict[str, list[str]]]

_DATABASE_SCHEMA = TypeAdapter(DatabaseSchema)


@dataclass(frozen=True, slots=True)
class SchemaCheck:
    """One Hệ CSDL's real database against the agent: what it lacks, what it has beyond, what the agent may read.

    `missing` refuses the start; `warnings` are logged; `view_columns` is every
    view the package opens, cut down to what the agent was built with (plus
    what the rules filter on) wherever the bundle says what that was.
    """

    missing: list[str]
    warnings: list[str]
    view_columns: dict[str, list[str]]


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
    #: The directory `runtime.yaml` is in — what its relative paths (`auth.custom.module`) are relative to.
    base: Path = Path(".")
    #: The Knowledge Base ids the workflow's `knowledge_search` nodes search — folders under `documents/` (DS-18).
    shelves: tuple[str, ...] = ()


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
    name, spec, workflow, built_on = _bundle(_path(config.bundle, base))
    trusted = frozenset(config.trusted_hosts)
    declared = {source.id: source for source in config.data_sources}
    read_by = _data_query_views(workflow)
    unknown = sorted(set(read_by) - set(declared))
    if unknown:
        raise RuntimeConfigError(
            "Workflow đọc Hệ CSDL chưa khai trong runtime.yaml (data_sources[].id): " + ", ".join(unknown)
        )
    actions = mcp_action_names(workflow)
    if MAIL_SEND in actions and config.mail is None:
        raise RuntimeConfigError(
            f"Workflow gửi mail ({MAIL_SEND}) nhưng runtime.yaml chưa có mail — khai mail.mode: smtp | gmail_oauth | custom"
        )
    unreachable = [name for name in actions if name != MAIL_SEND]
    shelves = _searched_shelves(workflow)
    if shelves:
        _check_documents(config, shelves, base)
    sources: dict[str, DatabaseSource] = {}
    problems: list[str] = []
    warnings: list[str] = []
    for source_id, declared_source in declared.items():
        rules = _access_rules(_path(declared_source.access, base))
        dsn = secrets[declared_source.dsn_secret]
        dialect = DatabaseDialect(declared_source.dialect)
        try:
            found = dialect_for(dialect).introspect(dsn, trusted_hosts=trusted)
        except DatabaseProbeError as exc:
            problems.append(f"Không đọc được Hệ CSDL {declared_source.name}: {exc}")
            continue
        check = check_schema(found, rules=rules, read=read_by.get(source_id, ()), built_on=built_on.get(source_id, {}))
        problems.extend(f"Hệ CSDL {declared_source.name}: {problem}" for problem in check.missing)
        warnings.extend(f"Hệ CSDL {declared_source.name}: {warning}" for warning in check.warnings)
        sources[source_id] = DatabaseSource(
            id=int(source_id) if source_id.isdigit() else 0,
            owner_id=0,
            name=declared_source.name,
            dialect=dialect,
            dsn_secret_ref=declared_source.dsn_secret,
            allowed_views=list(check.view_columns),
            view_columns=check.view_columns,
            view_row_counts={},
            base_tables=list(found.base_tables),
            last_probe_status=DatabaseSourceStatus.CONNECTED,
            access_rules=rules.model_dump(mode="json"),
        )
    if problems:
        # Every gap at once, for the reason `resolve_secrets` names every missing secret: one fix, not one per restart.
        raise RuntimeConfigError(
            "DB thật thiếu thứ agent dùng — sửa DB hoặc access.yaml rồi khởi động lại:\n"
            + "\n".join(f"- {problem}" for problem in problems)
        )
    if config.auth.mode == "oidc" and config.auth.oidc is not None and not config.auth.oidc.client_id:
        warnings.append(
            "auth.oidc chưa có client_id — trang chat của gói không đăng nhập được, chỉ API /chat dùng được"
        )
    if unreachable:
        warnings.append(
            "Gói chỉ có kênh gửi mail; các action sau sẽ báo lỗi nếu workflow gọi tới: " + ", ".join(unreachable)
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
        base=base,
        shelves=tuple(shelves),
    )


def _searched_shelves(workflow: WorkflowJSON) -> list[str]:
    """The Knowledge Base ids every `knowledge_search` node searches, sorted."""
    return sorted(
        {
            str(ref)
            for node in workflow.nodes_of_type(NodeType.KNOWLEDGE_SEARCH)
            for ref in KnowledgeSearchConfig.model_validate(node.config).source_ids
        }
    )


def _check_documents(config: RuntimeConfig, shelves: Sequence[str], base: Path) -> None:
    """A workflow that searches documents needs them, an embedding endpoint and a Qdrant — all named at once."""
    root = _path(config.documents, base)
    problems = [f"thiếu thư mục {root / shelf} (kho tài liệu {shelf})" for shelf in missing_shelves(root, shelves)]
    if config.embedding is None:
        problems.append("runtime.yaml thiếu embedding (base_url, model)")
    if config.qdrant is None:
        problems.append("runtime.yaml thiếu qdrant (url)")
    if problems:
        raise RuntimeConfigError("Workflow tìm trong tài liệu nhưng: " + "; ".join(problems))


def check_schema(
    found: Introspection, *, rules: AccessRules, read: Iterable[str], built_on: Mapping[str, Sequence[str]]
) -> SchemaCheck:
    """One Hệ CSDL's real database against what the agent uses — DS-15 / ADR-0059 §4.

    Expected: each view in `read` (what the `data_query` nodes read) with the
    columns `built_on` says it had when the agent was built; the employee view
    with its match and attribute columns; each view a rule grants with the
    columns its `where` filters on. `schema_problems` names every one missing.

    A column the database has beyond that, on a view `built_on` describes, is
    a warning and is left out of `view_columns`: the agent was evaluated
    without it, and a column added to an ERP view is as likely to be a salary
    as a convenience. A view `built_on` does not describe (a hand-written
    bundle; the employee view) is opened whole — said so for a node's view.
    """
    read = sorted(set(read))
    expected: dict[str, list[str]] = {}

    def expect(view: str, columns: Iterable[str]) -> None:
        wanted = expected.setdefault(view, [])
        wanted.extend(column for column in columns if column not in wanted)

    for view in read:
        expect(view, built_on.get(view, ()))
    identity = rules.identity
    expect(identity.view, [identity.match_by, *identity.attributes.values()])
    for rule in rules.rules:
        for view, grant in rule.views.items():
            expect(view, grant.where if isinstance(grant, WhereGrant) else ())

    present = {view.name: list(view.columns) for view in found.views}
    warnings = [
        f"bundle không ghi cột của view {view} lúc dựng agent — không kiểm được cột, agent đọc mọi cột của view"
        for view in read
        if view not in built_on
    ]
    view_columns: dict[str, list[str]] = {}
    for view, wanted in expected.items():
        if view not in present:
            continue
        if view not in built_on:
            view_columns[view] = present[view]
            continue
        added = [column for column in present[view] if column not in wanted]
        if added:
            warnings.append(
                f"view {view} có cột mới so với lúc dựng agent: {', '.join(added)} — agent không đọc các cột này"
            )
        view_columns[view] = [column for column in present[view] if column in wanted]
    return SchemaCheck(missing=schema_problems(found, expected), warnings=warnings, view_columns=view_columns)


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


def _bundle(path: Path) -> tuple[str, AgentSolutionSpecification, WorkflowJSON, DatabaseSchema]:
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeConfigError(f"Không đọc được bundle {path}: {exc}") from None
    if document.get("format") != BUNDLE_FORMAT:
        raise RuntimeConfigError(f"Bundle {path} không phải định dạng {BUNDLE_FORMAT} (có: {document.get('format')!r})")
    try:
        spec = AgentSolutionSpecification.model_validate(document.get("solution_spec"))
        workflow = WorkflowJSON.model_validate(document.get("workflow"))
        built_on = _DATABASE_SCHEMA.validate_python(document.get("database_schema") or {})
    except ValidationError as exc:
        raise RuntimeConfigError(f"Bundle {path} sai định dạng: {exc}") from None
    try:
        ensure_runnable(workflow)
    except ValueError as exc:
        raise RuntimeConfigError(f"Workflow trong bundle chưa chạy được: {exc}") from None
    return str(document.get("name") or "Agent"), spec, workflow, built_on


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
