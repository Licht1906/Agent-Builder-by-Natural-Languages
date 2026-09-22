"""`AgentSpec` — the single contract between "sinh agent" and "chạy & quản trị".

An agent built by a user is **data, never code**: no code generation, no `exec`,
ever. Every safety control in the platform hangs off that decision, so this module
is where the controls are enforced — in validators, not in prompts.
"""

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from src.agents.tools import TOOL_REGISTRY
from src.models.model_catalog import ASSIGNABLE_MODELS, DEFAULT_MODEL_ALLOWLIST, ModelTier, tier_of


class SystemLimits(BaseModel):
    """Hard ceilings owned by the platform. A spec can go under them, never over.

    Frozen: these are the outer boundary the runtime enforces, so no spec — and no
    request handler — gets to relax them at runtime.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_assignment=True)

    max_steps: int
    max_iterations_per_step: int
    max_total_tokens: int
    max_wall_seconds: int


#: `max_steps` is 24 and was 12, because the thing it counts changed under it.
#:
#: Twelve was written for a V1 `AgentSpec`, where a step is a piece of *work* and
#: nothing else: the list is linear, it ends when it runs out, and every entry
#: earns its place. `src.workflow.composition` then reused the same number for a
#: V2 graph, on the argument that both answer "how much work may one run be" —
#: and that argument stopped being true the moment a graph got branches. A V2
#: graph spends nodes on **shape**: an `end` on every branch, an `if_else` to
#: split, a `collect` in front of each action that needs parameters, a
#: `human_approval` in front of each irreversible one. None of those is work; all
#: of them are nodes. Counting the built-in templates: `action_with_approval` is
#: 7 nodes for 2 pieces of work. So twelve V2 nodes bought a creator roughly four
#: working steps, where twelve V1 steps bought twelve — the same ceiling
#: enforcing a third of the agent, purely because of which document they routed
#: through.
#:
#: Twenty-four rather than something larger, for two reasons that both bind. A
#: graph with three branches, an approval and a loop lands around twenty nodes,
#: so this clears a genuinely complex agent with room over; past that the drawing
#: stops being something a person can read on a canvas, which is the real limit
#: and not one a number can raise. And the runtime is what actually pays: the
#: worst real run this platform has ever recorded is 2,790 tokens, and
#: `DEFAULT_WORKFLOW_BUDGET` stops a run at 20,000 — doubling the node ceiling
#: doubles the worst case and leaves that brake with several times the headroom
#: it needs. Raising this further without raising the budget would mean handing
#: creators a ceiling the runtime cuts them off before they can reach.
SYSTEM_LIMITS = SystemLimits(
    max_steps=24,
    max_iterations_per_step=10,
    max_total_tokens=200_000,
    max_wall_seconds=300,
)


def _model_allowlist_from(info: ValidationInfo) -> frozenset[str]:
    """The model allowlist in force for this validation.

    Admins configure the allowlist at runtime, so it arrives as validation
    context: `AgentSpec.model_validate(data, context={"model_allowlist": [...]})`.
    Without context we fall back to everything assignable. The allowlist can only
    *narrow* `ASSIGNABLE_MODELS` — a model nobody vetted, and the one reserved for
    the meta-agent, are never allowed however they got into the context.
    """
    context = info.context or {}
    configured = context.get("model_allowlist")
    candidates = DEFAULT_MODEL_ALLOWLIST if configured is None else configured
    return frozenset(name for name in candidates if name in ASSIGNABLE_MODELS)


class InterviewSlots(BaseModel):
    """What the interview collected. Feeds eval (`success_criteria`) and guardrails."""

    model_config = ConfigDict(extra="forbid")

    audience: str = Field(..., min_length=1, description="Ai dùng agent này")
    inputs: str = Field(..., min_length=1, description="Dữ liệu đầu vào từ đâu")
    capabilities: list[str] = Field(default_factory=list, description="Cần làm được những việc gì")
    success_criteria: list[str] = Field(default_factory=list, description="Thế nào là trả lời đúng → nuôi eval")
    prohibitions: list[str] = Field(default_factory=list, description="Tuyệt đối không được làm → nuôi guardrails")


class Step(BaseModel):
    """One node of the pipeline. A step with tools becomes a bounded ReAct loop."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1, description="Nhãn hiển thị trên sơ đồ")
    prompt: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1, description="Phải nằm trong allowlist")
    tools: list[str] = Field(default_factory=list, description="Phải nằm trong tool registry")
    max_iterations: int = Field(..., ge=1, description="Trần vòng lặp ReAct của bước")
    next: str | None = Field(default=None, description="None = bước kế tiếp trong danh sách")


