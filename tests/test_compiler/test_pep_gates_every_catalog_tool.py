"""UC1-22 (#138), FR-44 — no tool call, of any scope, ever reaches its real
implementation without first clearing the PEP.

`execute_tool_call` (`src.compiler.tools`) is already the only place
`BoundTool.implementation` is ever invoked inside this package — there is no
per-tool special case anywhere in `_tools_node` (`src.compiler.graph`) that
could accidentally call a tool's real implementation directly. That is a
structural fact about the code today, not a fact this suite can trust to stay
true on its own: the ticket's own acceptance criterion is that a *future* tool
added to the catalog without going through that one path gets caught by an
integration test, not by someone re-reading `graph.py`.

So this suite runs a real compiled graph — not a hand-built one — through
every tool in `src.agentspec.tool_catalog.CATALOG`, all bound to spy
implementations, with a PEP that always refuses. If a later change ever wires
a new (or existing) tool's implementation straight into the tools node instead
of through `execute_tool_call`, one of these spies fires and this test fails.
"""

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from src.agentspec.spec import Scope
from src.agentspec.tool_catalog import CATALOG
from src.compiler.state import initial_state
from src.compiler.tools import RowScope
from tests.conftest import ScriptedToolModel
from tests.test_compiler.test_graph import _PRINCIPAL, _compile, _deny_pep, _spec

#: One capability per catalog tool, each with a distinct connection-free
#: binding and `requires_approval` set for the write ones — mirrors
#: `agentspec.yaml`'s own two write tools (§7.1's schema requires the field
#: whenever `scope: write`).
_ALL_CAPABILITIES = [
    {"tool": entry.name, "scope": entry.scope.value, **({"requires_approval": True} if entry.scope is Scope.WRITE else {})}
    for entry in CATALOG
]


def _spy_implementations() -> tuple[dict[str, Any], dict[str, bool]]:
    reached: dict[str, bool] = dict.fromkeys((entry.name for entry in CATALOG), False)

    def _make(name: str):
        def _impl(args: Mapping[str, Any], scope: RowScope) -> dict[str, Any]:
            reached[name] = True
            return {"note": f"the real {name} ran — PEP was bypassed"}

        return _impl

    implementations = {entry.name: _make(entry.name) for entry in CATALOG}
    return implementations, reached


def _call_every_catalog_tool_model() -> ScriptedToolModel:
    tool_calls = [{"name": entry.name, "args": {}, "id": f"c-{entry.name}"} for entry in CATALOG]
    return ScriptedToolModel(
        responses=[
            AIMessage(content="", tool_calls=tool_calls),
            AIMessage(content="đã thử tất cả các tool trong catalog."),
        ]
    )


def test_a_pep_that_denies_everything_stops_every_catalog_tool_before_its_implementation() -> None:
    implementations, reached = _spy_implementations()
    spec = _spec(
        capabilities=_ALL_CAPABILITIES,
        authority={"autonomy": "propose_then_act", "budget": {"max_tool_calls_per_session": 40}},
    )
    compiled = _compile(
        spec,
        model=_call_every_catalog_tool_model(),
        tool_implementations=implementations,
        pep_client=_deny_pep("no path around the PEP"),
    )

    final_state = compiled.graph.invoke(
        initial_state([HumanMessage(content="thử mọi tool")], principal=_PRINCIPAL)
    )

    assert reached == dict.fromkeys(reached, False), f"a tool's real implementation ran despite a deny: {reached}"
    tool_messages = [m for m in final_state["messages"] if getattr(m, "tool_call_id", None) is not None]
    assert len(tool_messages) == len(CATALOG)
    assert all("denied" in str(m.content) for m in tool_messages)


def test_a_pep_that_always_requires_approval_never_reaches_a_catalog_tools_implementation_either() -> None:
    """`require_approval` is a distinct effect from `deny` (`src.policy.pep.Effect`)
    with its own branch in `execute_tool_call` — worth its own case since a
    future bug could plausibly handle one branch's "don't call the real tool"
    rule and forget the other.
    """

    def _require_approval_pep(request: Any):
        from src.policy.pep import Decision, Effect

        return Decision(
            effect=Effect.REQUIRE_APPROVAL, reasons=[], row_filter={}, approver_roles=["agent_admin"], redact_fields=[], max_rows=0
        )

    implementations, reached = _spy_implementations()
    spec = _spec(
        capabilities=_ALL_CAPABILITIES,
        authority={"autonomy": "propose_then_act", "budget": {"max_tool_calls_per_session": 40}},
    )
    compiled = _compile(
        spec, model=_call_every_catalog_tool_model(), tool_implementations=implementations, pep_client=_require_approval_pep
    )

    compiled.graph.invoke(initial_state([HumanMessage(content="thử mọi tool")], principal=_PRINCIPAL))

    assert reached == dict.fromkeys(reached, False), f"a tool's real implementation ran despite require_approval: {reached}"
