"""Tool registry package. Importing it populates the closed catalogue."""

from src.agents.tools import builtin as _builtin  # noqa: F401  (registers the tools)
from src.agents.tools.registry import TOOL_REGISTRY, ToolRegistry, ToolRisk, ToolSpec

__all__ = [
    "TOOL_REGISTRY",
    "ToolRegistry",
    "ToolRisk",
    "ToolSpec",
]
