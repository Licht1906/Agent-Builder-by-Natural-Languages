"""What a `data_query` node reads through — DS-04 / ADR-0059 §5–6.

`DatabaseReader` is the runtime's seam, the way `KnowledgeStore` is for a
`knowledge_search` node: the run is handed one, bound to the creator who owns
the agent and to the person asking, and the node never reaches a database or
the secret store itself. A test hands it a fake; the platform hands it
`PlatformDatabaseReader`.

**Every read goes through the same four gates, in this order**, and the order
is the security argument:

1. the Hệ CSDL is the owner's, and the query fits the node's views and the
   columns the last probe saw (`query_problems`) — refused with the reason;
2. the source is open at all (`closed_deny`): rules saved, still fitting the
   ticked views, the view ticked — decided before anybody is looked up;
3. somebody is asking, and the employee view knows them (`resolve_principal`)
   — nobody asking is no query;
4. `scope_on` for that asker — `Deny` stops here; `All`/`Where` reach the
   dialect, the `Where` pairs ANDed into SQL the code builds.

**A write goes through the same four gates** (ADR-0094) — against the tables
ticked for writing and the rule's `tables` grants instead of views — plus one:
a value the write sets on a scope column must be the asker's own, so nobody
moves a row out of their scope or creates one in somebody else's. `authorize`
asks the gates without changing anything (what an approval card is prepared
from); `write` asks them again, fresh, and only then changes the database.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Annotated, Any, Protocol

from fastapi import Depends
from sqlalchemy.orm import Session

from src.config import get_settings
from src.datasources.access import (
    POSITION,
    WRITE_DENIED,
    All,
    Deny,
    DenyCause,
    Principal,
    Where,
    asker_of,
    resolve_principal,
)
from src.datasources.dialects import (
    ColumnRef,
    DatabaseProbeError,
    IdentitySearch,
    QueryRequest,
    QueryRows,
    WriteRequest,
    dialect_for,
)
from src.datasources.graph import owned_source
from src.datasources.query import StructuredQuery, effective_limit, query_problems
from src.datasources.sources import (
    access_rules_of,
    closed_deny,
    closed_write_deny,
    current_access_problems,
    dsn_of,
    scope_on,
    scope_on_write,
    write_dsn_of,
)
from src.datasources.write import StructuredWrite, WriteOp, describe_write, write_problems
from src.db.base import get_db
from src.db.models import Agent, DatabaseSource
from src.models.node_registry import DataQueryConfig, DataWriteConfig
from src.services.secret_store import SecretStore, get_secret_store

#: What an asker hears about a read that failed for any reason but permission.
#: The reason itself (a DSN problem, a column the model guessed) is the
#: creator's and stays on the transcript: some of it describes the database.
UNREAD_FOR_ASKER = "Không đọc được dữ liệu để trả lời câu hỏi này lúc này."

#: Said when a node's `source_id` names nothing its owner may read.
_NO_SOURCE = "Hệ CSDL của node này không tồn tại hoặc không thuộc về bạn"


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    """One read: rows, or why there are none — a refusal of the query, or a deny of the asker.

    Kept apart because they are told apart: a refusal is the *query's* fault
    and its reason goes back to the model and the transcript; a deny is the
    asker's lack of permission and the asker hears only `Deny.reason`.
    `source_name`/`view`/`description` are what a citation is built from.
    """

    rows: QueryRows | None
    source_name: str
    view: str
    description: str
    refusal: str | None = None
    denied: Deny | None = None
    #: The asker's scope ANDed into the read — `(column, value)` pairs, `()`
    #: for a rule of `all`, `None` when nothing was read. What the audit chain
    #: records as "bộ lọc phạm vi đã áp" (DS-10).
    scope: tuple[tuple[str, Any], ...] | None = None

    @property
    def transcript_reason(self) -> str:
        """Why there are no rows, for the creator's transcript — the refusal in full, or the deny's own sentence."""
        if self.denied is not None:
            return self.denied.reason
        return f"Không đọc được dữ liệu từ {self.view}: {self.refusal}"

    @property
    def asker_reason(self) -> str:
        """What the asker may hear: "không có quyền" for a deny — never its cause — and nothing technical otherwise."""
        return self.denied.reason if self.denied is not None else UNREAD_FOR_ASKER


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """One write — authorized, done, or why not: a refusal of the write, or a deny of the asker (ADR-0094).

    `affected` is the rows the database changed, `None` for a write that did
    not happen (refused, denied, or only authorized). `description` is the
    write in words, the line a transcript and an approval card read.
    """

    affected: int | None
    source_name: str
    table: str
    description: str
    refusal: str | None = None
    denied: Deny | None = None
    #: The asker's scope bound into the write — `()` for `all_rows`, `None` when it never got that far.
    scope: tuple[tuple[str, Any], ...] | None = None

    @property
    def allowed(self) -> bool:
        """Whether every gate let this write through — the answer `authorize` exists to give."""
        return self.refusal is None and self.denied is None

    @property
    def transcript_reason(self) -> str:
        """Why nothing changed, for the creator's transcript — the refusal in full, or the deny's own sentence."""
        if self.denied is not None:
            return self.denied.reason
        return f"Không ghi được vào {self.table}: {self.refusal}"

    @property
    def asker_reason(self) -> str:
        """What the asker may hear: "không có quyền" for a deny, nothing technical otherwise."""
        return self.denied.reason if self.denied is not None else UNWRITTEN_FOR_ASKER


#: What an asker hears about a write that failed for any reason but permission.
UNWRITTEN_FOR_ASKER = "Không thực hiện được thay đổi dữ liệu này lúc này — chưa có gì bị thay đổi."


@dataclass(frozen=True, slots=True)
class Asker:
    """Somebody a creator can run a trial as: the value matched against a sign-in, and their position."""

    value: str
    position: str | None
    source: str


#: How many people one "Chạy với tư cách…" search offers.
ASKER_LIMIT = 20


class DatabaseReader(Protocol):
    """The runtime's door into the Hệ CSDL a `data_query` node names — for one asker.

    `asker` is who every read is scoped to (`None`: nobody, so every read is
    denied). `asked_by` is how a route or the eval runner gets the same door
    for somebody else — the platform's "Chạy với tư cách…" (DS-05) and an eval
    case's `run_as` — without the runtime ever choosing who is asking.
    """

    @property
    def asker(self) -> str | None:
        """Whose rows every read is scoped to; `None` is nobody."""
        ...

    def asked_by(self, asker: str | None) -> "DatabaseReader":
        """This reader for `asker` instead — the same owner, store and trusted hosts."""
        ...

    def askers(self, configs: Sequence[DataQueryConfig], text: str) -> list[Asker]:
        """People the employee views behind `configs` know, matching `text` — "Chạy với tư cách…"'s list."""
        ...

    def columns_of(self, config: DataQueryConfig) -> dict[str, list[str]]:
        """`{view: columns}` the node may query — what the model's query schema offers. Empty for no usable source."""
        ...

    def read(self, config: DataQueryConfig, query: StructuredQuery) -> QueryOutcome:
        """Run `query` for whoever this reader was built for. Never raises a database failure — it refuses."""
        ...

    def tables_of(self, config: DataWriteConfig) -> dict[str, list[str]]:
        """`{table: columns}` the node may write — what the model's write schema offers. Empty for none (ADR-0094)."""
        ...

    def notes_of(self, source_id: str) -> str:
        """The creator's description of the Hệ CSDL for the model (ADR-0095) — `""` for none or no such source."""
        ...

    def authorize(self, config: DataWriteConfig, write: StructuredWrite) -> WriteOutcome:
        """Every gate `write` would pass, changing nothing — `allowed` or why not."""
        ...

    def write_grants(self, config: DataWriteConfig) -> dict[str, dict[str, Any]]:
        """`{table: {"columns": [...], "grants": {operation: {"scope": [[column, value]]} | {"denied": {...}}}}}`.

        What this asker may do to each of the node's tables, as JSON — what an
        eval cassette freezes so a case can prepare and authorize a write
        without a database (ADR-0094).
        """
        ...

    def write(self, config: DataWriteConfig, write: StructuredWrite) -> WriteOutcome:
        """The gates again, then the change. Never raises a database failure — it refuses."""
        ...


