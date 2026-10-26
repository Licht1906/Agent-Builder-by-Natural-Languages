"""The structured query a `data_query` node's model fills in — DS-04 / ADR-0059 §5.

**Không text-to-SQL.** The model never writes SQL: it fills in this JSON —
which view, which columns, which filters from a closed list of operators,
optional grouping and aggregates — and `query_problems` checks it against the
views the node may read and the columns the last probe saw. A dialect builds
the statement from the checked query with every value as a parameter, and
ANDs the asker's scope into it; nothing the model wrote reaches SQL as text.

Two layers of refusal, on purpose. `StructuredQuery`'s own validators refuse
what is wrong *with the query alone* — an operator off the whitelist, a value
that does not fit its operator, a field that is not part of the format.
`query_problems` refuses what is wrong *for this node on this database* —
a view it may not read, a column the view does not have.
"""

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The values a filter may compare a column with: one scalar, never a document.
FilterScalar = str | int | float | bool

#: `in`/`not_in` lists longer than this are a query the model wrote by listing
#: rows rather than describing them.
_MAX_IN_VALUES = 100

#: A plain lower-case SQL name: what an aggregate alias may be called, and what
#: an asker attribute must be to become `app.<name>` on a session. Either way
#: it is quoted or bound later; holding it to this shape keeps the transcript
#: and the session readable as the names they are.
PLAIN_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class FilterOp(StrEnum):
    """The spec's operator whitelist, and nothing else."""

    EQ = "="
    NE = "!="
    GT = ">"
    GE = ">="
    LT = "<"
    LE = "<="
    IN = "in"
    NOT_IN = "not_in"
    LIKE = "like"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    BETWEEN = "between"


_LIST_OPS = frozenset({FilterOp.IN, FilterOp.NOT_IN})
_NO_VALUE_OPS = frozenset({FilterOp.IS_NULL, FilterOp.IS_NOT_NULL})


class QueryFilter(BaseModel):
    """`column op value` — the value's shape decided by the operator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str
    op: FilterOp
    value: FilterScalar | list[FilterScalar] | None = None

    @model_validator(mode="after")
    def _value_fits_operator(self) -> "QueryFilter":
        """`in`/`not_in` a non-empty list, `between` two, `is_null` none, everything else one scalar."""
        value = self.value
        if self.op in _NO_VALUE_OPS:
            if value is not None:
                raise ValueError(f"{self.op.value} không nhận giá trị")
        elif self.op in _LIST_OPS:
            if not isinstance(value, list) or not 1 <= len(value) <= _MAX_IN_VALUES:
                raise ValueError(f"{self.op.value} cần một danh sách 1–{_MAX_IN_VALUES} giá trị")
        elif self.op is FilterOp.BETWEEN:
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("between cần đúng hai giá trị [từ, đến]")
        elif value is None or isinstance(value, list):
            raise ValueError(f"{self.op.value} cần đúng một giá trị")
        return self


class AggregateFn(StrEnum):
    """The spec's aggregate functions — one list for the model's schema, the check and the SQL."""

    SUM = "sum"
    COUNT = "count"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class SortDir(StrEnum):
    ASC = "asc"
    DESC = "desc"


class QueryAggregate(BaseModel):
    """`{fn, column, as}` — `count` may take `*`."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    fn: AggregateFn
    column: str
    alias: str = Field(..., alias="as")

    @field_validator("alias")
    @classmethod
    def _plain_name(cls, alias: str) -> str:
        if not PLAIN_NAME.match(alias):
            raise ValueError("tên 'as' chỉ gồm chữ thường, số và gạch dưới")
        return alias


class QueryOrder(BaseModel):
    """One `ORDER BY` term: a view column the query returns, or an aggregate's alias."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str
    dir: SortDir = SortDir.ASC


class StructuredQuery(BaseModel):
    """The spec's query format. Empty `columns` with no aggregates means every column of the view."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    view: str
    columns: list[str] = Field(default_factory=list)
    filters: list[QueryFilter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    aggregates: list[QueryAggregate] = Field(default_factory=list)
    order_by: list[QueryOrder] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1)

    @property
    def aggregating(self) -> bool:
        """Whether rows come back grouped — then only `group_by` columns and aggregate aliases are returned."""
        return bool(self.aggregates or self.group_by)


def query_problems(
    query: StructuredQuery, *, views: Sequence[str], columns_by_view: Mapping[str, Sequence[str]]
) -> list[str]:
    """One sentence per reason `query` may not run on a node that reads `views`, against the last probe's columns."""
    if query.view not in views:
        return [f"view {query.view} không nằm trong các view node này được đọc"]
    if query.view not in columns_by_view:
        return [f"không có view {query.view}"]
    known = set(columns_by_view[query.view])
    problems = [f"view {query.view} không có cột {column}" for column in _referenced(query) if column not in known]
    problems.extend(_aggregate_problems(query))
    returned = set(returned_columns(query, columns_by_view[query.view]))
    problems.extend(
        f"order_by {order.column} không nằm trong các cột query trả về"
        for order in query.order_by
        if order.column in known and order.column not in returned
    )
    return list(dict.fromkeys(problems))


def returned_columns(query: StructuredQuery, view_columns: Sequence[str]) -> list[str]:
    """The names a result row has, in order: grouped or chosen columns, then aggregate aliases."""
    if query.aggregating:
        return [*query.group_by, *(aggregate.alias for aggregate in query.aggregates)]
    return list(query.columns) or list(view_columns)


def effective_limit(query: StructuredQuery, *, max_rows: int) -> int:
    """`query.limit` clamped to the node's ceiling — the ceiling itself when the model asked for none."""
    return min(query.limit or max_rows, max_rows)


def _referenced(query: StructuredQuery) -> list[str]:
    """Every view column `query` names, in the order a reader meets them — aliases and `*` left out."""
    aliases = {aggregate.alias for aggregate in query.aggregates}
    names = [
        *query.columns,
        *(item.column for item in query.filters),
        *query.group_by,
        *(aggregate.column for aggregate in query.aggregates if aggregate.column != "*"),
        *(order.column for order in query.order_by if order.column not in aliases),
    ]
    return list(dict.fromkeys(names))


def _aggregate_problems(query: StructuredQuery) -> list[str]:
    problems = [
        f"chỉ count mới dùng được * — {aggregate.fn.value} cần một cột"
        for aggregate in query.aggregates
        if aggregate.column == "*" and aggregate.fn is not AggregateFn.COUNT
    ]
    if query.aggregating:
        problems.extend(
            f"cột {column} phải nằm trong group_by khi query có aggregate"
            for column in query.columns
            if column not in query.group_by
        )
    return problems
