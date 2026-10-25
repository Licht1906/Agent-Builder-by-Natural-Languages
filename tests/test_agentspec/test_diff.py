"""UC1-16 (#132) — `diff_spec`, spec.md §19, FR-31."""

from src.agentspec import validate_spec
from src.agentspec.diff import diff_spec
from tests.test_agentspec.conftest import spec_payload


def _spec(**overrides):
    return validate_spec(spec_payload(**overrides))


def test_editing_one_field_shows_up_as_exactly_that_path() -> None:
    """AC-16: "sửa một trường trong spec → diff hiển thị đúng đường dẫn trường đó"."""
    before = _spec()
    after = _spec(metadata={**spec_payload()["metadata"], "name": "Tên khác"})

    diffs = diff_spec(before, after)

    assert [d.path for d in diffs] == ["metadata.name"]
    assert diffs[0].before == "Trợ lý công nợ"
    assert diffs[0].after == "Tên khác"


def test_identical_specs_produce_no_diff() -> None:
    before = _spec()
    after = _spec()

    assert diff_spec(before, after) == []


def test_lifecycle_bookkeeping_fields_are_excluded_even_though_they_always_differ() -> None:
    before = validate_spec(spec_payload(metadata={**spec_payload()["metadata"], "version": 1, "status": "approved"}))
    after = validate_spec(
        spec_payload(
            metadata={
                **spec_payload()["metadata"],
                "version": 2,
                "status": "draft",
                "parent_version": 1,
                "approved_by": ["usr_1"],
            }
        )
    )

    diffs = diff_spec(before, after)

    assert diffs == []


def test_a_change_inside_a_list_is_reported_by_index() -> None:
    before = _spec(capabilities=[{"tool": "erp.query_receivables", "scope": "read"}])
    after = _spec(
        capabilities=[
            {"tool": "erp.query_receivables", "scope": "read"},
            {"tool": "erp.query_customer", "scope": "read"},
        ]
    )

    diffs = diff_spec(before, after)

    # A wholly new list item is reported as one diff at its own index, not
    # decomposed field by field — there is no "before" value for any of its
    # fields to compare against.
    added = next(d for d in diffs if d.path == "capabilities[1]")
    assert added.before is None
    assert added.after == {"tool": "erp.query_customer", "scope": "read", "requires_approval": False, "constraints": None}