@dataclass(frozen=True, slots=True)
class PlatformDatabaseReader:
    """`DatabaseReader` over `database_sources`, for one owner and one asker — `asked_by` picks another."""

    db: Session
    owner_id: int
    asker: str | None
    secret_store: SecretStore
    trusted_hosts: frozenset[str]

    def asked_by(self, asker: str | None) -> "PlatformDatabaseReader":
        """This reader for `asker` — blank is nobody, the same as `None`."""
        return replace(self, asker=asker_of(asker))

    def askers(self, configs: Sequence[DataQueryConfig], text: str) -> list[Asker]:
        """Each source's employee view, searched once, deduplicated by the value a sign-in would match.

        Only a source whose rules are saved and still fit is searched: its
        employee view is one the creator ticked, and nothing else is read.
        A source that cannot be read right now contributes nobody rather than
        failing the whole list — it is a suggestion box, and free typing
        still works (DS-05).
        """
        found: dict[str, Asker] = {}
        for source_id in dict.fromkeys(config.source_id for config in configs):
            source = self._source(source_id)
            rules = access_rules_of(source) if source is not None else None
            if source is None or rules is None or rules.identity is None or current_access_problems(source):
                continue
            identity = rules.identity
            search = IdentitySearch(
                match=ColumnRef(view=identity.view, column=identity.match_by),
                label_column=identity.attributes[POSITION],
                text=text.strip(),
                limit=ASKER_LIMIT,
            )
            try:
                dsn = dsn_of(source, self.secret_store)
                pairs = dialect_for(source.dialect).identity_candidates(dsn, search, trusted_hosts=self.trusted_hosts)
            except DatabaseProbeError:
                continue
            for value, position in pairs:
                found.setdefault(str(value), Asker(value=str(value), position=position, source=source.name))
        return list(found.values())[:ASKER_LIMIT]

    def columns_of(self, config: DataQueryConfig) -> dict[str, list[str]]:
        """The node's views still ticked on its source, with the columns the last probe saw — `{}` for no source."""
        source = self._source(config.source_id)
        if source is None:
            return {}
        return {view: list(source.view_columns.get(view, [])) for view in _readable_views(source, config)}

    def read(self, config: DataQueryConfig, query: StructuredQuery) -> QueryOutcome:
        """The four gates in the module docstring, in order; the first that refuses is the outcome."""
        source = self._source(config.source_id)
        if source is None:
            return QueryOutcome(rows=None, source_name="", view=query.view, description="", refusal=_NO_SOURCE)
        problems = query_problems(query, views=_readable_views(source, config), columns_by_view=source.view_columns)
        if problems:
            return _outcome(source, query, refusal="; ".join(problems))
        closed = closed_deny(source, query.view)
        if closed is not None:
            return _outcome(source, query, denied=closed)
        try:
            return self._scoped_read(source, config, query)
        except DatabaseProbeError as exc:
            return _outcome(source, query, refusal=str(exc))

    def _scoped_read(self, source: DatabaseSource, config: DataQueryConfig, query: StructuredQuery) -> QueryOutcome:
        """Gates 3 and 4: who is asking, what they may see — then the query itself.

        An open Hệ CSDL (ADR-0095) skips gate 3: nobody needs looking up when
        everyone sees the same, so a trial with nobody picked reads too.
        """
        rules = access_rules_of(source)
        assert rules is not None  # `closed_deny` already denied a source without rules
        if not self.asker and not rules.open:
            return _outcome(source, query, denied=Deny(cause=DenyCause.UNKNOWN_ASKER))
        dsn = dsn_of(source, self.secret_store)
        dialect = dialect_for(source.dialect)
        principal = None
        if not rules.open:
            lookup = dialect.identity_lookup(dsn, trusted_hosts=self.trusted_hosts)
            principal = resolve_principal(rules, self.asker or "", lookup=lookup)
        scope = scope_on(source, principal, query.view)
        if isinstance(scope, Deny):
            return _outcome(source, query, denied=scope)
        request = _request(source, query, scope=scope, principal=principal, max_rows=config.max_rows)
        rows = dialect.run_query(dsn, request, trusted_hosts=self.trusted_hosts)
        return QueryOutcome(
            rows=rows,
            source_name=source.name,
            view=query.view,
            description=describe(query, scoped=bool(request.scope)),
            scope=tuple(request.scope),
        )

    def notes_of(self, source_id: str) -> str:
        """The owner's own description of the source, or `""`."""
        source = self._source(source_id)
        return (source.agent_notes or "") if source is not None else ""

    def tables_of(self, config: DataWriteConfig) -> dict[str, list[str]]:
        """The node's tables still ticked for writing on its source, with their columns — `{}` for no source."""
        source = self._source(config.source_id)
        if source is None:
            return {}
        return {table: list((source.table_columns or {}).get(table, [])) for table in _writable_tables(source, config)}

    def authorize(self, config: DataWriteConfig, write: StructuredWrite) -> WriteOutcome:
        """The gates, and nothing else — what a `human_approval` asks before it shows a write on its card."""
        checked = self._checked_write(config, write)
        if isinstance(checked, WriteOutcome):
            return checked
        return _write_outcome(checked.source, write, scope=checked.request.scope)

    def write(self, config: DataWriteConfig, write: StructuredWrite) -> WriteOutcome:
        """The gates, asked again now — a rule may have changed since the card — then one transaction."""
        checked = self._checked_write(config, write)
        if isinstance(checked, WriteOutcome):
            return checked
        try:
            affected = dialect_for(checked.source.dialect).run_write(
                checked.dsn, checked.request, trusted_hosts=self.trusted_hosts
            )
        except DatabaseProbeError as exc:
            return _write_outcome(checked.source, write, refusal=str(exc))
        return WriteOutcome(
            affected=affected,
            source_name=checked.source.name,
            table=write.table,
            description=describe_write(write),
            scope=checked.request.scope,
        )

    def write_grants(self, config: DataWriteConfig) -> dict[str, dict[str, Any]]:
        """Each writable table's columns and this asker's grant per operation — one employee lookup for all of it."""
        source = self._source(config.source_id)
        if source is None:
            return {}
        principal = None
        rules = access_rules_of(source)
        if self.asker and rules is not None and not rules.open:
            try:
                dsn = dsn_of(source, self.secret_store)
                lookup = dialect_for(source.dialect).identity_lookup(dsn, trusted_hosts=self.trusted_hosts)
                principal = resolve_principal(rules, self.asker, lookup=lookup)
            except DatabaseProbeError:
                principal = None
        found: dict[str, dict[str, Any]] = {}
        for table in _writable_tables(source, config):
            grants: dict[str, Any] = {}
            for operation in WriteOp:
                scope = scope_on_write(source, principal, table, operation)
                if isinstance(scope, Deny):
                    grants[operation.value] = {"denied": {"cause": scope.cause.value, "reason": scope.reason}}
                else:
                    pairs = scope.pairs if isinstance(scope, Where) else ()
                    grants[operation.value] = {"scope": [[column, value] for column, value in pairs]}
            found[table] = {"columns": list((source.table_columns or {}).get(table, [])), "grants": grants}
        return found

    def _checked_write(self, config: DataWriteConfig, write: StructuredWrite) -> "WriteOutcome | _ClearedWrite":
        """The module docstring's gates for a write, in order; the first that refuses is the outcome."""
        source = self._source(config.source_id)
        if source is None:
            return WriteOutcome(
                affected=None, source_name="", table=write.table, description=describe_write(write), refusal=_NO_SOURCE
            )
        problems = write_problems(
            write, tables=_writable_tables(source, config), columns_by_table=source.table_columns or {}
        )
        if problems:
            return _write_outcome(source, write, refusal="; ".join(problems))
        closed = closed_write_deny(source, write.table)
        if closed is not None:
            return _write_outcome(source, write, denied=closed)
        rules = access_rules_of(source)
        assert rules is not None  # `closed_write_deny` already denied a source without rules
        if not self.asker and not rules.open:
            return _write_outcome(source, write, denied=Deny(cause=DenyCause.UNKNOWN_ASKER, reason=WRITE_DENIED))
        try:
            dsn = dsn_of(source, self.secret_store)
            principal = None
            if not rules.open:
                lookup = dialect_for(source.dialect).identity_lookup(dsn, trusted_hosts=self.trusted_hosts)
                principal = resolve_principal(rules, self.asker or "", lookup=lookup)
        except DatabaseProbeError as exc:
            return _write_outcome(source, write, refusal=str(exc))
        scope = scope_on_write(source, principal, write.table, write.operation)
        if isinstance(scope, Deny):
            return _write_outcome(source, write, denied=scope)
        pairs = scope.pairs if isinstance(scope, Where) else ()
        if any(column in write.values and str(write.values[column]) != str(value) for column, value in pairs):
            return _write_outcome(source, write, denied=Deny(cause=DenyCause.OUT_OF_SCOPE_VALUE, reason=WRITE_DENIED))
        request = WriteRequest(
            write=write,
            table_columns=tuple((source.table_columns or {}).get(write.table, [])),
            scope=pairs,
            session_attributes=dict(principal.attributes) if principal else {},
            max_rows=config.max_rows,
        )
        try:
            # The asker was looked up through the read DSN; the change itself goes through the write one.
            write_dsn = write_dsn_of(source, self.secret_store)
        except DatabaseProbeError as exc:
            return _write_outcome(source, write, refusal=str(exc))
        return _ClearedWrite(source=source, dsn=write_dsn, request=request)

    def _source(self, source_id: str) -> DatabaseSource | None:
        return owned_source(self.db, source_id, self.owner_id)


