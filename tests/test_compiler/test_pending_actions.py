"""UC1-21 (#137) — a `require_approval` decision is captured on `AgentState.
pending_actions` in the exact shape `src.workflow.uc1_pending_actions` persists.

UC1-23 (#139), FR-45 — `execute_tool_call`'s own hash binding: an `Approval`
is only ever honoured for the *exact* `args` it was computed against.
`_approval_binding_pep` below is a small, faithful stand-in for
`authz.rego`'s own `approval_required`/`approval_granted` rules (mirroring
`test_provenance.py`'s `_t2_faithful_pep` for T2) — the real Rego rule has its
own coverage in `authz_test.rego`; this proves the Python side actually
submits what that rule needs to fire.
"""

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from src.agentspec.spec import Scope, SpecStatus
from src.compiler.state import initial_state
from src.compiler.tools import (
    ApprovalRequiredError,
    BoundTool,
    RowScope,
    ToolCallContext,
    ToolDeniedError,
    execute_tool_call,
    hash_action,
)
from src.policy.pep import Approval, AuthzRequest, Decision, Effect, Principal
from tests.conftest import ScriptedToolModel
from tests.test_compiler.test_graph import _PRINCIPAL, _compile, _noop_audit_sink, _spec


def _require_approval_pep(*, approver_roles: list[str] | None = None):
    def _client(request: AuthzRequest) -> Decision:
        return Decision(
            effect=Effect.REQUIRE_APPROVAL,
            reasons=[],
            row_filter={},
            approver_roles=approver_roles or ["sales_manager"],
            redact_fields=[],
            max_rows=0,
        )

    return _client


def test_a_require_approval_decision_is_captured_as_a_pending_action() -> None:
    spec = _spec(
        capabilities=[{"tool": "mail.send_reminder", "scope": "write", "requires_approval": True}],
        authority={"autonomy": "propose_then_act", "budget": {"max_tool_calls_per_session": 40}},
    )
    compiled = _compile(
        spec,
        model=ScriptedToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "mail.send_reminder", "args": {"customer_id": "cust-a"}, "id": "c1"}],
                )
            ]
        ),
        tool_implementations={"mail.send_reminder": lambda args, scope: {"queued": True}},
        pep_client=_require_approval_pep(approver_roles=["sales_manager"]),
    )

    final_state = compiled.graph.invoke(initial_state([HumanMessage(content="gửi email nhắc nợ")], principal=_PRINCIPAL))

    assert final_state["pending_actions"] == [
        {
            "tool": "mail.send_reminder",
            "args": {"customer_id": "cust-a"},
            "action_hash": hash_action("mail.send_reminder", {"customer_id": "cust-a"}),
            "connection_id": "",
            "approver_roles": ["sales_manager"],
        }
    ]


def _approval_binding_pep():
    """`authz.rego`'s own two rules, faithfully: `approval_granted` needs a
    same-tool, same-hash, different-approver-than-principal entry in
    `input.approvals`; absent that, `require_approval` (never a hard `deny` —
    matching real Rego, where a mismatched approval simply fails to grant
    rather than triggering a `deny` rule).
    """

    def _client(request: AuthzRequest) -> Decision:
        granted = any(
            a.tool == request.action.tool and a.action_hash == request.action.hash and a.approver_sub != request.principal.sub
            for a in request.approvals
        )
        if granted:
            return Decision(effect=Effect.ALLOW, reasons=[], row_filter={}, approver_roles=[], redact_fields=[], max_rows=0)
        return Decision(
            effect=Effect.REQUIRE_APPROVAL, reasons=[], row_filter={}, approver_roles=["sales_manager"], redact_fields=[], max_rows=0
        )

    return _client


def _bound_write(reached: dict[str, Any]) -> BoundTool:
    def _impl(args: dict[str, Any], scope: RowScope) -> dict[str, Any]:
        reached["called"] = True
        return {"sent": True}

    return BoundTool(name="mail.send_reminder", scope=Scope.WRITE, connection_id="conn_mail", implementation=_impl)


def _ctx(*, approvals: list[Approval]) -> ToolCallContext:
    spec = _spec()
    policy_spec = spec.model_copy(update={"metadata": spec.metadata.model_copy(update={"status": SpecStatus.DEPLOYED})})
    return ToolCallContext(
        policy_spec=policy_spec,
        principal=Principal(sub="u_rep_1", employee_id="E102", roles=["sales_rep"], department="SALES"),
        provenance={"from_untrusted_data": False, "sources": []},
        session={"tool_calls_used": 0, "per_tool_calls": {}},
        pep_client=_approval_binding_pep(),
        audit_sink=_noop_audit_sink,
        approvals=approvals,
    )


def test_a_matching_approval_lets_execution_through() -> None:
    reached: dict[str, Any] = {"called": False}
    bound = _bound_write(reached)
    original_args = {"customer_id": "cust-a", "amount_due": 500}
    approval = Approval(
        tool="mail.send_reminder", approver_sub="u_manager_9", approver_roles=["sales_manager"], action_hash=hash_action("mail.send_reminder", original_args)
    )

    outcome = execute_tool_call(bound, original_args, _ctx(approvals=[approval]))

    assert reached["called"] is True
    assert outcome.result == {"sent": True}


def test_editing_the_payload_after_approval_invalidates_it() -> None:
    """FR-45's own acceptance criterion: "duyệt một action, sửa amount trước
    khi thực thi → PEP deny vì hash không khớp" — modelled here as an
    `Approval` bound to the *original* payload's hash, replayed against
    *edited* `args`: `execute_tool_call` recomputes the hash from what it was
    actually called with, so the stale approval no longer matches and the
    real tool never runs.
    """
    reached: dict[str, Any] = {"called": False}
    bound = _bound_write(reached)
    original_args = {"customer_id": "cust-a", "amount_due": 500}
    edited_args = {"customer_id": "cust-a", "amount_due": 999999}
    approval = Approval(
        tool="mail.send_reminder",
        approver_sub="u_manager_9",
        approver_roles=["sales_manager"],
        action_hash=hash_action("mail.send_reminder", original_args),
    )

    try:
        execute_tool_call(bound, edited_args, _ctx(approvals=[approval]))
        raised = False
    except (ToolDeniedError, ApprovalRequiredError):
        raised = True

    assert raised is True
    assert reached["called"] is False


def test_approver_equal_to_requester_never_grants_even_with_a_matching_hash() -> None:
    """FR-46 at the PEP layer: same faithful fake, `approver_sub == principal.sub`."""
    reached: dict[str, Any] = {"called": False}
    bound = _bound_write(reached)
    args = {"customer_id": "cust-a"}
    self_approval = Approval(tool="mail.send_reminder", approver_sub="u_rep_1", approver_roles=["sales_manager"], action_hash=hash_action("mail.send_reminder", args))

    try:
        execute_tool_call(bound, args, _ctx(approvals=[self_approval]))
        raised = False
    except (ToolDeniedError, ApprovalRequiredError):
        raised = True

    assert raised is True
    assert reached["called"] is False
