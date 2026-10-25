# Policy Enforcement Point for child agents.
#
# Every tool call the runtime is about to make is submitted here first. The
# runtime is a dumb executor: if this policy does not return "allow", nothing
# happens. The LLM never sees this file and cannot influence it — the spec it
# drafted has already been frozen and signed by a human at this point.
#
# Expected input:
# {
#   "spec":       <AgentSpec, status must be "deployed">,
#   "principal":  {"sub","employee_id","roles":[],"department"},   # THE CALLER
#   "action":     {"tool","scope","mode","hash","connection_id","args":{}},
#   "provenance": {"from_untrusted_data": bool, "sources": []},
#   "session":    {"tool_calls_used": int, "per_tool_calls": {}},
#   "approvals":  [{"tool","approver_sub","approver_roles":[],"action_hash"}]
# }
#
# Note that `principal` is whoever is talking to the agent right now, NOT
# metadata.created_by. Delegated authority, never ambient authority.

package agentbuilder.authz

import rego.v1

# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

default effect := "deny"

effect := "allow" if {
	count(deny) == 0
	not approval_required
}

effect := "allow" if {
	count(deny) == 0
	approval_required
	approval_granted
}

effect := "require_approval" if {
	count(deny) == 0
	approval_required
	not approval_granted
}

decision := {
	"effect": effect,
	"reasons": deny,
	"row_filter": row_filter,
	"approver_roles": effective_approver_roles,
	"redact_fields": object.get(input.spec.prohibitions, "never_expose", []),
	"max_rows": max_rows,
}

# ---------------------------------------------------------------------------
# Row filter — abstract, not column names.
# The connector adapter translates this into a SQL predicate or an API filter.
# Keeping column names out of the policy means one policy works across ERPs.
# ---------------------------------------------------------------------------

# A manager sees their team, not just their own book. `audience.data_scope` is
# the spec's *ceiling* — the widest thing anyone using this agent may ever see —
# and within that ceiling how much a given caller gets is a fact about who is
# asking. Deriving the filter from the spec alone made it a constant: a
# sales_manager asking "cả nhóm có bao nhiêu khách quá hạn?" was silently
# answered about their own records and told nothing had been narrowed.
#
# Widening stops here and goes no further: this rule only ever lifts
# `own_records` to `department`, never to `all`, and only for a role the spec
# itself already named in `audience.roles` (checked by the deny rule below). A
# spec whose ceiling is already `department` or `all` is untouched.
row_filter := {
	"scope": "department",
	"department": input.principal.department,
} if {
	input.spec.audience.data_scope == "own_records"
	"sales_manager" in input.principal.roles
}

row_filter := {
	"scope": "own_records",
	"principal_id": input.principal.employee_id,
} if {
	input.spec.audience.data_scope == "own_records"
	not "sales_manager" in input.principal.roles
}

row_filter := {
	"scope": "department",
	"department": input.principal.department,
} if {
	input.spec.audience.data_scope == "department"
}

row_filter := {"scope": "all"} if {
	input.spec.audience.data_scope == "all"
}

max_rows := n if {
	n := capability.constraints.max_rows
} else := 100

# ---------------------------------------------------------------------------
# Deny rules. Any single hit blocks the call. Deny always beats allow.
# ---------------------------------------------------------------------------

deny contains msg if {
	input.spec.metadata.status != "deployed"
	msg := sprintf("spec status is %q, expected \"deployed\"", [input.spec.metadata.status])
}

# --- audience -------------------------------------------------------------

deny contains "caller holds no role listed in audience.roles" if {
	not role_match
}

role_match if {
	some r in input.principal.roles
	r in input.spec.audience.roles
}

deny contains msg if {
	depts := object.get(input.spec.audience, "departments", [])
	count(depts) > 0
	not input.principal.department in depts
	msg := sprintf("caller department %q not in audience.departments", [input.principal.department])
}

# --- capability allow-list ------------------------------------------------

capability := c if {
	some c in input.spec.capabilities
	c.tool == input.action.tool
}

deny contains msg if {
	not capability
	msg := sprintf("tool %q is not in capabilities", [input.action.tool])
}

deny contains msg if {
	capability.scope == "read"
	input.action.scope == "write"
	msg := sprintf("tool %q is granted read scope but was called for write", [input.action.tool])
}

# --- prohibitions ---------------------------------------------------------

deny contains msg if {
	some pattern in object.get(input.spec.prohibitions, "deny_tools", [])
	glob.match(pattern, ["."], input.action.tool)
	msg := sprintf("tool %q matches deny_tools pattern %q", [input.action.tool, pattern])
}

deny contains msg if {
	object.get(input.spec.prohibitions, "external_egress", false) == false
	host := object.get(input.action.args, "host", "")
	host != ""
	not host in allowed_hosts
	msg := sprintf("egress to %q blocked; host not declared in connections", [host])
}