@dataclass(frozen=True, slots=True)
class _ClearedWrite:
    """A write every gate let through: the source, its DSN, and the request a dialect runs. Never leaves this module."""

    source: DatabaseSource
    dsn: str
    request: WriteRequest


def get_database_reader(
    agent_id: int,
    db: Annotated[Session, Depends(get_db)],
    secret_store: Annotated[SecretStore, Depends(get_secret_store)],
) -> DatabaseReader:
    """FastAPI dependency: the reader for one run of `agent_id` — `get_knowledge_store`'s shape and reason.

    Scoped to the agent *owner's* Hệ CSDL, never the caller's, and bound to
    nobody: on the platform the asker is the person a creator picks with
    "Chạy với tư cách…" (DS-05, ADR-0059 §3), which a trial route applies with
    `asked_by`. A run nobody picked an asker for is denied every read before a
    query is sent — the default ADR-0059 §6 wants, not a gap in it.
    """
    agent = db.get(Agent, agent_id)
    return PlatformDatabaseReader(
        db=db,
        owner_id=agent.owner_id if agent is not None else _NO_SUCH_OWNER,
        asker=None,
        secret_store=secret_store,
        trusted_hosts=get_settings().database_source_trusted_host_set,
    )


#: An owner id no row can have — what a reader for an agent that does not exist is bound to.
_NO_SUCH_OWNER = -1


