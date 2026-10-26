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
"""

from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Depends
from sqlalchemy.orm import Session

from src.config import get_settings
from src.datasources.access import All, Deny, DenyCause, Principal, Where, resolve_principal
from src.datasources.dialects import DatabaseProbeError, QueryRequest, QueryRows, dialect_for
from src.datasources.graph import owned_source
from src.datasources.query import StructuredQuery, effective_limit, query_problems
from src.datasources.sources import access_rules_of, closed_deny, dsn_of, scope_on
from src.db.base import get_db
from src.db.models import Agent, DatabaseSource
from src.models.node_registry import DataQueryConfig
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


class DatabaseReader(Protocol):
    """The runtime's door into the Hệ CSDL a `data_query` node names."""

    def columns_of(self, config: DataQueryConfig) -> dict[str, list[str]]:
        """`{view: columns}` the node may query — what the model's query schema offers. Empty for no usable source."""
        ...

    def read(self, config: DataQueryConfig, query: StructuredQuery) -> QueryOutcome:
        """Run `query` for whoever this reader was built for. Never raises a database failure — it refuses."""
        ...


@dataclass(frozen=True, slots=True)
class PlatformDatabaseReader:
    """`DatabaseReader` over `database_sources`, for one owner and one asker (`None` until DS-05 picks one)."""

    db: Session
    owner_id: int
    asker: str | None
    secret_store: SecretStore
    trusted_hosts: frozenset[str]

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
        """Gates 3 and 4: who is asking, what they may see — then the query itself."""
        if not self.asker:
            return _outcome(source, query, denied=Deny(cause=DenyCause.UNKNOWN_ASKER))
        rules = access_rules_of(source)
        assert rules is not None  # `closed_deny` already denied a source without rules
        dsn = dsn_of(source, self.secret_store)
        dialect = dialect_for(source.dialect)
        lookup = dialect.identity_lookup(dsn, trusted_hosts=self.trusted_hosts)
        principal = resolve_principal(rules, self.asker, lookup=lookup)
        scope = scope_on(source, principal, query.view)
        if isinstance(scope, Deny):
            return _outcome(source, query, denied=scope)
        request = _request(source, query, scope=scope, principal=principal, max_rows=config.max_rows)
        rows = dialect.run_query(dsn, request, trusted_hosts=self.trusted_hosts)
        return QueryOutcome(
            rows=rows, source_name=source.name, view=query.view, description=describe(query, scoped=bool(request.scope))
        )

    def _source(self, source_id: str) -> DatabaseSource | None:
        return owned_source(self.db, source_id, self.owner_id)


def get_database_reader(
    agent_id: int,
    db: Annotated[Session, Depends(get_db)],
    secret_store: Annotated[SecretStore, Depends(get_secret_store)],
) -> DatabaseReader:
    """FastAPI dependency: the reader for one run of `agent_id` — `get_knowledge_store`'s shape and reason.

    Scoped to the agent *owner's* Hệ CSDL, never the caller's. Nobody is
    asking yet: on the platform the asker is the person a creator picks with
    "Chạy với tư cách…" (DS-05, ADR-0059 §3), so until that arrives every
    `data_query` read is denied before a query is sent — which is the
    default ADR-0059 §6 wants, not a gap in it.
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
