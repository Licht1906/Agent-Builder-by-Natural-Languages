"""The `database_sources` row and the DSN behind it — DS-02 / ADR-0059 §2.

`src.workflow.external_connections`' split, kept: this module owns the row —
creating one, listing an owner's own, changing one, deleting one, running
`/test` against it — and `src.api.database_source_routes` turns rows into
responses or 404s. Every byte that leaves the process goes through a dialect
(`src.datasources.dialects`); this module only resolves the DSN and writes the
verdict back.

**The DSN is written once and read only to dial.** It goes into the secret
store the moment it arrives (`create_database_source`, `replace_dsn`) and is
read back in exactly one place, `run_database_test`, straight into the
dialect. No function here returns it.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.datasources.access import (
    AccessRules,
    Deny,
    DenyCause,
    Principal,
    Scope,
    access_problems,
    scope_for,
)
from src.datasources.dialects import (
    ColumnRef,
    DatabaseProbeError,
    Introspection,
    ProbeFailure,
    ViewInfo,
    dialect_for,
    schema_problems,
)
from src.db.models import Agent, AgentVersion, DatabaseDialect, DatabaseSource, DatabaseSourceStatus
from src.models.solution_spec import AgentSolutionSpecification, DataSourceKind
from src.services.secret_store import SecretNotFoundError, SecretStore, SecretUnreadableError


class AllowedViewsError(ValueError):
    """The views a creator ticked are not all views the last probe saw — one sentence per name, joined."""


class ColumnNotReadableError(ValueError):
    """A column a caller asked to read that is not on a ticked view the last probe saw — refused before dialling."""


class AccessRulesError(ValueError):
    """Rules that name a view or column this Hệ CSDL does not have or does not allow — one sentence each."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(problems))


class DatabaseSourceInUseError(ValueError):
    """≥1 agent's spec still reads from this Hệ CSDL — the ticket's "xoá … đang được dùng → bị chặn".

    No `confirm=True` past it, `ExternalConnectionInUseError`'s reason: a spec
    whose `database` source points at nothing is a workflow that cannot be
    generated, and a confirmed spec cannot be edited to point elsewhere.
    """

    def __init__(self, agents: Sequence[str]) -> None:
        self.agents = list(agents)
        super().__init__(f"Không thể xoá — Hệ CSDL này đang được dùng bởi agent: {', '.join(agents)}")


def create_database_source(
    db: Session, owner_id: int, *, name: str, dialect: DatabaseDialect, dsn: str, secret_store: SecretStore
) -> DatabaseSource:
    """A fresh, `untested` Hệ CSDL owned by `owner_id`, its DSN already in the secret store. Never dials out.

    Keyword-only past the owner, `create_external_connection`'s shape: the
    three values are the request's own fields, and naming them at the call
    site is what keeps a `dsn` from being passed where a `name` was meant.
    """
    source = DatabaseSource(owner_id=owner_id, name=name, dialect=dialect)
    db.add(source)
    db.flush()
    _store_dsn(source, dsn, secret_store)
    return _persist(db, source)


def list_database_sources(db: Session, owner_id: int) -> Sequence[DatabaseSource]:
    """Every Hệ CSDL `owner_id` owns, newest first."""
    return (
        db.execute(select(DatabaseSource).where(DatabaseSource.owner_id == owner_id).order_by(DatabaseSource.id.desc()))
        .scalars()
        .all()
    )


def rename_database_source(db: Session, source: DatabaseSource, name: str) -> DatabaseSource:
    """`source` under a new name. Nothing else about it changes — a spec names a Hệ CSDL by id, not by name."""
    source.name = name
    return _persist(db, source)


def replace_dsn(db: Session, source: DatabaseSource, dsn: str, *, secret_store: SecretStore) -> DatabaseSource:
    """A new DSN for `source` — what a row carried over from `uc1_connections` needs before anything else.

    The verdict goes back to `untested`: the last probe described the old DSN,
    and keeping `connected` would claim something nobody has checked. The
    snapshot stays, so the ticks still show against the views last seen.
    """
    _store_dsn(source, dsn, secret_store)
    _apply(source, DatabaseSourceStatus.UNTESTED, reason=None, detail=None)
    source.last_probed_at = None
    return _persist(db, source)


def set_allowed_views(db: Session, source: DatabaseSource, views: Sequence[str]) -> DatabaseSource:
    """Keep the views a creator ticked — refused unless the last probe saw every one of them as a view.

    Checked against the snapshot rather than a fresh dial, so ticking a box is
    not a network call; `schema_problems` is the same check the package's
    start-up runs (DS-15), so a base table is refused here in the words it
    will be refused in there (UC1-09's rule).
    """
    if not source.view_columns and not source.base_tables:
        raise AllowedViewsError("Kiểm tra kết nối trước để thấy danh sách view rồi mới chọn")
    problems = schema_problems(snapshot_of(source), {view: [] for view in views})
    if problems:
        raise AllowedViewsError("; ".join(problems))
    source.allowed_views = list(dict.fromkeys(views))
    return _persist(db, source)