def describe(query: StructuredQuery, *, scoped: bool) -> str:
    """The filters a result was read under, in words — what a citation says about an aggregate or a list."""
    parts = [
        f"{item.column} {item.op.value}{'' if item.value is None else f' {_shown(item.value)}'}"
        for item in query.filters
    ]
    described = f"lọc: {'; '.join(parts)}" if parts else "không lọc"
    if query.group_by:
        described += f"; nhóm theo {', '.join(query.group_by)}"
    if scoped:
        described += "; trong phạm vi dữ liệu của người hỏi"
    return described


def _shown(value: object) -> str:
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


def _readable_views(source: DatabaseSource, config: DataQueryConfig) -> list[str]:
    """The node's views that are still ticked on the source — a view unticked since confirm is not readable."""
    return [view for view in config.views if view in source.allowed_views]


def _writable_tables(source: DatabaseSource, config: DataWriteConfig) -> list[str]:
    """The node's tables that are still ticked for writing — a table unticked since confirm is not writable."""
    return [table for table in config.tables if table in (source.allowed_write_tables or [])]


def _write_outcome(
    source: DatabaseSource,
    write: StructuredWrite,
    *,
    refusal: str | None = None,
    denied: Deny | None = None,
    scope: tuple[tuple[str, Any], ...] | None = None,
) -> WriteOutcome:
    return WriteOutcome(
        affected=None,
        source_name=source.name,
        table=write.table,
        description=describe_write(write),
        refusal=refusal,
        denied=denied,
        scope=scope,
    )


def _request(
    source: DatabaseSource, query: StructuredQuery, *, scope: All | Where, principal: Principal | None, max_rows: int
) -> QueryRequest:
    return QueryRequest(
        query=query,
        view_columns=tuple(source.view_columns.get(query.view, [])),
        scope=scope.pairs if isinstance(scope, Where) else (),
        session_attributes=dict(principal.attributes) if principal else {},
        limit=effective_limit(query, max_rows=max_rows),
    )


def _outcome(
    source: DatabaseSource, query: StructuredQuery, *, refusal: str | None = None, denied: Deny | None = None
) -> QueryOutcome:
    return QueryOutcome(
        rows=None,
        source_name=source.name,
        view=query.view,
        description=describe(query, scoped=False),
        refusal=refusal,
        denied=denied,
    )
