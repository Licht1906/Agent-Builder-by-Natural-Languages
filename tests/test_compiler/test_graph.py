"""UC1-10 (#124) — `compile_spec`: a spec becomes a runnable graph bound to
exactly its `capabilities[]`, and every tool call it makes sends the PEP a
frozen, `deployed`-status copy of the spec regardless of the spec's real
lifecycle status.

`ScriptedToolModel` is `tests.conftest`'s own fake tool-calling chat model —
`bind_tools` returns `self`, and it replays a fixed list of `AIMessage`s, the
same seam every other suite in this repo scripts a model with (§20's "Seam
thứ hai: model factory").
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.agentspec.views import DisallowedBaseTableError
from src.compiler.graph import (
    CompiledAgent,
    CompiledAgentRegistry,
    CompileDependencies,
    UnboundCapabilityError,
    UnverifiedConnectionObjectsError,
    compile_spec,
)
from src.compiler.state import initial_state
from src.compiler.tools import RowScope, ToolImplementation, default_tool_implementations
from src.policy.pep import AuditSink, AuthzRequest, Decision, Effect, PEPClient, Principal
from tests.conftest import ScriptedToolModel
from tests.test_agentspec.conftest import spec_payload

_PRINCIPAL = Principal(sub="u1", employee_id="E102", roles=["sales_rep"], department="SALES")


def _noop_audit_sink(request_id: str, request: AuthzRequest, decision: Decision) -> None:
    """UC1-32 (#144): every test in this suite is about compiling/running the
    graph, not about auditing — a spy that records nothing, the same role
    `NullTrace` plays for `src.services.tracing`. Tests that care whether an
    `AuditSink` actually gets called live in `tests/test_db` (real DB) or
    build their own recording fake inline.
    """


def _compile(
    spec: Any,
    *,
    model: Any,
    tool_implementations: dict[str, ToolImplementation],
    pep_client: PEPClient,
    audit_sink: AuditSink = _noop_audit_sink,
) -> CompiledAgent:
    """`compile_spec(spec, CompileDependencies(...))`, spelled the way most of this
    suite wants to call it — one keyword per axis that actually varies test to
    test, matching the shape `compile_spec` had before `CompileDependencies`
    bundled its four dependencies into one argument.
    """
    return compile_spec(
        spec,
        CompileDependencies(
            model=model, tool_implementations=tool_implementations, pep_client=pep_client, audit_sink=audit_sink
        ),
    )


def _allow_pep(*, row_filter: dict[str, Any] | None = None):
    seen: list[AuthzRequest] = []

    def _client(request: AuthzRequest) -> Decision:
        seen.append(request)
        return Decision(
            effect=Effect.ALLOW,
            reasons=[],
            row_filter=row_filter or {},
            approver_roles=[],
            redact_fields=[],
            max_rows=100,
        )

    _client.seen = seen  # type: ignore[attr-defined]
    return _client


def _deny_pep(reason: str):
    def _client(request: AuthzRequest) -> Decision:
        return Decision(
            effect=Effect.DENY, reasons=[reason], row_filter={}, approver_roles=[], redact_fields=[], max_rows=0
        )

    return _client


def _tool_implementations() -> dict[str, Any]:
    return default_tool_implementations(resolve_credential=lambda ref: "postgresql://ro:pw@erp/db")


def test_compiles_and_binds_exactly_the_capabilities() -> None:
    spec = _spec()
    compiled = _compile(
        spec, model=ScriptedToolModel(responses=[]), tool_implementations=_tool_implementations(), pep_client=_allow_pep()
    )

    assert compiled.bound_tool_names == {"erp.query_receivables"}
    assert compiled.agent_id == "agt_ar_assistant"
    assert compiled.version == 1


def test_a_capability_with_no_implementation_refuses_to_compile() -> None:
    spec = _spec(
        capabilities=[{"tool": "task.create", "scope": "write", "requires_approval": True}],
        authority={"autonomy": "propose_then_act", "budget": {"max_tool_calls_per_session": 40}},
    )
    with pytest.raises(UnboundCapabilityError):
        _compile(spec, model=ScriptedToolModel(responses=[]), tool_implementations={}, pep_client=_allow_pep())


def test_pep_receives_the_frozen_deployed_spec_never_the_drafts_real_status() -> None:
    """§7 AC: "PEP nhận đúng spec đã compile, không phải spec draft"."""
    spec = _spec()  # metadata.status == "draft"
    assert spec.metadata.status.value == "draft"

    pep = _allow_pep()
    compiled = _compile(spec, model=_read_then_answer_model(), tool_implementations=_tool_implementations(), pep_client=pep)

    _run_turn(compiled, "Khách nào quá hạn trên 30 ngày?")

    assert pep.seen, "PEP was never called"
    assert all(request.spec.metadata.status.value == "deployed" for request in pep.seen)
    assert compiled.policy_spec.metadata.status.value == "deployed"


def test_a_full_turn_calls_the_tool_then_returns_a_final_answer() -> None:
    compiled = _compile(
        _spec(), model=_read_then_answer_model(), tool_implementations=_tool_implementations(), pep_client=_allow_pep()
    )

    final_state = _run_turn(compiled, "Khách nào quá hạn trên 30 ngày?")

    assert final_state["session"]["tool_calls_used"] == 1
    assert final_state["session"]["per_tool_calls"] == {"erp.query_receivables": 1}
    last_message = final_state["messages"][-1]
    assert isinstance(last_message, AIMessage)
    assert not last_message.tool_calls

    # UC1-38 (#126) FR-37: the transcript records both the tool step and the
    # agent's own steps, with a nonzero cost — read directly off graph state,
    # never reconstructed from `messages`.
    tool_steps = [step for step in final_state["transcript"] if step["node"] == "tools"]
    assert tool_steps == [
        {
            "node": "tools",
            "tool": "erp.query_receivables",
            "args": {"days_overdue_gt": 30},
            "result": tool_steps[0]["result"],
            "tokens": None,
            "latency_ms": tool_steps[0]["latency_ms"],
            "cost_usd": 0.0,
            "row_filter": {},
        }
    ]
    assert sum(step["cost_usd"] for step in final_state["transcript"]) > 0


def test_a_budget_exhausted_denial_stops_the_turn_instead_of_looping() -> None:
    """FR-39: "vượt trần thì dừng ... giống runtime thật" — the graph must not loop back to the agent."""
    agent_calls = 0

    class _CountingModel(ScriptedToolModel):
        def bind_tools(self, tools: object, *, tool_choice: str | None = None, **kwargs: object):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
            nonlocal agent_calls
            agent_calls += 1
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = _CountingModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "erp.query_receivables", "args": {}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "erp.query_receivables", "args": {}, "id": "c2"}]),
        ]
    )
    compiled = _compile(
        _spec(), model=model, tool_implementations=_tool_implementations(), pep_client=_deny_pep("session tool-call budget exhausted (1)")
    )

    final_state = _run_turn(compiled, "hỏi liên tục")

    assert agent_calls == 1, "the agent node must not run again after a budget-exhausted denial"
    assert final_state["budget_exceeded"] is True


def test_a_denied_write_tool_never_reaches_its_implementation() -> None:
    called = False

    def _write(args: dict[str, Any], scope: RowScope) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"queued": True}

    spec = _spec(
        capabilities=[{"tool": "mail.send_reminder", "scope": "write", "requires_approval": True}],
        authority={"autonomy": "propose_then_act", "budget": {"max_tool_calls_per_session": 40}},
    )
    compiled = _compile(
        spec,
        model=ScriptedToolModel(
            responses=[AIMessage(content="", tool_calls=[{"name": "mail.send_reminder", "args": {}, "id": "c1"}])]
        ),
        tool_implementations={"mail.send_reminder": _write},
        pep_client=_deny_pep("write action derived from untrusted content"),
    )

    state = compiled.graph.invoke(initial_state([HumanMessage(content="gửi email nhắc nợ")], principal=_PRINCIPAL))

    assert called is False
    tool_message = next(m for m in state["messages"] if getattr(m, "tool_call_id", None) == "c1")
    assert "denied" in tool_message.content


def test_recompiling_a_new_version_does_not_disturb_an_already_registered_version() -> None:
    registry = CompiledAgentRegistry()
    v1 = _compile(_spec(), model=ScriptedToolModel(responses=[]), tool_implementations=_tool_implementations(), pep_client=_allow_pep())
    registry.register(v1)

    v2_spec = _spec(
        metadata={**spec_payload()["metadata"], "version": 2},
        capabilities=[{"tool": "kb.search", "scope": "read"}],
    )
    v2 = _compile(v2_spec, model=ScriptedToolModel(responses=[]), tool_implementations=_tool_implementations(), pep_client=_allow_pep())
    registry.register(v2)

    assert registry.get("agt_ar_assistant", 1) is v1
    assert registry.get("agt_ar_assistant", 2) is v2
    assert registry.get("agt_ar_assistant", 1).bound_tool_names == {"erp.query_receivables"}
    assert registry.get("agt_ar_assistant", 2).bound_tool_names == {"kb.search"}


def test_compile_refuses_a_connection_whose_allowed_objects_were_never_verified_as_views() -> None:
    """FR-24 at compile time, not only at probe time (UC1-09's own "tại thời điểm probe/compile")."""
    spec = _spec(
        connections=[
            {
                "id": "conn_erp",
                "type": "postgres",
                "credential_ref": "vault://conn/erp_readonly",
                "allowed_objects": ["orders"],
                "read_only": True,
            }
        ]
    )
    with pytest.raises(DisallowedBaseTableError):
        compile_spec(
            spec,
            CompileDependencies(
                model=ScriptedToolModel(responses=[]),
                tool_implementations=_tool_implementations(),
                pep_client=_allow_pep(),
                audit_sink=_noop_audit_sink,
                verified_connection_objects={"conn_erp": ["v_ar_open"]},
            ),
        )


def test_compile_accepts_a_connection_whose_allowed_objects_a_probe_already_verified() -> None:
    spec = _spec(
        connections=[
            {
                "id": "conn_erp",
                "type": "postgres",
                "credential_ref": "vault://conn/erp_readonly",
                "allowed_objects": ["v_ar_open"],
                "read_only": True,
            }
        ]
    )
    compiled = compile_spec(
        spec,
        CompileDependencies(
            model=ScriptedToolModel(responses=[]),
            tool_implementations=_tool_implementations(),
            pep_client=_allow_pep(),
            audit_sink=_noop_audit_sink,
            verified_connection_objects={"conn_erp": ["v_ar_open", "v_customer_safe"]},
        ),
    )
    assert compiled.bound_tool_names == {"erp.query_receivables"}


def test_an_unverified_connection_is_not_reported_as_a_base_table() -> None:
    """The message a 422 carries names the connection, not `information_schema`.

    `v_ar_open`/`v_customer_safe` *are* views (migration `a4c2c2363959`), so the
    parent's "không chấp nhận bảng gốc" wording sent readers of the demo runbook
    looking for a base table that does not exist — while the real cause was a
    connection whose last successful probe confirmed nothing.
    """
    spec = _spec(
        connections=[
            {
                "id": "conn_erp",
                "type": "postgres",
                "credential_ref": "vault://conn/erp_readonly",
                "allowed_objects": ["v_ar_open", "v_customer_safe"],
                "read_only": True,
            }
        ]
    )
    with pytest.raises(UnverifiedConnectionObjectsError) as caught:
        compile_spec(
            spec,
            CompileDependencies(
                model=ScriptedToolModel(responses=[]),
                tool_implementations=_tool_implementations(),
                pep_client=_allow_pep(),
                audit_sink=_noop_audit_sink,
            ),
        )

    message = str(caught.value)
    assert "conn_erp" in message
    assert "erp_readonly" in message
    assert "bảng gốc" not in message
    # Still one exception type for the routes that catch FR-24 at either moment.
    assert isinstance(caught.value, DisallowedBaseTableError)


def test_compile_refuses_a_connection_nobody_has_ever_probed() -> None:
    """No entry in `verified_connection_objects` at all — never silently waved through by the default."""
    spec = _spec(
        connections=[
            {
                "id": "conn_erp",
                "type": "postgres",
                "credential_ref": "vault://conn/erp_readonly",
                "allowed_objects": ["v_ar_open"],
                "read_only": True,
            }
        ]
    )
    with pytest.raises(DisallowedBaseTableError):
        compile_spec(
            spec,
            CompileDependencies(
                model=ScriptedToolModel(responses=[]),
                tool_implementations=_tool_implementations(),
                pep_client=_allow_pep(),
                audit_sink=_noop_audit_sink,
            ),
        )


def test_a_tool_call_with_trailing_garbage_in_its_arguments_still_runs() -> None:
    """`AGENT_MODEL` is 7B and sometimes emits a second JSON object after the first.

    LangChain files that under `invalid_tool_calls` with the raw string intact,
    and the next pass through the agent node sends it straight back — where
    OpenRouter's provider `json.loads` it and answers 400 `Extra data: line 1
    column 23`. The leading object is what the model meant; the turn must
    complete on it.
    """
    compiled = _compile(
        _spec(),
        model=_malformed_then_answer_model('{"days_overdue_gt": 30} {"days_overdue_gt": 30}'),
        tool_implementations=_tool_implementations(),
        pep_client=_allow_pep(),
    )

    final_state = _run_turn(compiled, "Khách nào quá hạn trên 30 ngày?")

    assert final_state["session"]["per_tool_calls"] == {"erp.query_receivables": 1}
    repaired = final_state["messages"][1]
    assert isinstance(repaired, AIMessage)
    assert repaired.tool_calls[0]["args"] == {"days_overdue_gt": 30}
    assert not repaired.invalid_tool_calls
    # The raw string must not survive anywhere the wire format can reach it —
    # `_convert_message_to_dict` falls back to `additional_kwargs` the moment
    # both typed lists are empty.
    assert "tool_calls" not in repaired.additional_kwargs


@pytest.mark.parametrize("arguments", ["", "khách hàng quá hạn 30 ngày"])
def test_arguments_that_are_not_a_json_object_are_dropped_not_guessed_at(arguments: str) -> None:
    """Unreadable arguments leave the model a turn to try again — never a tool call
    this graph executes on invented input, and never a 400 the thread cannot recover from.
    """
    compiled = _compile(
        _spec(),
        model=_malformed_then_answer_model(arguments),
        tool_implementations=_tool_implementations(),
        pep_client=_allow_pep(),
    )

    final_state = _run_turn(compiled, "Khách nào quá hạn trên 30 ngày?")

    assert final_state["session"]["per_tool_calls"] == {}
    dropped = final_state["messages"][1]
    assert isinstance(dropped, AIMessage)
    assert not dropped.tool_calls
    assert not dropped.invalid_tool_calls
    assert "tool_calls" not in dropped.additional_kwargs


def _spec(**overrides: Any):
    from src.agentspec import validate_spec

    return validate_spec(spec_payload(**overrides))


def _malformed_then_answer_model(arguments: str) -> ScriptedToolModel:
    """A model whose first reply carries `arguments` verbatim, built the way the real
    provider path builds one: raw `additional_kwargs["tool_calls"]`, which `AIMessage`'s
    own validator sorts into `tool_calls`/`invalid_tool_calls` exactly as
    `langchain_openai` would.
    """
    return ScriptedToolModel(
        responses=[
            AIMessage(
                content="",
                additional_kwargs={
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "erp.query_receivables", "arguments": arguments},
                        }
                    ]
                },
            ),
            AIMessage(content="7 khách hàng quá hạn trên 30 ngày, nguồn v_ar_open."),
        ]
    )


def _read_then_answer_model() -> ScriptedToolModel:
    return ScriptedToolModel(
        responses=[
            AIMessage(
                content="", tool_calls=[{"name": "erp.query_receivables", "args": {"days_overdue_gt": 30}, "id": "c1"}]
            ),
            AIMessage(content="7 khách hàng quá hạn trên 30 ngày, nguồn v_ar_open."),
        ]
    )


def _run_turn(compiled, message: str):
    return compiled.graph.invoke(initial_state([HumanMessage(content=message)], principal=_PRINCIPAL))
