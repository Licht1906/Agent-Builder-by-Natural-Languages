"""UC-1's `AgentSpec` and the single validation layer every writer of one passes through.

See ADR-0047. The schema of record is `docs/usecase_spec/platform/agentspec.schema.json`.
"""

from src.agentspec.guardrails import BASELINE_INPUT, BASELINE_OUTPUT
from src.agentspec.lifecycle import (
    InvalidTransitionError,
    SuspensionReason,
    approve,
    deploy,
    new_draft_version,
    rollback,
    submit_for_approval,
    suspend,
)
from src.agentspec.spec import AgentSpec, SpecStatus
from src.agentspec.tool_catalog import catalog_entries, is_known_tool, known_tool_names
from src.agentspec.validation import (
    Clarification,
    DraftedSpec,
    Problem,
    SpecValidationError,
    validate_drafted_spec,
    validate_spec,
)

__all__ = [
    "BASELINE_INPUT",
    "BASELINE_OUTPUT",
    "AgentSpec",
    "Clarification",
    "DraftedSpec",
    "InvalidTransitionError",
    "Problem",
    "SpecStatus",
    "SpecValidationError",
    "SuspensionReason",
    "approve",
    "catalog_entries",
    "deploy",
    "is_known_tool",
    "known_tool_names",
    "new_draft_version",
    "rollback",
    "submit_for_approval",
    "suspend",
    "validate_drafted_spec",
    "validate_spec",
]
