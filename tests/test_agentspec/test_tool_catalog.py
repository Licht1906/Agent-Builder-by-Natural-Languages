"""UC1-07 (#121) — every `capabilities[].tool` must name a real, registered tool.

Seam: `validate_spec` — the same gate UC1-03 built, so a spec inventing a tool is
refused wherever a spec is refused for any other reason, not by a second check a
caller could forget to call (AC: rejected at the API layer, not only the UI).
"""

import pytest

from src.agentspec import SpecValidationError, validate_spec
from src.agentspec.tool_catalog import catalog_entries, is_known_tool, known_tool_names
from tests.test_agentspec.conftest import spec_payload


def test_the_reference_uc1_spec_uses_only_catalogued_tools(reference_spec: dict) -> None:
    validate_spec(reference_spec)


def test_a_spec_naming_an_uncatalogued_tool_is_refused() -> None:
    payload = spec_payload(capabilities=[{"tool": "erp.query_all", "scope": "read"}])
    with pytest.raises(SpecValidationError) as caught:
        validate_spec(payload)
    assert [(problem.code, problem.path) for problem in caught.value.problems] == [
        ("unknown_tool", "capabilities[0].tool")
    ]
    assert "erp.query_all" in caught.value.problems[0].message


def test_every_uncatalogued_tool_is_named_not_just_the_first() -> None:
    payload = spec_payload(
        capabilities=[
            {"tool": "erp.query_receivables", "scope": "read"},
            {"tool": "erp.query_all", "scope": "read"},
            {"tool": "finance.wire_report", "scope": "read"},
        ]
    )
    with pytest.raises(SpecValidationError) as caught:
        validate_spec(payload)
    assert [problem.path for problem in caught.value.problems] == [
        "capabilities[1].tool",
        "capabilities[2].tool",
    ]


def test_catalog_is_a_closed_fixed_list_in_code() -> None:
    names = known_tool_names()
    assert names == {
        "erp.query_receivables",
        "erp.query_customer",
        "kb.search",
        "mail.send_reminder",
        "task.create",
    }
    assert is_known_tool("erp.query_receivables")
    assert not is_known_tool("erp.query_all")
    assert {entry.name for entry in catalog_entries()} == names
