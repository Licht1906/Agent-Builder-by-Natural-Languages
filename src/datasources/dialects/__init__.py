"""The dialect layer: one module per database engine, one interface they all keep — ADR-0059 §8.

`base` holds the dialect-free half (what a probe finds, how a failure is
named, and the schema check over a probe's findings); each sibling module is
one engine. `dialect_for` is the only way callers pick one, so adding SQL
Server (DS-06) or MySQL (DS-07) is a module plus a row in `_DIALECTS`.
"""

from src.datasources.dialects.base import (
    ColumnRef,
    DatabaseProbeError,
    Dialect,
    IdentityLookup,
    Introspection,
    ProbeFailure,
    QueryRequest,
    QueryRows,
    ViewInfo,
    schema_problems,
)
from src.datasources.dialects.postgresql import PostgresDialect
from src.db.models import DatabaseDialect

__all__ = [
    "ColumnRef",
    "DatabaseProbeError",
    "Dialect",
    "IdentityLookup",
    "Introspection",
    "ProbeFailure",
    "QueryRequest",
    "QueryRows",
    "ViewInfo",
    "dialect_for",
    "schema_problems",
]

_DIALECTS: dict[DatabaseDialect, Dialect] = {DatabaseDialect.POSTGRESQL: PostgresDialect()}


def dialect_for(dialect: DatabaseDialect) -> Dialect:
    """The implementation behind `dialect` — every `DatabaseDialect` member has one."""
    return _DIALECTS[dialect]
