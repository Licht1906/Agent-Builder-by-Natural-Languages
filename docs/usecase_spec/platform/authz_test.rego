# UC1-13 (#114): one positive and one negative case per `deny contains` rule in
# `authz.rego`, plus the row_filter branches and the approval-integrity rules
# (self-approval, action_hash mismatch) that §11.3 calls out by name.
#
# Fixtures mirror the real shape in `agentspec.yaml`, not an abbreviated one —
# a fixture that drops fields the real spec always carries would let a rule
# pass here and still break against a real compiled spec.
#
# `with input as X` at the end of a test rule overrides `input` for every
# expression evaluated inside that rule, including calls into `data.agentbuilder.authz`
# — so `authz.effect`/`authz.deny`/`authz.row_filter` below all see `X`, not the
# real request.

package agentbuilder.authz_test

import data.agentbuilder.authz
import rego.v1

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

base_spec := {
	"metadata": {"status": "deployed"},
	"audience": {
		"roles": ["sales_rep", "sales_manager"],
		"departments": ["SALES"],
		"data_scope": "own_records",
	},
	"capabilities": [
		{
			"tool": "erp.query_receivables",
			"scope": "read",
			"constraints": {
				"max_rows": 500,
				"max_calls_per_session": 15,
				"allowed_columns": ["order_id", "amount_due", "days_overdue"],
			},
		},
		{"tool": "kb.search", "scope": "read"},
		{"tool": "mail.send_reminder", "scope": "write", "requires_approval": true},
		{"tool": "task.create", "scope": "write", "requires_approval": true},
	],
	"prohibitions": {
		"never_expose": ["bank_account", "tax_id", "credit_limit_internal"],
		"deny_tools": ["erp.update_*", "erp.delete_*", "finance.*"],
		"external_egress": false,
	},
	"authority": {
		"autonomy": "propose_then_act",
		"approval_matrix": {
			"mail.send_reminder": {"approver_roles": ["sales_manager"]},
			"task.create": {
				"approver_roles": ["sales_manager"],
				"condition": {"field": "assignee_id", "op": "neq", "value": "$principal.employee_id"},
			},
		},
		"budget": {"max_tool_calls_per_session": 40},
	},
	"connections": [
		{"id": "conn_erp", "allowed_objects": ["v_ar_open", "v_customer_safe"]},
		{"id": "conn_mail", "allowed_hosts": ["smtp.internal.corp"]},
	],
}

base_principal := {
	"sub": "u_rep_1",
	"employee_id": "E102",
	"roles": ["sales_rep"],
	"department": "SALES",
}

base_action := {
	"tool": "erp.query_receivables",
	"scope": "read",
	"mode": "execute",
	"hash": "hash-read-1",
	"connection_id": "conn_erp",
	"args": {},
}

base_provenance := {"from_untrusted_data": false, "sources": []}

base_session := {"tool_calls_used": 0, "per_tool_calls": {}}

base_input := {
	"spec": base_spec,
	"principal": base_principal,
	"action": base_action,
	"provenance": base_provenance,
	"session": base_session,
	"approvals": [],
}

# `with_spec`/`with_action`/... shallow-merge one top-level key of `base_input`
# so each test only states the field it actually varies.
with_spec(overrides) := object.union(base_input, {"spec": object.union(base_spec, overrides)})

with_audience(overrides) := object.union(base_input, {"spec": object.union(base_spec, {"audience": object.union(base_spec.audience, overrides)})})

with_authority(overrides) := object.union(base_input, {"spec": object.union(base_spec, {"authority": object.union(base_spec.authority, overrides)})})

with_prohibitions(overrides) := object.union(base_input, {"spec": object.union(base_spec, {"prohibitions": object.union(base_spec.prohibitions, overrides)})})

with_principal(overrides) := object.union(base_input, {"principal": object.union(base_principal, overrides)})

with_action(overrides) := object.union(base_input, {"action": object.union(base_action, overrides)})

with_provenance(overrides) := object.union(base_input, {"provenance": object.union(base_provenance, overrides)})

with_session(overrides) := object.union(base_input, {"session": object.union(base_session, overrides)})

with_approvals(approvals) := object.union(base_input, {"approvals": approvals})

deny_contains(substr) if {
	some msg in authz.deny
	contains(msg, substr)
}

# ---------------------------------------------------------------------------
# Sanity: the fixture itself, unmodified, is a clean allow.
# ---------------------------------------------------------------------------

test_baseline_fixture_allows if {
	authz.effect == "allow" with input as base_input
}

# ---------------------------------------------------------------------------
# spec.metadata.status
# ---------------------------------------------------------------------------

test_undeployed_spec_denies if {
	deny_contains("expected \"deployed\"") with input as with_spec({"metadata": {"status": "draft"}})
}

test_deployed_spec_does_not_deny_on_status if {
	not deny_contains("expected \"deployed\"") with input as base_input
}

# ---------------------------------------------------------------------------
# audience.roles — valid / invalid caller role
# ---------------------------------------------------------------------------

