"""UC1-09 (#123) — least privilege enforced at the database, not by a model's promise.

`connections[].allowed_objects` may name only Postgres views, never a base
table (FR-24, §7's "Đây là nơi least privilege được áp thật sự chứ không phải
bằng lời hứa của model"). Checked against the same query by two different
callers that must never drift apart: `POST /connections/{id}/probe` (UC1-08 /
#122, FR-23) when a creator first wires up a connection, and the compiler
(UC1-10 / #124) at compile time, so a spec cannot reach `deployed` carrying a
base table that slipped past an earlier, now-stale probe.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: `(schema) -> every view name Postgres reports in that schema`. The seam a
#: test scripts with a fixed set; production fills it with a real
#: `information_schema.views` query — see `src.services.postgres_introspection`.
#: Same shape as `src.services.connection_probe.HttpGet`: one callable a caller
#: swaps out, not a class hierarchy.
ViewLister = Callable[[str], Sequence[str]]


@dataclass(frozen=True, slots=True)
class NonViewObject:
    """One name `allowed_objects` listed that `information_schema.views` does not know as a view."""

    name: str


class DisallowedBaseTableError(ValueError):
    """`allowed_objects` names at least one object that is not a real view.

    Carries every offending name, not just the first — a spec listing three
    base tables should be told about all three in one round trip, the same
    "collect every problem" shape `SpecValidationError` and
    `InvalidTransitionError` already use.
    """

    def __init__(self, objects: Sequence[str]) -> None:
        self.objects: list[str] = list(objects)
        super().__init__(
            f"allowed_objects chứa object không phải view: {', '.join(objects)} — "
            "compiler chỉ chấp nhận view, không chấp nhận bảng gốc (FR-24)"
        )


def assert_allowed_objects_are_views(
    allowed_objects: Sequence[str], *, list_views: ViewLister, schema: str = "public"
) -> None:
    """Raise `DisallowedBaseTableError` unless every name in `allowed_objects` is a real view.

    An empty `allowed_objects` passes trivially: a connection that declares
    nothing has nothing to check here, and refusing it would be a second,
    misplaced enforcement of "declare at least one object" — a rule this
    function does not own.
    """
    if not allowed_objects:
        return
    views = set(list_views(schema))
    offending = [name for name in allowed_objects if name not in views]
    if offending:
        raise DisallowedBaseTableError(offending)
