"""The tool registry: a **closed** catalogue, declared in code.

Two rules this module exists to enforce:

1. **Risk level is assigned by whoever writes the code, never by an LLM.**
   The meta-agent may only *pick* a tool name from `names()`. `ToolSpec` is
   frozen, so nothing downstream — least of all the content of an `AgentSpec` —
   can talk a tool into a lower risk level.
2. **A tool with no simulated implementation cannot be registered.** `simulate`
   is a required field, so the sandbox can always swap in a safe stand-in. That
   is a constraint on the developer, not on the LLM.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolRisk(StrEnum):
    """How much scrutiny a tool deserves on the approval screen."""

    LOW = "low"
    HIGH = "high"


class ToolSpec(BaseModel):
    """One entry in the catalogue. Frozen: risk cannot be edited after the fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1, description="Hiển thị cho người tạo")
    args_schema: type[BaseModel]
    risk: ToolRisk
    has_side_effects: bool = Field(..., description="True = tool tác động ra thế giới bên ngoài")
    requires_declared_domains: bool = Field(
        default=False,
        description="True = version phải khai `allowed_domains` mới được dùng tool này",
    )
    run: Callable[..., Any] = Field(..., description="Implementation thật")
    simulate: Callable[..., Any] = Field(..., description="Implementation mô phỏng, bắt buộc — sandbox dựa vào nó")

    @model_validator(mode="after")
    def _simulation_must_not_be_the_real_implementation(self) -> Self:
        """A required field alone still lets `simulate=run` through.

        That declaration passes every "has a simulation" check while making a
        sandbox run call the real thing — the one failure mode this field
        exists to prevent, and the one that would be silent. So it is refused
        at registration, where a developer sees it, rather than discovered by a
        customer receiving a real email from an unapproved agent (#9).
        """
        if self.simulate is self.run:
            raise ValueError(f"tool {self.name!r}: bản mô phỏng không được chính là implementation thật")
        return self

    def implementation(self, *, sandbox: bool) -> Callable[..., Any]:
        """The implementation to call for this run.

        In sandbox mode a side-effecting tool runs its simulated implementation;
        read-only tools run for real, so what the creator sees is trustworthy.
        """
        if sandbox and self.has_side_effects:
            return self.simulate
        return self.run


class ToolRegistry:
    """A closed catalogue. Nothing outside this process can add to it."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> ToolSpec:
        """Add a tool to the catalogue. A name may only be registered once."""
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} đã được đăng ký")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> ToolSpec:
        """The tool registered under `name`, or a KeyError naming what is allowed."""
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"tool {name!r} không có trong tool registry; được phép: {sorted(self._tools)}") from None

    def names(self) -> tuple[str, ...]:
        """Every registered tool name — the only names an `AgentSpec` may use."""
        return tuple(self._tools)

    def all(self) -> tuple[ToolSpec, ...]:
        """Every registered tool, for the catalogue screen and for validation."""
        return tuple(self._tools.values())

    def domain_gated_names(self) -> tuple[str, ...]:
        """Tools whose use obliges the version to declare `allowed_domains`."""
        return tuple(tool.name for tool in self._tools.values() if tool.requires_declared_domains)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


#: The one registry the platform runs on. Populated by `src.agents.tools.builtin`.
TOOL_REGISTRY = ToolRegistry()
