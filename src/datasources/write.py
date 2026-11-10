"""The structured write a `data_write` node's model fills in — ADR-0094.

`query.py`'s twin for the other direction, and held to the same rule: **the
model never writes SQL.** It fills in this JSON — which table, which of three
operations, which values, which rows by equality — and `write_problems` checks
it against the tables the node may change and the columns the last probe saw.
A dialect builds a parameterised `INSERT`/`UPDATE`/`DELETE` from the checked
write and ANDs the asker's scope into it; nothing the model wrote reaches SQL
as text.

Narrower than a query on purpose. Rows are picked by **equality only** — a
write that says "every invoice over 30 days" in a range is a bulk change a
person cannot check on an approval card, and the node's row ceiling would
refuse it anyway. An `UPDATE` or `DELETE` with no `where` is refused here,
before anyone is asked to approve it: "change every row" is never what a
conversation meant.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.datasources.query import FilterScalar

#: What a column may be set to: one scalar, or `None` for SQL `NULL`.
WriteValue = FilterScalar | None


class WriteOp(StrEnum):
    """The three changes a write can make — one list for the model's schema, the rules and the SQL."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


class WriteMatch(BaseModel):
    """`column = value` — how an `UPDATE` or `DELETE` names the rows it changes. Equality only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str
    value: FilterScalar


class StructuredWrite(BaseModel):
    """One change to one table: `insert` takes `values`; `update` takes `values` and `where`; `delete` takes `where`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str
    operation: WriteOp
    values: dict[str, WriteValue] = Field(default_factory=dict)
    where: list[WriteMatch] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fits_operation(self) -> "StructuredWrite":
        """Each operation with exactly the parts it uses — a `where` on an insert is a write that means two things."""
        if self.operation is WriteOp.INSERT:
            if not self.values:
                raise ValueError("insert cần ít nhất một giá trị trong values")
            if self.where:
                raise ValueError("insert không nhận where")
        elif self.operation is WriteOp.UPDATE:
            if not self.values:
                raise ValueError("update cần ít nhất một giá trị trong values")
            if not self.where:
                raise ValueError("update cần where — không sửa toàn bộ bảng")
        else:
            if self.values:
                raise ValueError("delete không nhận values")
            if not self.where:
                raise ValueError("delete cần where — không xoá toàn bộ bảng")
        return self


def write_problems(
    write: StructuredWrite, *, tables: Sequence[str], columns_by_table: Mapping[str, Sequence[str]]
) -> list[str]:
    """One sentence per reason `write` may not run on a node that changes `tables`, against the last probe's columns."""
    if write.table not in tables:
        return [f"bảng {write.table} không nằm trong các bảng node này được ghi"]
    if write.table not in columns_by_table:
        return [f"không có bảng {write.table}"]
    known = set(columns_by_table[write.table])
    named = [*write.values, *(match.column for match in write.where)]
    return [f"bảng {write.table} không có cột {column}" for column in dict.fromkeys(named) if column not in known]


def describe_write(write: StructuredWrite) -> str:
    """The write in words — what the transcript and the asker are told was done (or would have been)."""
    target = f"bảng {write.table}"
    rows = "; ".join(f"{match.column} = {match.value}" for match in write.where)
    values = ", ".join(f"{column} = {'NULL' if value is None else value}" for column, value in write.values.items())
    if write.operation is WriteOp.INSERT:
        return f"thêm một dòng vào {target} ({values})"
    if write.operation is WriteOp.UPDATE:
        return f"sửa {target} tại {rows}: {values}"
    return f"xoá khỏi {target} các dòng {rows}"