class Budget(BaseModel):
    """Per-run ceilings. Enforced by the runtime, not advisory."""

    model_config = ConfigDict(extra="forbid")

    max_total_tokens: int = Field(..., ge=1)
    max_wall_seconds: int = Field(..., ge=1)


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    slots: InterviewSlots
    steps: list[Step] = Field(..., min_length=1)
    guardrails: list[str] = Field(default_factory=list)
    budget: Budget
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Domain tool cửa-thoát được phép gọi; admin duyệt theo từng version",
    )

    @field_validator("allowed_domains")
    @classmethod
    def _domains_must_be_bare_hostnames(cls, domains: list[str]) -> list[str]:
        """A domain is matched by exact hostname, so it must *be* a hostname.

        Scheme, path, wildcard or whitespace here would silently never match and
        read on the approval screen as a permission that was granted. Reject them
        instead, and normalise case so matching is predictable.
        """
        cleaned: list[str] = []
        for domain in domains:
            candidate = domain.strip().lower()
            if not candidate:
                raise ValueError("`allowed_domains` chứa một mục rỗng")
            if any(character in candidate for character in "/:*? "):
                raise ValueError(
                    f"domain {domain!r} phải là hostname trần (ví dụ 'api.erp.internal'), "
                    "không kèm scheme, cổng, đường dẫn hay ký tự đại diện"
                )
            cleaned.append(candidate)
        return cleaned

    @model_validator(mode="after")
    def _step_ids_must_be_unique(self) -> "AgentSpec":
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"id của bước bị trùng: {step.id!r}")
            seen.add(step.id)
        return self

    @model_validator(mode="after")
    def _models_must_be_allowlisted(self, info: ValidationInfo) -> "AgentSpec":
        allowlist = _model_allowlist_from(info)
        for step in self.steps:
            if step.model not in allowlist:
                raise ValueError(
                    f"bước {step.id!r} dùng model {step.model!r} không nằm trong "
                    f"allowlist model; được phép: {sorted(allowlist)}"
                )
        return self

    @model_validator(mode="after")
    def _must_stay_within_system_limits(self) -> "AgentSpec":
        if len(self.steps) > SYSTEM_LIMITS.max_steps:
            raise ValueError(f"số bước ({len(self.steps)}) vượt trần hệ thống ({SYSTEM_LIMITS.max_steps})")
        for step in self.steps:
            if step.max_iterations > SYSTEM_LIMITS.max_iterations_per_step:
                raise ValueError(
                    f"bước {step.id!r} vượt trần vòng lặp của hệ thống: "
                    f"{step.max_iterations} > {SYSTEM_LIMITS.max_iterations_per_step}"
                )
        if self.budget.max_total_tokens > SYSTEM_LIMITS.max_total_tokens:
            raise ValueError(
                f"vượt trần ngân sách token của hệ thống: "
                f"{self.budget.max_total_tokens} > {SYSTEM_LIMITS.max_total_tokens}"
            )
        if self.budget.max_wall_seconds > SYSTEM_LIMITS.max_wall_seconds:
            raise ValueError(
                f"vượt trần ngân sách thời gian của hệ thống: "
                f"{self.budget.max_wall_seconds} > {SYSTEM_LIMITS.max_wall_seconds}"
            )
        return self

    @model_validator(mode="after")
    def _tools_must_exist_in_the_registry(self) -> "AgentSpec":
        for step in self.steps:
            for name in step.tools:
                if name not in TOOL_REGISTRY:
                    raise ValueError(
                        f"bước {step.id!r} xin tool {name!r} không có trong tool "
                        f"registry; được phép: {sorted(TOOL_REGISTRY.names())}"
                    )
        return self

    @model_validator(mode="after")
    def _cheap_models_may_not_be_given_tools(self) -> "AgentSpec":
        """The weakest point of per-step model selection, so it is a hard rule.

        Cheap models call tools unreliably, and the resulting failures are very
        hard to trace back to the model choice.
        """
        for step in self.steps:
            if not step.tools:
                continue
            if tier_of(step.model) is ModelTier.CHEAP:
                raise ValueError(
                    f"bước {step.id!r} dùng model rẻ {step.model!r} cho một bước có "
                    f"tool {step.tools} — model rẻ gọi tool không đáng tin"
                )
        return self

    @model_validator(mode="after")
    def _domain_gated_tools_require_declared_domains(self) -> "AgentSpec":
        """Which tools are domain-gated is the registry's business, not a name here."""
        if self.allowed_domains:
            return self
        gated = TOOL_REGISTRY.domain_gated_names()
        offenders = {
            step.id: sorted(set(step.tools) & set(gated)) for step in self.steps if set(step.tools) & set(gated)
        }
        if offenders:
            raise ValueError(f"bước {offenders} cần khai `allowed_domains` — mỗi lần dùng phải khai domain")
        return self

    def next_step_id(self, step_id: str) -> str | None:
        """Resolve the successor of a step.

        `Step.next is None` means "the next step in the list" — the MVP runs
        linearly, and the field exists so that adding branching later is an
        extension rather than a breaking change to this contract.
        """
        for index, step in enumerate(self.steps):
            if step.id != step_id:
                continue
            if step.next is not None:
                return step.next
            following = self.steps[index + 1 :]
            return following[0].id if following else None
        raise KeyError(f"không có bước nào mang id {step_id!r}")