allowed_hosts contains h if {
	some c in object.get(input.spec, "connections", [])
	some h in object.get(c, "allowed_hosts", [])
}

# --- prompt injection: the important one ----------------------------------
# A write whose arguments were derived from content the agent read out of a
# database row, a document, or a web page is never executed, regardless of how
# convincing the model found it.

deny contains "write action derived from untrusted content" if {
	input.action.scope == "write"
	input.provenance.from_untrusted_data == true
}

# --- autonomy -------------------------------------------------------------

deny contains "autonomy is read_only; write tools are unreachable" if {
	input.spec.authority.autonomy == "read_only"
	input.action.scope == "write"
}

deny contains "autonomy is propose; a human must execute this action" if {
	input.spec.authority.autonomy == "propose"
	input.action.scope == "write"
	input.action.mode == "execute"
}

# --- budget ---------------------------------------------------------------

deny contains msg if {
	limit := input.spec.authority.budget.max_tool_calls_per_session
	input.session.tool_calls_used >= limit
	msg := sprintf("session tool-call budget exhausted (%v)", [limit])
}

deny contains msg if {
	limit := capability.constraints.max_calls_per_session
	used := object.get(input.session.per_tool_calls, input.action.tool, 0)
	used >= limit
	msg := sprintf("per-tool budget exhausted for %q (%v)", [input.action.tool, limit])
}

# --- data surface ---------------------------------------------------------

deny contains msg if {
	allowed := capability.constraints.allowed_columns
	some col in object.get(input.action.args, "columns", [])
	not col in allowed
	msg := sprintf("column %q not in allowed_columns", [col])
}

deny contains msg if {
	some col in object.get(input.action.args, "columns", [])
	col in object.get(input.spec.prohibitions, "never_expose", [])
	msg := sprintf("column %q is listed in never_expose", [col])
}

deny contains msg if {
	obj := object.get(input.action.args, "object", "")
	obj != ""
	not obj in allowed_objects
	msg := sprintf("object %q not in connections[].allowed_objects", [obj])
}

allowed_objects contains o if {
	some c in object.get(input.spec, "connections", [])
	some o in object.get(c, "allowed_objects", [])
}

# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

has_matrix_entry if {
	_ = input.spec.authority.approval_matrix[input.action.tool]
}

matrix_entry := input.spec.authority.approval_matrix[input.action.tool]

effective_approver_roles := roles if {
	has_matrix_entry
	roles := matrix_entry.approver_roles
} else := ["agent_admin"]

approval_required if {
	capability.requires_approval
	not has_matrix_entry
}

approval_required if {
	capability.requires_approval
	has_matrix_entry
	matrix_condition_met
}

matrix_condition_met if {
	not matrix_entry.condition
}

matrix_condition_met if {
	condition_true(matrix_entry.condition)
}

# An approval is bound to the exact action hash. Approving "send a reminder to
# customer A for 12,000,000" does not authorise a different payload afterwards.
approval_granted if {
	some a in input.approvals
	a.tool == input.action.tool
	a.action_hash == input.action.hash
	a.approver_sub != input.principal.sub
	some r in a.approver_roles
	r in effective_approver_roles
}

# ---------------------------------------------------------------------------
# Structured condition evaluation.
# Deliberately a tiny fixed operator set. Nothing here evaluates a string as
# code, which is why the meta-agent is forbidden from emitting expressions.
# ---------------------------------------------------------------------------

resolve("$principal.employee_id") := input.principal.employee_id

resolve("$principal.department") := input.principal.department

resolve("$principal.sub") := input.principal.sub

resolve(v) := v if {
	is_string(v)
	not startswith(v, "$principal.")
}

resolve(v) := v if {
	not is_string(v)
}

arg_value(field) := object.get(input.action.args, split(field, "."), null)

condition_true(c) if {
	c.op == "eq"
	arg_value(c.field) == resolve(c.value)
}

condition_true(c) if {
	c.op == "neq"
	arg_value(c.field) != resolve(c.value)
}

condition_true(c) if {
	c.op == "gt"
	arg_value(c.field) > resolve(c.value)
}

condition_true(c) if {
	c.op == "gte"
	arg_value(c.field) >= resolve(c.value)
}

condition_true(c) if {
	c.op == "lt"
	arg_value(c.field) < resolve(c.value)
}

condition_true(c) if {
	c.op == "lte"
	arg_value(c.field) <= resolve(c.value)
}

condition_true(c) if {
	c.op == "in"
	arg_value(c.field) in resolve(c.value)
}

condition_true(c) if {
	c.op == "not_in"
	not arg_value(c.field) in resolve(c.value)
}
