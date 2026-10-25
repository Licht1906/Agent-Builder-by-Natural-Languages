"""UC1-09 (#123) — `allowed_objects` accepts only real views."""

import pytest

from src.agentspec.views import DisallowedBaseTableError, assert_allowed_objects_are_views


def _views(*names: str):
    def _list(schema: str) -> list[str]:
        assert schema == "public"
        return list(names)

    return _list


def test_view_in_allowed_objects_passes() -> None:
    assert_allowed_objects_are_views(["v_ar_open"], list_views=_views("v_ar_open", "v_customer_safe"))


def test_base_table_in_allowed_objects_is_refused() -> None:
    with pytest.raises(DisallowedBaseTableError) as excinfo:
        assert_allowed_objects_are_views(["orders"], list_views=_views("v_ar_open"))
    assert excinfo.value.objects == ["orders"]


def test_every_offending_name_is_reported_not_just_the_first() -> None:
    with pytest.raises(DisallowedBaseTableError) as excinfo:
        assert_allowed_objects_are_views(
            ["orders", "v_ar_open", "fin_invoice"], list_views=_views("v_ar_open")
        )
    assert excinfo.value.objects == ["orders", "fin_invoice"]


def test_empty_allowed_objects_passes_trivially() -> None:
    assert_allowed_objects_are_views([], list_views=_views())