def delete_database_source(db: Session, source: DatabaseSource, *, secret_store: SecretStore) -> None:
    """Remove `source` and its DSN — refused while any agent's spec still names it."""
    using = _agents_reading_from(db, source)
    if using:
        raise DatabaseSourceInUseError(using)
    if source.dsn_secret_ref:
        secret_store.delete(source.dsn_secret_ref)
    db.delete(source)
    db.commit()


def run_database_test(
    db: Session, source: DatabaseSource, *, secret_store: SecretStore, trusted_hosts: frozenset[str]
) -> DatabaseSource:
    """`POST .../test` for real: resolve the DSN, probe, and keep what was found. Never raises a probe failure.

    A source with no DSN is `needs_dsn` rather than `failed`: nothing was
    tried, and the one thing to do about it is different (`run_connection_test`'s
    split between a missing credential and a broken one).
    """
    try:
        dsn = _dsn_of(source, secret_store)
        found = dialect_for(source.dialect).introspect(dsn, trusted_hosts=trusted_hosts)
    except DatabaseProbeError as exc:
        status = DatabaseSourceStatus.NEEDS_DSN if exc.reason in _NO_DSN else DatabaseSourceStatus.FAILED
        _apply(source, status, reason=exc.reason, detail=str(exc))
    else:
        _keep_snapshot(source, found)
    source.last_probed_at = datetime.now(UTC).replace(tzinfo=None)
    return _persist(db, source)


def snapshot_of(source: DatabaseSource) -> Introspection:
    """The last probe, rebuilt from the row — what `schema_problems` and a response read without dialling."""
    views = tuple(
        ViewInfo(name=name, columns=tuple(columns), row_count=source.view_row_counts.get(name))
        for name, columns in source.view_columns.items()
    )
    return Introspection(views=views, base_tables=tuple(source.base_tables))


def access_rules_of(source: DatabaseSource) -> AccessRules | None:
    """`source`'s quy tắc truy cập, or `None` — which `scope_for` callers must read as "deny everyone"."""
    return AccessRules.model_validate(source.access_rules) if source.access_rules else None


def current_access_problems(source: DatabaseSource) -> list[str]:
    """What is wrong with the saved rules *now* — a view unticked or dropped since they were saved shows here."""
    rules = access_rules_of(source)
    return [] if rules is None else _problems_with(source, rules)


def save_access_rules(db: Session, source: DatabaseSource, rules: AccessRules) -> DatabaseSource:
    """Keep `rules` — refused, naming every view and column, unless they fit the last probe and the ticked views."""
    problems = _problems_with(source, rules)
    if problems:
        raise AccessRulesError(problems)
    source.access_rules = rules.model_dump(mode="json")
    return _persist(db, source)


def scope_on(source: DatabaseSource, principal: Principal | None, view: str) -> Scope:
    """What `principal` may see of `view` on this Hệ CSDL — the one door `data_query` (DS-04) asks through.

    `scope_for` plus the three denies only the row can know: no rules saved;
    rules that no longer fit the ticked views and the last probe (a view
    unticked or a column dropped after saving — the whole source closes,
    rather than trusting the half that still fits); a view that is not
    ticked at all. Mặc định cấm, here as in the engine.
    """
    rules = access_rules_of(source)
    if rules is None:
        return Deny(cause=DenyCause.NO_RULES)
    if _problems_with(source, rules):
        return Deny(cause=DenyCause.RULES_OUT_OF_DATE)
    if view not in source.allowed_views:
        return Deny(cause=DenyCause.VIEW_NOT_ALLOWED)
    return scope_for(rules, principal, view)


def distinct_column_values(
    source: DatabaseSource, column: ColumnRef, *, secret_store: SecretStore, trusted_hosts: frozenset[str]
) -> list[Any]:
    """The distinct values of a ticked view's column — the positions the access form offers. Dials out.

    The view must be ticked and the column must be in the last probe before
    anything is sent: this reads a creator's real data, and only what they
    already said an agent may read.
    """
    if column.view not in source.allowed_views:
        raise ColumnNotReadableError(f"view {column.view} chưa được tick cho phép")
    if column.column not in source.view_columns.get(column.view, []):
        raise ColumnNotReadableError(f"view {column.view} không có cột {column.column}")
    dsn = _dsn_of(source, secret_store)
    return dialect_for(source.dialect).distinct_values(dsn, column, trusted_hosts=trusted_hosts)