test_role_not_in_audience_denies if {
	deny_contains("caller holds no role listed in audience.roles") with input as with_principal({"roles": ["hr_staff"]})
}

test_role_in_audience_allows if {
	not deny_contains("caller holds no role listed in audience.roles") with input as base_input
}

# ---------------------------------------------------------------------------
# audience.departments — valid / invalid caller department
# ---------------------------------------------------------------------------

test_department_not_in_audience_denies if {
	deny_contains("not in audience.departments") with input as with_principal({"department": "MARKETING"})
}

test_department_in_audience_allows if {
	not deny_contains("not in audience.departments") with input as base_input
}

# ---------------------------------------------------------------------------
# data_scope — three levels, row_filter mirrors each
# ---------------------------------------------------------------------------

test_data_scope_own_records_row_filter if {
	authz.row_filter == {"scope": "own_records", "principal_id": "E102"} with input as base_input
}

test_data_scope_department_row_filter if {
	authz.row_filter == {"scope": "department", "department": "SALES"} with input as with_audience({"data_scope": "department"})
}

test_data_scope_all_row_filter if {
	authz.row_filter == {"scope": "all"} with input as with_audience({"data_scope": "all"})
}

# A manager is widened to their department; a rep under the same spec is not.
test_manager_widens_own_records_to_department if {
	authz.row_filter == {"scope": "department", "department": "SALES"} with input as with_principal({"roles": ["sales_manager"]})
}

# The spec's ceiling still holds: widening only ever reaches `department`, and a
# manager on an `own_records` spec never gets `all`.
test_manager_widening_never_reaches_all if {
	authz.row_filter.scope != "all" with input as with_principal({"roles": ["sales_manager"]})
}

# ---------------------------------------------------------------------------
# capability allow-list — tool must exist in capabilities[]
# ---------------------------------------------------------------------------

test_tool_not_in_capabilities_denies if {
	deny_contains("is not in capabilities") with input as with_action({"tool": "erp.query_all"})
}

test_tool_in_capabilities_allows if {
	not deny_contains("is not in capabilities") with input as base_input
}

# ---------------------------------------------------------------------------
# read-scoped capability called for write
# ---------------------------------------------------------------------------

test_read_capability_called_as_write_denies if {
	deny_contains("granted read scope but was called for write") with input as with_action({"scope": "write"})
}

test_read_capability_called_as_read_allows if {
	not deny_contains("granted read scope but was called for write") with input as base_input
}

# ---------------------------------------------------------------------------
# prohibitions.deny_tools — glob match on invented write variants
# ---------------------------------------------------------------------------

test_deny_tools_glob_matches_variant_denies if {
	deny_contains("matches deny_tools pattern") with input as with_action({"tool": "erp.update_customer", "scope": "write"})
}

test_deny_tools_glob_no_match_allows if {
	not deny_contains("matches deny_tools pattern") with input as base_input
}

# ---------------------------------------------------------------------------
# prohibitions.external_egress — host allowlist from connections[]
# ---------------------------------------------------------------------------

test_egress_to_undeclared_host_denies if {
	deny_contains("host not declared in connections") with input as with_action({"args": {"host": "evil.example.com"}})
}

test_egress_to_declared_host_allows if {
	not deny_contains("host not declared in connections") with input as with_action({"args": {"host": "smtp.internal.corp"}})
}

# ---------------------------------------------------------------------------
# T2 — provenance flag blocks writes derived from untrusted content.
# §17.2 R2 marks this "Cao" (high risk) — mandatory, not optional coverage.
# ---------------------------------------------------------------------------

untrusted_write_input := object.union(
	with_action({"tool": "mail.send_reminder", "scope": "write", "hash": "hash-mail-untrusted"}),
	{"provenance": {"from_untrusted_data": true, "sources": ["v_ar_open"]}},
)

test_untrusted_provenance_write_denies if {
	deny_contains("write action derived from untrusted content") with input as untrusted_write_input
}

test_trusted_provenance_write_does_not_deny_on_provenance if {
	not deny_contains("write action derived from untrusted content") with input as with_action({"tool": "mail.send_reminder", "scope": "write", "hash": "hash-mail-trusted"})
}

# ---------------------------------------------------------------------------
# authority.autonomy
# ---------------------------------------------------------------------------

read_only_write_input := object.union(
	with_authority({"autonomy": "read_only"}),
	{"action": object.union(base_action, {"tool": "mail.send_reminder", "scope": "write"})},
)

test_autonomy_read_only_blocks_write_denies if {
	deny_contains("autonomy is read_only; write tools are unreachable") with input as read_only_write_input
}

test_autonomy_read_only_allows_read if {
	not deny_contains("autonomy is read_only; write tools are unreachable") with input as with_authority({"autonomy": "read_only"})
}

propose_execute_input := object.union(
	with_authority({"autonomy": "propose"}),
	{"action": object.union(base_action, {"tool": "mail.send_reminder", "scope": "write", "mode": "execute"})},
)

propose_plan_input := object.union(
	with_authority({"autonomy": "propose"}),
	{"action": object.union(base_action, {"tool": "mail.send_reminder", "scope": "write", "mode": "propose"})},
)

