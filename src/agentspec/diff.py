"""UC1-16 (#132), spec.md §19, FR-31 — a field-level diff between two `AgentSpec`
versions, for the approval screen's "diff so với version trước".

A generic recursive walk over each spec's own `model_dump`, not a diff
specific to any one field — the same reason `Problem.path` (`src.agentspec.validation`)
is a dotted/bracketed string rather than a `code` enum: a reviewer's screen
renders whatever path changed, and a hand-written list of "fields that might
change" would drift the moment a new field is added to `AgentSpec` without a
matching line added here.
"""

from dataclasses import dataclass
from typing import Any

from src.agentspec.spec import AgentSpec

#: Bookkeeping the platform writes on every transition, never a person's edit
#: (`src.agentspec.lifecycle`) — always differs between any two versions and
#: would otherwise show up as noise on every diff, defeating AC-16's own
#: "sửa một trường → diff hiển thị đúng đường dẫn trường đó" (exactly that
#: field, not that field plus four the reviewer never touched).
_EXCLUDED_PATHS = frozenset({"metadata.version", "metadata.status", "metadata.approved_by", "metadata.parent_version"})


@dataclass(frozen=True, slots=True)
class FieldDiff:
    """One path whose value differs between two versions."""

    path: str
    before: Any
    after: Any


def diff_spec(before: AgentSpec, after: AgentSpec) -> list[FieldDiff]:
    """Every field that differs between `before` and `after`, path-sorted.

    Both sides go through the same `model_dump(mode="json", by_alias=True)`
    `src.agentspec.store` already persists — the diff is over the wire shape a
    reviewer's screen reads, not over Python objects a UI has no use for.
    """
    diffs: list[FieldDiff] = []
    _walk(before.model_dump(mode="json", by_alias=True), after.model_dump(mode="json", by_alias=True), "", diffs)
    return sorted(diffs, key=lambda diff: diff.path)


def _walk(before: Any, after: Any, path: str, diffs: list[FieldDiff]) -> None:
    if path in _EXCLUDED_PATHS:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            _walk(before.get(key), after.get(key), f"{path}.{key}" if path else key, diffs)
        return
    if isinstance(before, list) and isinstance(after, list):
        # Positional, not a semantic list diff (an inserted item shifts every
        # index after it) — enough for a reviewer to see *that* `connections[1]`
        # changed, which is what AC-16 asks for; a smarter alignment is not.
        for index in range(max(len(before), len(after))):
            _walk(
                before[index] if index < len(before) else None,
                after[index] if index < len(after) else None,
                f"{path}[{index}]",
                diffs,
            )
        return
    if before != after and path not in _EXCLUDED_PATHS:
        diffs.append(FieldDiff(path=path, before=before, after=after))