def unowned_database_refs(db: Session, owner_id: int, spec: AgentSolutionSpecification) -> list[str]:
    """Every `database` ref in `spec` that names no Hệ CSDL `owner_id` owns — `ensure_knowledge_bases_owned`'s rule.

    The spec screen only offers a creator's own, but the API is the boundary:
    a ref is an id, and an id typed by hand could name somebody else's ERP.
    """
    refs = [source.ref for source in spec.data if source.kind is DataSourceKind.DATABASE and source.ref is not None]
    owned = {
        str(source_id)
        for source_id in db.execute(select(DatabaseSource.id).where(DatabaseSource.owner_id == owner_id)).scalars()
    }
    return [ref for ref in refs if ref not in owned]


#: The probe failures that mean "no DSN to dial" rather than "a DSN that failed".
_NO_DSN = frozenset({ProbeFailure.DSN_MISSING, ProbeFailure.DSN_UNREADABLE})


def _problems_with(source: DatabaseSource, rules: AccessRules) -> list[str]:
    """`access_problems` against this row's ticked views and last probe — the one way the check is asked."""
    return access_problems(rules, allowed_views=source.allowed_views, snapshot=snapshot_of(source))


def _dsn_of(source: DatabaseSource, secret_store: SecretStore) -> str:
    """The plaintext DSN, for the dialect only. Raises `DatabaseProbeError` when there is none to read."""
    if not source.dsn_secret_ref:
        raise DatabaseProbeError(ProbeFailure.DSN_MISSING)
    try:
        return secret_store.get(source.dsn_secret_ref)
    except SecretNotFoundError:
        raise DatabaseProbeError(ProbeFailure.DSN_MISSING) from None
    except SecretUnreadableError:
        raise DatabaseProbeError(ProbeFailure.DSN_UNREADABLE) from None


def _keep_snapshot(source: DatabaseSource, found: Introspection) -> None:
    """Record a successful probe — and untick whatever it shows is no longer a view.

    A tick that names a base table or a view since dropped is exactly what
    `set_allowed_views` refuses, so leaving it standing would let a probe
    launder one in: a row carried over from `uc1_connections`, or a view an
    ERP admin turned into a table. Said in the verdict, not done silently.
    """
    source.view_columns = {view.name: list(view.columns) for view in found.views}
    source.view_row_counts = {view.name: view.row_count for view in found.views if view.row_count is not None}
    source.base_tables = list(found.base_tables)
    still_views = [name for name in source.allowed_views if name in source.view_columns]
    dropped = [name for name in source.allowed_views if name not in source.view_columns]
    source.allowed_views = still_views
    detail = f"Kết nối thành công — {len(found.views)} view"
    if dropped:
        detail += f"; đã bỏ chọn {', '.join(dropped)} vì không còn là view"
    _apply(source, DatabaseSourceStatus.CONNECTED, reason=None, detail=detail)


def _apply(
    source: DatabaseSource, status: DatabaseSourceStatus, *, reason: ProbeFailure | None, detail: str | None
) -> None:
    """The one place a probe verdict is written — status, reason and sentence always move together."""
    source.last_probe_status = status
    source.last_probe_reason = reason.value if reason else None
    source.last_probe_detail = detail


def _store_dsn(source: DatabaseSource, dsn: str, secret_store: SecretStore) -> None:
    """Put `dsn` behind this source's key, minting the key the first time. `source` must have an id."""
    if not source.dsn_secret_ref:
        source.dsn_secret_ref = f"database_source:{source.id}:dsn"
    secret_store.put(source.dsn_secret_ref, dsn)


def _agents_reading_from(db: Session, source: DatabaseSource) -> list[str]:
    """The names of `source`'s owner's agents with any version whose spec has a `database` source naming it.

    Read off the stored JSON rather than `AgentSolutionSpecification`: a
    version mid-interview holds a partial spec that would not validate, and
    it still names this source. A `data_query` node's `source_id` (DS-04)
    joins this check when that node exists.
    """
    ref = str(source.id)
    rows = db.execute(
        select(Agent.name, AgentVersion.solution_spec)
        .join(AgentVersion, AgentVersion.agent_id == Agent.id)
        .where(Agent.owner_id == source.owner_id, AgentVersion.solution_spec.is_not(None))
    ).all()
    names = [
        name
        for name, spec in rows
        if any(
            isinstance(entry, dict) and entry.get("kind") == DataSourceKind.DATABASE and entry.get("ref") == ref
            for entry in (spec or {}).get("data", [])
        )
    ]
    return list(dict.fromkeys(names))


def _persist(db: Session, source: DatabaseSource) -> DatabaseSource:
    db.commit()
    db.refresh(source)
    return source
