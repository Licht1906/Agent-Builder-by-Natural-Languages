"""`TOOL_SCHEMAS` — what the model is told each tool accepts, held against what
the implementations actually read.

The bug these guard is not a crash. `_query_receivables` reads
`args.get("days_overdue_gt")` and gets `None` when the model sent `days` or
`status` instead, which is indistinguishable from "no filter asked for" — so a
question about receivables *over 30 days* was answered with every open
receivable, confidently and with a correct-looking citation. Nothing raised, and
the only place it showed up was the golden set's accuracy.
"""

import inspect
import re
from typing import Any

import pytest

from src.compiler.graph import _tool_spec
from src.compiler.tools import TOOL_SCHEMAS, _positive_int, default_tool_implementations

#: Every `args.get("...")` the implementations perform, per tool — the other side
#: of the contract `TOOL_SCHEMAS` advertises.
_ARGS_READ: dict[str, set[str]] = {
    "erp.query_receivables": {"days_overdue_gt"},
    "erp.query_customer": {"customer_id"},
    "mail.send_reminder": {"customer_id", "amount_due"},
    "task.create": {"assignee_id"},
}

#: `kb.search` is a declared stub — its implementation returns a fixed empty
#: passage list and reads no arguments at all ("knowledge retrieval wiring is a
#: separate ticket"). Its schema therefore advertises an argument nothing reads
#: yet, which is a promise about the future rather than drift, and is named here
#: so it cannot quietly become drift: the moment the stub is replaced, the tool
#: moves into `_ARGS_READ` and the equality check applies to it like the rest.
_STUB_TOOLS = frozenset({"kb.search"})


def _implementations() -> dict[str, Any]:
    return default_tool_implementations(resolve_credential=lambda _ref: "postgresql://ro:pw@erp/db")


def test_every_catalogue_tool_has_a_schema() -> None:
    assert set(TOOL_SCHEMAS) == set(_implementations())


def _arguments_read_by(tool: str) -> set[str]:
    """The argument names `tool`'s own implementation actually looks up.

    Read off that closure's source rather than off `default_tool_implementations`
    as a whole, so each tool is measured against its own body — slicing the
    shared source by tool name silently attributes one closure's reads to
    another, which is precisely the kind of near-miss this file exists to catch.
    """
    source = inspect.getsource(_implementations()[tool])
    return set(re.findall(r'args\.get\("([^"]+)"\)', source))


def test_the_stub_list_still_names_only_real_stubs() -> None:
    """Keeps `_STUB_TOOLS` from becoming a place to park a genuinely drifted tool:
    a stub is a tool whose implementation reads no arguments at all.
    """
    for tool in _STUB_TOOLS:
        assert _arguments_read_by(tool) == set(), f"{tool} reads arguments now — move it into _ARGS_READ"


@pytest.mark.parametrize("tool", sorted(_ARGS_READ))
def test_every_tool_advertises_exactly_the_arguments_it_reads(tool: str) -> None:
    """The drift check, and the reason this file exists.

    Both halves are read from the code itself — the advertisement from
    `TOOL_SCHEMAS`, the reads from the implementation's own source — so neither
    can be updated alone. `_ARGS_READ` is the third copy on purpose: it is what
    makes an accidental change to *either* side show up as a disagreement rather
    than as two things that still happen to match each other.
    """
    advertised = set(TOOL_SCHEMAS[tool]["parameters"]["properties"])

    assert advertised == _ARGS_READ[tool], f"{tool}: advertised {advertised}, expected {_ARGS_READ[tool]}"
    assert _arguments_read_by(tool) == _ARGS_READ[tool], (
        f"{tool}: implementation reads {_arguments_read_by(tool)}, expected {_ARGS_READ[tool]}"
    )


@pytest.mark.parametrize("tool", sorted(TOOL_SCHEMAS))
def test_no_schema_advertises_a_numeric_type(tool: str) -> None:
    """`AGENT_MODEL`'s only tool-capable provider truncates numeric argument values
    on the way out (`{"days_overdue_gt": 30}` arrives as `{"days_overdue_gt": 3`),
    so every argument goes on the wire as a string and is coerced on arrival.
    See `TOOL_SCHEMAS`' own note; revisit when a second provider exists.
    """
    types = {field["type"] for field in TOOL_SCHEMAS[tool]["parameters"]["properties"].values()}

    assert types <= {"string"}, f"{tool} advertises non-string types {types - {'string'}}"


@pytest.mark.parametrize("tool", sorted(TOOL_SCHEMAS))
def test_every_advertised_argument_is_described(tool: str) -> None:
    """A name alone does not tell a 7B model when to use the argument — the empty
    `properties: {}` it replaced was, technically, also a valid schema.
    """
    schema = TOOL_SCHEMAS[tool]
    assert len(schema["description"]) > 40, f"{tool} description is too thin to pick the tool from"
    for name, field in schema["parameters"]["properties"].items():
        assert len(field.get("description", "")) > 20, f"{tool}.{name} has no usable description"


def test_the_numeric_workaround_round_trips() -> None:
    """The string the schema asks for has to survive `_positive_int`, or the
    workaround trades a truncated filter for a dropped one.
    """
    assert _positive_int("30") == 30
    assert _positive_int(" 30 ") == 30
    assert _positive_int(30) == 30
    # Still defensive: a model that sends prose or a negative gets "no filter",
    # never a query built from something that is not a count of days.
    assert _positive_int("ba mươi") is None
    assert _positive_int("-5") is None
    assert _positive_int(True) is None


def test_a_tool_outside_the_catalogue_still_gets_a_spec() -> None:
    """A spec may name a capability this repo has no catalogue entry for; not being
    able to describe it is not a reason to withhold it from the model.
    """
    spec = _tool_spec("finance.unknown_tool", "read")

    assert spec["function"]["name"] == "finance.unknown_tool"
    assert spec["function"]["parameters"]["additionalProperties"] is True


def test_a_catalogue_tool_gets_its_real_schema_not_the_stub() -> None:
    spec = _tool_spec("erp.query_receivables", "read")

    assert "days_overdue_gt" in spec["function"]["parameters"]["properties"]
    assert spec["function"]["description"] != "UC-1 tool (read)"
    # The view name belongs in the description: it is what the citation guardrail
    # then requires the answer to name.
    assert "v_ar_open" in spec["function"]["description"]


def test_descriptions_never_leak_a_forbidden_column() -> None:
    """`prohibitions.never_expose` names columns the agent must not surface. Telling
    the model they exist is the first step toward it asking for them.
    """
    forbidden = ("bank_account", "tax_id", "personal_phone", "national_id", "credit_limit_internal")
    blob = re.sub(r"\s+", " ", str(TOOL_SCHEMAS)).lower()

    assert [column for column in forbidden if column in blob] == []
