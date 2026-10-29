"""A `StructuredQuery` run over rows already in memory — what an eval cassette replays through (EV-02).

The dialects (`src.datasources.dialects`) compile a structured query to SQL and
send it to the creator's database. An eval run must not (ADR-0069 §2): it reads
a frozen snapshot of each view as the case's asker saw it, and this module is
what answers the model's query from that snapshot. A query is a pure function of
the rows it reads, so the same query on the same snapshot gives the same answer
on every run — which is the whole reason a Baseline and a Candidate can be
compared.

The semantics are Postgres's, the dialect the demo database speaks, as far as a
structured query can reach them:

- a comparison with `NULL` is never true (`=`, `!=`, `in`, `not_in`, `between`
  included);
- `like` compares the value as text, case-sensitively, `%` and `_` the only
  wildcards — the dialects cast to text for the same reason;
- `count(*)` counts rows, `count(col)` the non-null values, `sum`/`avg`/`min`/
  `max` ignore nulls and are `NULL` over no values; aggregates with no
  `group_by` are one row even over no rows;
- `ORDER BY … ASC` puts nulls last, `DESC` first;
- the limit is applied after the sort.

Values are what the snapshot stored: JSON scalars, dates as ISO strings (which
sort and compare as dates). A number compared with a string that reads as a
number is compared as a number.
"""

import re
import unicodedata
from collections.abc import Callable, Sequence
from typing import Any

from src.datasources.dialects.base import QueryRows
from src.datasources.query import (
    AggregateFn,
    FilterOp,
    QueryAggregate,
    QueryFilter,
    SortDir,
    StructuredQuery,
    returned_columns,
)

Row = dict[str, Any]


def run_in_memory(
    query: StructuredQuery, *, columns: Sequence[str], rows: Sequence[Sequence[Any]], limit: int
) -> QueryRows:
    """`query` over `rows` (each in `columns`' order), at most `limit` rows back — `QueryRows` like a dialect's."""
    records: list[Row] = [dict(zip(columns, row, strict=False)) for row in rows]
    kept = [record for record in records if all(_holds(record.get(item.column), item) for item in query.filters)]
    shown = returned_columns(query, columns)
    out = (
        _aggregated(query, kept)
        if query.aggregating
        else [{name: record.get(name) for name in shown} for record in kept]
    )
    for order in reversed(query.order_by):
        out = _sorted(out, order.column, descending=order.dir is SortDir.DESC)
    return QueryRows(
        columns=tuple(shown), rows=tuple(tuple(record.get(name) for name in shown) for record in out[:limit])
    )


def _holds(value: Any, item: QueryFilter) -> bool:
    """One filter on one value, `NULL` never satisfying a comparison."""
    op, wanted = item.op, item.value
    if op is FilterOp.IS_NULL:
        return value is None
    if op is FilterOp.IS_NOT_NULL:
        return value is not None
    if value is None:
        return False
    if op is FilterOp.LIKE:
        return _like(str(wanted)).fullmatch(_as_text(value)) is not None
    if op in (FilterOp.IN, FilterOp.NOT_IN):
        assert isinstance(wanted, list)
        found = any(_compare(value, candidate) == 0 for candidate in wanted if candidate is not None)
        return found if op is FilterOp.IN else not found
    if op is FilterOp.BETWEEN:
        assert isinstance(wanted, list)
        low, high = wanted
        return low is not None and high is not None and _compare(value, low) >= 0 and _compare(value, high) <= 0
    order = _compare(value, wanted)
    return {
        FilterOp.EQ: order == 0,
        FilterOp.NE: order != 0,
        FilterOp.GT: order > 0,
        FilterOp.GE: order >= 0,
        FilterOp.LT: order < 0,
        FilterOp.LE: order <= 0,
    }[op]