test_autonomy_propose_execute_denies if {
	deny_contains("a human must execute this action") with input as propose_execute_input
}

test_autonomy_propose_non_execute_mode_allows if {
	not deny_contains("a human must execute this action") with input as propose_plan_input
}

# ---------------------------------------------------------------------------
# authority.budget — session-wide and per-tool
# ---------------------------------------------------------------------------

test_session_budget_exhausted_denies if {
	deny_contains("session tool-call budget exhausted") with input as with_session({"tool_calls_used": 40})
}

test_session_budget_remaining_allows if {
	not deny_contains("session tool-call budget exhausted") with input as with_session({"tool_calls_used": 39})
}

test_per_tool_budget_exhausted_denies if {
	deny_contains("per-tool budget exhausted for \"erp.query_receivables\"") with input as with_session({"per_tool_calls": {"erp.query_receivables": 15}})
}

test_per_tool_budget_remaining_allows if {
	not deny_contains("per-tool budget exhausted for \"erp.query_receivables\"") with input as with_session({"per_tool_calls": {"erp.query_receivables": 14}})
}

# ---------------------------------------------------------------------------
# Data surface — allowed_columns, never_expose, allowed_objects
# ---------------------------------------------------------------------------

test_column_outside_allowed_columns_denies if {
	deny_contains("not in allowed_columns") with input as with_action({"args": {"columns": ["ssn"]}})
}

test_column_within_allowed_columns_allows if {
	not deny_contains("not in allowed_columns") with input as with_action({"args": {"columns": ["amount_due"]}})
}

test_never_expose_column_denies_even_if_requested if {
	deny_contains("is listed in never_expose") with input as with_action({"args": {"columns": ["bank_account"]}})
}

test_column_not_in_never_expose_allows if {
	not deny_contains("is listed in never_expose") with input as with_action({"args": {"columns": ["amount_due"]}})
}

test_object_outside_allowed_objects_denies if {
	deny_contains("not in connections[].allowed_objects") with input as with_action({"args": {"object": "fin_invoice"}})
}

test_object_within_allowed_objects_allows if {
	not deny_contains("not in connections[].allowed_objects") with input as with_action({"args": {"object": "v_ar_open"}})
}

# ---------------------------------------------------------------------------
# Approval integrity — self-approval and action_hash binding.
# Neither is a `deny` rule: a broken approval simply never grants, so the
# decision stays "require_approval" rather than escalating to "allow".
# ---------------------------------------------------------------------------

self_approval_input := object.union(
	with_action({"tool": "mail.send_reminder", "scope": "write", "hash": "hash-mail-self"}),
	{"approvals": [{
		"tool": "mail.send_reminder",
		"approver_sub": "u_rep_1", # same as principal.sub — the requester
		"approver_roles": ["sales_manager"],
		"action_hash": "hash-mail-self",
	}]},
)

distinct_approver_input := object.union(
	with_action({"tool": "mail.send_reminder", "scope": "write", "hash": "hash-mail-ok"}),
	{"approvals": [{
		"tool": "mail.send_reminder",
		"approver_sub": "u_manager_9", # distinct from principal.sub
		"approver_roles": ["sales_manager"],
		"action_hash": "hash-mail-ok",
	}]},
)

hash_mismatch_input := object.union(
	with_action({"tool": "mail.send_reminder", "scope": "write", "hash": "hash-mail-changed"}),
	{"approvals": [{
		"tool": "mail.send_reminder",
		"approver_sub": "u_manager_9",
		"approver_roles": ["sales_manager"],
		"action_hash": "hash-mail-original", # approved a different payload
	}]},
)

hash_match_input := object.union(
	with_action({"tool": "mail.send_reminder", "scope": "write", "hash": "hash-mail-same"}),
	{"approvals": [{
		"tool": "mail.send_reminder",
		"approver_sub": "u_manager_9",
		"approver_roles": ["sales_manager"],
		"action_hash": "hash-mail-same",
	}]},
)

test_self_approval_does_not_grant if {
	authz.effect == "require_approval" with input as self_approval_input
}

test_distinct_approver_grants_allow if {
	authz.effect == "allow" with input as distinct_approver_input
}

test_action_hash_mismatch_does_not_grant if {
	authz.effect == "require_approval" with input as hash_mismatch_input
}

test_matching_action_hash_grants_allow if {
	authz.effect == "allow" with input as hash_match_input
}

# ---------------------------------------------------------------------------
# approval_matrix structured condition — task.create is approval-free only
# when the assignee is the caller themself.
# ---------------------------------------------------------------------------

test_task_assigned_to_self_needs_no_approval if {
	authz.effect == "allow" with input as with_action({"tool": "task.create", "scope": "write", "hash": "hash-task-self", "args": {"assignee_id": "E102"}})
}

test_task_assigned_to_someone_else_requires_approval if {
	authz.effect == "require_approval" with input as with_action({"tool": "task.create", "scope": "write", "hash": "hash-task-other", "args": {"assignee_id": "E999"}})
}
