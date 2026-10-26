"""A checked structured query as a SQLAlchemy Core statement — the engine-free half of compiling one.

Every dialect compiles the same `Select` with its own SQLAlchemy dialect
(`postgresql`, `mssql`, later `mysql`), so the rules live here once:

- **Values are bind parameters, always.** Nothing a model wrote becomes SQL
  text; a column or view name only ever arrives as an identifier SQLAlchemy
  quotes, and only after `query_problems` matched it against the last probe.
- **The asker's scope is ANDed into `WHERE`**, beside the query's own filters
  and so before any `GROUP BY` — an aggregate over rows the asker may not see
  is a leak with a sum on it.
- **A bare view name is the engine's default schema** (`public`, `dbo`), as
  the probe listed it — never whatever the session's search path says.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy import Unicode, and_, cast, column, func, select, table
from sqlalchemy.sql import ColumnElement, Select, TableClause

from src.datasources.dialects.base import QueryRequest
from src.datasources.query import AggregateFn, FilterOp, QueryFilter, SortDir, returned_columns

_AGGREGATES: dict[AggregateFn, Callable[..., ColumnElement[Any]]] = {
    AggregateFn.SUM: func.sum,
    AggregateFn.AVG: func.avg,
    AggregateFn.MIN: func.min,
    AggregateFn.MAX: func.max,
    AggregateFn.COUNT: func.count,
}


def source_table(view: str, columns: tuple[str, ...], *, default_schema: str) -> TableClause:
    """`view` as a creator names it (`v_x`, `sales.v_x`) with `columns`, a bare name placed in `default_schema`."""
    schema, _, name = view.rpartition(".")
    return table(name, *(column(column_name) for column_name in columns), schema=schema or default_schema)


def select_for(request: QueryRequest, *, default_schema: str) -> Select[Any]:
    """The statement `request` describes — filters and scope in `WHERE`, then grouping, order and the ceiling."""
    query = request.query
    source = source_table(query.view, request.view_columns, default_schema=default_schema)
    columns = source.c
    selected: list[ColumnElement[Any]] = []
    if query.aggregating:
        selected.extend(columns[column_name] for column_name in query.group_by)
        for aggregate in query.aggregates:
            function = _AGGREGATES[aggregate.fn]
            argument = () if aggregate.column == "*" else (columns[aggregate.column],)
            selected.append(function(*argument).label(aggregate.alias))
    else:
        selected.extend(columns[column_name] for column_name in returned_columns(query, request.view_columns))
    conditions = [_condition(columns[item.column], item) for item in query.filters]
    conditions.extend(columns[column_name] == value for column_name, value in request.scope)
    statement = select(*selected).select_from(source)
    if conditions:
        statement = statement.where(and_(*conditions))
    if query.group_by:
        statement = statement.group_by(*(columns[column_name] for column_name in query.group_by))
    labels = {element.name: element for element in selected if hasattr(element, "name")}
    for order in query.order_by:
        # An aggregate alias or a view column — `query_problems` already refused anything else.
        target = labels[order.column] if order.column in labels else columns[order.column]
        statement = statement.order_by(target.desc() if order.dir is SortDir.DESC else target.asc())
    return statement.limit(request.limit)


def _condition(target: ColumnElement[Any], item: QueryFilter) -> ColumnElement[bool]:
    """One whitelisted operator as a Core expression — the value bound, never interpolated."""
    value = item.value
    match item.op:
        case FilterOp.EQ:
            return target == value
        case FilterOp.NE:
            return target != value
        case FilterOp.GT:
            return target > value
        case FilterOp.GE:
            return target >= value
        case FilterOp.LT:
            return target < value
        case FilterOp.LE:
            return target <= value
        case FilterOp.IN:
            return target.in_(value)  # type: ignore[arg-type]
        case FilterOp.NOT_IN:
            return target.not_in(value)  # type: ignore[arg-type]
        case FilterOp.LIKE:
            # Compared as text, so `like` on a number or a date column means
            # what a person typing `%30%` meant rather than a type error.
            # `Unicode`, not `String`: SQL Server's bare `VARCHAR` is 30
            # characters of a code page, which drops both the tail and "ư".
            return cast(target, Unicode).like(str(value))
        case FilterOp.IS_NULL:
            return target.is_(None)
        case FilterOp.IS_NOT_NULL:
            return target.is_not(None)
        case FilterOp.BETWEEN:
            assert isinstance(value, list)  # `QueryFilter` holds `between` to a two-item list
            low, high = value
            return target.between(low, high)
