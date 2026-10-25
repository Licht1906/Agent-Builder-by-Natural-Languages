"""The `metaagent` service (spec.md §6.2): turns a natural-language description
into a draft `AgentSpec` plus `clarifications` (UC1-05, FR-10/FR-11)."""

from src.metaagent.draft_spec import DraftSpecError, draft_spec, render_system_prompt

__all__ = ["DraftSpecError", "draft_spec", "render_system_prompt"]