def _compare(left: Any, right: Any) -> int:
    """-1/0/1 the way the database would order two non-null values.

    Two numbers as numbers; a number and text that reads as one as numbers (a
    filter value the model wrote as `"30"`); two texts by `_collation_key` —
    equal only when identical, as `=` on text is.
    """
    if isinstance(left, str) and isinstance(right, str):
        if left == right:
            return 0
        return -1 if _collation_key(left) < _collation_key(right) else 1
    a, b = _comparable(left), _comparable(right)
    if type(a) is not type(b):
        a, b = _collation_key(str(left)), _collation_key(str(right))
    return (a > b) - (a < b)


def _comparable(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return str(value)


def _collation_key(text: str) -> tuple[str, str, str, str]:
    """An approximation of glibc's `en_US.utf8` order — the collation the demo's Postgres sorts text in.

    Four levels, each consulted only on a tie in the one before: the letters
    with accents, case, spaces and punctuation set aside ("Hải Phòng" before
    "Hà Nội", "Đà Nẵng" among the Ds); then accents, unmarked first; then case,
    lower first; then the text as written. Not ICU, and not every locale — a
    creator's database in another collation can order ties differently, which
    the cassette notes nowhere because the demo's cannot.
    """
    bare = "".join(char for char in unicodedata.normalize("NFD", text) if not unicodedata.combining(char))
    letters = "".join(char for char in bare.replace("đ", "d").replace("Đ", "D").casefold() if char.isalnum())
    marked = "".join(char for char in text.casefold() if char.isalnum())
    cased = "".join(char for char in text if char.isalnum()).swapcase()
    return letters, marked, cased, text


def _as_text(value: Any) -> str:
    """A value as `CAST(x AS text)` writes it — an integral float without its `.0`."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _like(pattern: str) -> re.Pattern[str]:
    """SQL `LIKE` as a regex: `%` any run, `_` one character, everything else literal."""
    parts = ("." if char == "_" else ".*" if char == "%" else re.escape(char) for char in pattern)
    return re.compile("".join(parts), re.DOTALL)


def _aggregated(query: StructuredQuery, records: list[Row]) -> list[Row]:
    """`GROUP BY` and the aggregates, groups in the order they were first met."""
    groups: dict[tuple[Any, ...], list[Row]] = {}
    for record in records:
        groups.setdefault(tuple(record.get(name) for name in query.group_by), []).append(record)
    if not query.group_by and not groups:
        groups[()] = []
    return [
        {
            **dict(zip(query.group_by, key, strict=True)),
            **{aggregate.alias: _aggregate(aggregate, members) for aggregate in query.aggregates},
        }
        for key, members in groups.items()
    ]


def _aggregate(aggregate: QueryAggregate, members: list[Row]) -> Any:
    if aggregate.fn is AggregateFn.COUNT:
        if aggregate.column == "*":
            return len(members)
        return sum(1 for member in members if member.get(aggregate.column) is not None)
    values = [member[aggregate.column] for member in members if member.get(aggregate.column) is not None]
    if not values:
        return None
    reducers: dict[AggregateFn, Callable[[list[Any]], Any]] = {
        AggregateFn.SUM: lambda found: sum(_comparable(value) for value in found),
        AggregateFn.AVG: lambda found: sum(_comparable(value) for value in found) / len(found),
        AggregateFn.MIN: lambda found: min(found, key=_comparable),
        AggregateFn.MAX: lambda found: max(found, key=_comparable),
    }
    result = reducers[aggregate.fn](values)
    return (
        int(result) if aggregate.fn is AggregateFn.SUM and isinstance(result, float) and result.is_integer() else result
    )


def _sorted(records: list[Row], column: str, *, descending: bool) -> list[Row]:
    """A stable sort on one key, nulls last ascending and first descending — Postgres's defaults."""
    present = [record for record in records if record.get(column) is not None]
    absent = [record for record in records if record.get(column) is None]
    present.sort(key=lambda record: _SortKey(record[column]), reverse=descending)
    return absent + present if descending else present + absent


class _SortKey:
    """`_compare` as a sort key, so mixed numbers-as-text sort the way filters compare them."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __lt__(self, other: "_SortKey") -> bool:
        return _compare(self.value, other.value) < 0
