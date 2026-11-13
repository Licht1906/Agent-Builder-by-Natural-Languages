"""Quy tắc truy cập — who may see which rows of which view. DS-03 / ADR-0059 §6.

The security core of the Hệ CSDL feature, and deliberately small. One model
(`AccessRules`) is both the platform's configuration of a Hệ CSDL and the
`access.yaml` an exported package ships with; one function (`scope_for`)
turns an asker and a view into the filter a query must carry.

**Mặc định cấm, in every branch.** No asker, no position, no rule for the
position, a view the rule does not list, an attribute the asker has no value
for — each is a `Deny`, written out as its own branch rather than left to
fall through. `All` exists only where a rule says `all` in so many words.

**Pure, but for one function.** `scope_for`, `access_problems` and the YAML
pair touch nothing outside their arguments. `resolve_principal` is the single
read — the asker's row in the employee view — and even it takes that read as
a `lookup` callable, so the engine is tested without a database and the
dialect owns the SQL (`PostgresDialect.identity_lookup`).

**A model never decides scope.** Nothing here is shown to or filled in by an
LLM; `data_query` (DS-04) asks `scope_for` and ANDs the answer into SQL the
code builds.

**Writing is granted separately, and more narrowly** (ADR-0094). A rule's
`tables` say which operations a position may make on which base table, and on
which of its rows — the same `where`-by-attribute shape, or `all_rows` written
out. A view grant never implies a table grant: being allowed to read
receivables says nothing about being allowed to change them.
"""

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from src.datasources.dialects import ColumnRef, IdentityLookup, Introspection
from src.datasources.write import WriteOp

#: The attribute every identity must declare — the key `rules` are looked up by.
POSITION = "position"

#: What an asker is told on every `Deny`, whatever the branch: "không có
#: quyền", never "không có dữ liệu" (the spec's own line), and never which of
#: the branches it was — that is for the audit trail, not for the asker.
DENIED = "Bạn không có quyền xem dữ liệu này"

#: `DENIED`'s twin for a write — said the same way whichever branch refused it.
WRITE_DENIED = "Bạn không có quyền thay đổi dữ liệu này"


# --------------------------------------------------------------------------- #
# The model — `access.yaml`
# --------------------------------------------------------------------------- #


def _non_blank(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("không được để trống")
    return cleaned


class WhereGrant(BaseModel):
    """`{where: {view_column: asker_attribute, …}}` — equality only, every pair ANDed. No free SQL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    where: dict[str, str] = Field(..., min_length=1)


#: One position's grant on one view: the literal `all`, or a `where`.
Grant = Literal["all"] | WhereGrant


class TableGrant(BaseModel):
    """One position's grant on one base table: which operations, on which rows — `where` or `all_rows`, never neither.

    `where` is a view grant's shape — `{table_column: asker_attribute}`,
    equality, ANDed — and bounds every row an update or delete may touch and
    every row an insert may create. `all_rows: true` is the only way to say
    "any row", and like `all` on a view it has to be written out.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operations: list[WriteOp] = Field(..., min_length=1)
    where: dict[str, str] | None = None
    all_rows: bool = False

    @model_serializer(mode="wrap")
    def _as_written(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Only the half that was chosen — `where` or `all_rows: true` — so `access.yaml` reads as it was written."""
        return {key: value for key, value in handler(self).items() if key == "operations" or value}

    @model_validator(mode="after")
    def _rows_are_named(self) -> "TableGrant":
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("operations bị lặp")
        if self.all_rows == bool(self.where):
            raise ValueError("cần đúng một trong hai: where (lọc theo thuộc tính) hoặc all_rows: true")
        return self


class Rule(BaseModel):
    """What one position may see, view by view, and change, table by table. Not listed is not allowed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    position: str
    views: dict[str, Grant] = Field(default_factory=dict)
    tables: dict[str, TableGrant] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _without_empty_grants(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """No `tables: {}` on a rule that writes nothing — a saved read-only rule stays exactly what it was."""
        return {key: value for key, value in handler(self).items() if key == "position" or value}

    @model_validator(mode="after")
    def _grants_something(self) -> "Rule":
        """A rule with neither views nor tables grants nothing — a position listed only to look covered."""
        if not self.views and not self.tables:
            raise ValueError("rule phải cho phép ít nhất một view hoặc một bảng")
        return self

    @field_validator("position")
    @classmethod
    def _normalised_position(cls, position: str) -> str:
        """Kept the way `scope_for` compares it — trimmed, NFC — so a rule and a DB value meet in one form."""
        return _normalised(_non_blank(position))


class Identity(BaseModel):
    """How an authenticated value becomes an asker: which view, which column it matches, which columns to read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    view: str
    match_by: str = Field(..., description="Cột khớp với giá trị đã xác thực, ví dụ email")
    attributes: dict[str, str] = Field(..., description="Tên thuộc tính -> cột trong view nhân viên")

    _strip_view = field_validator("view", "match_by")(_non_blank)

    @model_validator(mode="after")
    def _position_is_declared(self) -> "Identity":
        """`position` is what `rules` are keyed by; an identity without it can grant nothing to anybody."""
        if POSITION not in self.attributes:
            raise ValueError(f"identity.attributes phải có '{POSITION}' — chức vụ là khoá của rules")
        return self


class AccessRules(BaseModel):
    """`identity` + `rules`, exactly the spec's `access.yaml` — or `open: true`, written out on its own.

    `open` is the database that needs no permissions (ADR-0095): everyone who
    uses the agent sees every ticked view and may change every ticked table
    the same way, so there is no employee view to name and no rule to write.
    It is a rule set of its own, saved on purpose — never what *no* rules
    mean, which stays "deny everyone".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: Identity | None = None
    rules: list[Rule] = Field(default_factory=list)
    open: bool = False

    @model_serializer(mode="wrap")
    def _as_written(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """`{open: true}` alone for an open database; `identity` + `rules` otherwise, with no `open: false` noise."""
        if self.open:
            return {"open": True}
        return {key: value for key, value in handler(self).items() if key != "open"}

    @model_validator(mode="after")
    def _open_or_identified(self) -> "AccessRules":
        """Exactly one of the two shapes: open with nothing else, or an identity the rules are read against."""
        if self.open and (self.identity is not None or self.rules):
            raise ValueError("open: true không đi kèm identity hay rules — mọi người dùng đều như nhau")
        if not self.open and self.identity is None:
            raise ValueError(
                "cần identity (view nhân viên, cột khớp, thuộc tính) — hoặc open: true nếu không phân quyền"
            )
        return self

    @model_validator(mode="after")
    def _rules_are_coherent(self) -> "AccessRules":
        """One rule per position, and every `where` names an attribute the identity actually reads.

        Both are refused here rather than resolved at query time: two rules for
        one position would make the grant depend on list order, and a `where`
        on an undeclared attribute is a filter on a value nobody has.
        """
        seen: set[str] = set()
        for rule in self.rules:
            if rule.position in seen:
                raise ValueError(f"chức vụ {rule.position!r} có nhiều hơn một rule")
            seen.add(rule.position)
            filters = [(view, grant.where) for view, grant in rule.views.items() if isinstance(grant, WhereGrant)]
            filters.extend((table, grant.where) for table, grant in rule.tables.items() if grant.where)
            attributes = self.identity.attributes if self.identity is not None else {}
            for target, where in filters:
                unknown = [name for name in where.values() if name not in attributes]
                if unknown:
                    raise ValueError(
                        f"rule của {rule.position!r} trên {target} lọc theo thuộc tính chưa khai trong identity: "
                        f"{', '.join(unknown)}"
                    )
        return self


# --------------------------------------------------------------------------- #
# The asker, and what they may see
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Principal:
    """The person asking, as the employee view describes them — `attributes` keyed by the identity's own names."""

    match_value: str
    attributes: dict[str, Any] = field(default_factory=dict)


class DenyCause(StrEnum):
    """Which deny branch fired — for the audit trail (DS-10) and the sandbox transcript, never for the asker."""

    UNKNOWN_ASKER = "unknown_asker"
    NO_POSITION = "no_position"
    NO_RULE = "no_rule"
    VIEW_NOT_GRANTED = "view_not_granted"
    MISSING_ATTRIBUTE = "missing_attribute"
    NO_RULES = "no_rules"
    RULES_OUT_OF_DATE = "rules_out_of_date"
    VIEW_NOT_ALLOWED = "view_not_allowed"
    TABLE_NOT_GRANTED = "table_not_granted"
    OPERATION_NOT_GRANTED = "operation_not_granted"
    TABLE_NOT_ALLOWED = "table_not_allowed"
    OUT_OF_SCOPE_VALUE = "out_of_scope_value"


@dataclass(frozen=True)
class Deny:
    """Zero rows. `cause` says which branch denied, for the audit trail; `reason` is all the asker is told."""

    cause: DenyCause = DenyCause.UNKNOWN_ASKER
    reason: str = DENIED


@dataclass(frozen=True)
class All:
    """Every row of the view — only ever because a rule wrote `all`."""


@dataclass(frozen=True)
class Where:
    """Rows where every `(view_column, asker_value)` pair is equal."""

    pairs: tuple[tuple[str, Any], ...]


Scope = Deny | All | Where


def scope_for(rules: AccessRules, principal: Principal | None, view: str) -> Scope:
    """What `principal` may see of `view` under `rules` — `Deny` unless a rule says otherwise.

    The engine alone: it trusts `rules` to fit the database. A caller holding
    a Hệ CSDL asks `src.datasources.sources.scope_on`, which also denies when
    the rules have fallen out of step with the ticked views.
    """
    if rules.open:
        return All()
    if principal is None:
        return Deny(cause=DenyCause.UNKNOWN_ASKER)
    position = _filter_value(principal.attributes.get(POSITION))
    if position is None:
        return Deny(cause=DenyCause.NO_POSITION)
    rule = next((rule for rule in rules.rules if rule.position == _normalised(str(position))), None)
    if rule is None:
        return Deny(cause=DenyCause.NO_RULE)
    grant = rule.views.get(view)
    if grant is None:
        return Deny(cause=DenyCause.VIEW_NOT_GRANTED)
    if grant == "all":
        return All()
    pairs: list[tuple[str, Any]] = []
    for column, attribute in grant.where.items():
        value = _filter_value(principal.attributes.get(attribute))
        if value is None:
            return Deny(cause=DenyCause.MISSING_ATTRIBUTE)
        pairs.append((column, value))
    return Where(pairs=tuple(pairs))


def scope_for_write(rules: AccessRules, principal: Principal | None, table: str, operation: WriteOp) -> Scope:
    """What rows of `table` `principal` may change by `operation` under `rules` — `Deny` unless a rule says so.

    `scope_for`'s branches, in its order, with one more: the table may be
    granted but not this operation. Every deny carries `WRITE_DENIED`.
    """
    if rules.open:
        return All()
    if principal is None:
        return Deny(cause=DenyCause.UNKNOWN_ASKER, reason=WRITE_DENIED)
    position = _filter_value(principal.attributes.get(POSITION))
    if position is None:
        return Deny(cause=DenyCause.NO_POSITION, reason=WRITE_DENIED)
    rule = next((rule for rule in rules.rules if rule.position == _normalised(str(position))), None)
    if rule is None:
        return Deny(cause=DenyCause.NO_RULE, reason=WRITE_DENIED)
    grant = rule.tables.get(table)
    if grant is None:
        return Deny(cause=DenyCause.TABLE_NOT_GRANTED, reason=WRITE_DENIED)
    if operation not in grant.operations:
        return Deny(cause=DenyCause.OPERATION_NOT_GRANTED, reason=WRITE_DENIED)
    if grant.all_rows:
        return All()
    pairs: list[tuple[str, Any]] = []
    for column, attribute in (grant.where or {}).items():
        value = _filter_value(principal.attributes.get(attribute))
        if value is None:
            return Deny(cause=DenyCause.MISSING_ATTRIBUTE, reason=WRITE_DENIED)
        pairs.append((column, value))
    return Where(pairs=tuple(pairs))


def asker_of(value: str | None) -> str | None:
    """`value` as an asker: trimmed, and blank is nobody (`None`) — the one rule every door applies (DS-05)."""
    return (value or "").strip() or None


def resolve_principal(rules: AccessRules, match_value: str, *, lookup: IdentityLookup) -> Principal | None:
    """The asker whose `identity.match_by` column equals `match_value`, or `None` — never a guess.

    A blank value is nobody, decided before any lookup: matching an employee
    row on `''` would hand an anonymous caller whoever has an empty email.
    """
    value = (match_value or "").strip()
    if not value or rules.identity is None:
        return None
    identity = rules.identity
    columns = tuple(dict.fromkeys(identity.attributes.values()))
    row = lookup(ColumnRef(view=identity.view, column=identity.match_by), value, columns)
    if row is None:
        return None
    return Principal(
        match_value=value, attributes={name: row.get(column) for name, column in identity.attributes.items()}
    )


#: The value types an equality filter may compare against. Anything else an
#: employee view can hold — an array, a json document, bytes — would turn
#: `=` into something nobody wrote (`IN (...)`, a match on a blob).
_SCALARS = (str, int, float, Decimal, date, datetime, UUID)


def _filter_value(value: Any) -> Any | None:
    """`value` as a filter can use it — trimmed if text — or `None` for missing, blank or not a single scalar.

    `bool` is refused although it is an `int`: `True` as a position or an
    employee code is a column mistake, not a value to match on.
    """
    if isinstance(value, bool) or not isinstance(value, _SCALARS):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value


def _normalised(text: str) -> str:
    """`text` trimmed and in Unicode NFC — so `Nhân viên` typed and `Nhân viên` stored compare equal."""
    return unicodedata.normalize("NFC", text.strip())


# --------------------------------------------------------------------------- #
# Save-time checks against the last probe
# --------------------------------------------------------------------------- #


def access_problems(
    rules: AccessRules,
    *,
    allowed_views: Sequence[str],
    snapshot: Introspection,
    allowed_tables: Sequence[str] | None = None,
) -> list[str]:
    """One sentence per view, table or column `rules` name that the Hệ CSDL does not have or does not allow.

    `allowed_tables` `None` leaves the `tables` grants unchecked — what the
    *read* path asks, so a table unticked for writing closes writing only,
    never reading (ADR-0094). Saving and writing pass the ticked list.

    Checked against the last probe (`snapshot_of`), so saving is not a network
    call; the exported package runs the same check against production at
    start-up (DS-15). Every sentence names the exact view and column, and
    whose rule it came from.
    """
    views = {view.name: view for view in snapshot.views}
    allowed = set(allowed_views)
    if rules.open:
        return []
    assert rules.identity is not None  # the model refuses a closed rule set without one
    problems = _identity_problems(rules.identity, allowed, views)
    for rule in rules.rules:
        for view, grant in rule.views.items():
            whose = f"(quy tắc của chức vụ {rule.position})"
            if view not in allowed:
                problems.append(f"view {view} chưa được tick cho phép {whose}")
                continue
            columns = views[view].columns if view in views else ()
            if view not in views:
                problems.append(f"không có view {view} {whose}")
            elif isinstance(grant, WhereGrant):
                problems.extend(
                    f"view {view} không có cột {column} {whose}" for column in grant.where if column not in columns
                )
        if allowed_tables is not None:
            problems.extend(_table_problems(rule, set(allowed_tables), snapshot))
    return problems


def _table_problems(rule: Rule, allowed: set[str], snapshot: Introspection) -> list[str]:
    """`access_problems` for one rule's table grants: ticked for writing, still there, every `where` column real."""
    problems: list[str] = []
    whose = f"(quy tắc của chức vụ {rule.position})"
    for table, grant in rule.tables.items():
        if table not in allowed:
            problems.append(f"bảng {table} chưa được tick cho phép ghi {whose}")
            continue
        columns = snapshot.table_columns.get(table)
        if columns is None:
            problems.append(f"không có bảng {table} {whose}")
            continue
        problems.extend(
            f"bảng {table} không có cột {column} {whose}" for column in grant.where or {} if column not in columns
        )
    return problems


def _identity_problems(identity: Identity, allowed: set[str], views: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if identity.view not in allowed:
        problems.append(f"view nhân viên {identity.view} chưa được tick cho phép")
    if identity.view not in views:
        return [*problems, f"không có view nhân viên {identity.view}"]
    columns = views[identity.view].columns
    if identity.match_by not in columns:
        problems.append(f"view nhân viên {identity.view} không có cột {identity.match_by} (cột khớp)")
    problems.extend(
        f"view nhân viên {identity.view} không có cột {column} (thuộc tính {name})"
        for name, column in identity.attributes.items()
        if column not in columns
    )
    return problems


# --------------------------------------------------------------------------- #
# access.yaml both ways
# --------------------------------------------------------------------------- #


class _UniqueKeyLoader(yaml.SafeLoader):
    """`SafeLoader`, refusing a mapping that names the same key twice.

    Plain `safe_load` keeps the last of two keys, so two grants for one view
    under one position would quietly become one — a rule nobody wrote.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        duplicated = sorted({str(key) for key in keys if keys.count(key) > 1})
        if duplicated:
            raise yaml.constructor.ConstructorError(
                None, None, f"khoá bị lặp: {', '.join(duplicated)}", node.start_mark
            )
        return super().construct_mapping(node, deep=deep)


def to_yaml(rules: AccessRules) -> str:
    """`rules` as `access.yaml` — key order as the model declares it, Vietnamese left readable, stable across runs."""
    return yaml.safe_dump(rules.model_dump(mode="json"), sort_keys=False, allow_unicode=True, default_flow_style=False)


def from_yaml(text: str) -> AccessRules:
    """`access.yaml` read back into the model. Raises `ValueError` (a `ValidationError` for a bad shape)."""
    try:
        document = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 — a SafeLoader subclass
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or exc.__class__.__name__
        raise ValueError(f"YAML không đọc được: {problem}") from None
    if not isinstance(document, dict):
        raise ValueError("access.yaml phải là một mapping có identity và rules")
    return AccessRules.model_validate(document)
