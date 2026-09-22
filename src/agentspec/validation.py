"""The one gate. Meta-agent, compiler and API all reach a stored spec through here.

Three worries ride on a single call — right shape, no leaked credential, baseline
guardrails still present — because three separately-callable checks is three
chances for a caller to run two of them. UC1-03 exists to make "validated" mean
one thing.

Ordering is not cosmetic. The secret scan runs on the *raw* input, before Pydantic
sees it, because a Pydantic `ValidationError` quotes the value that failed: a DSN
in `credential_ref` fails the `vault://` pattern, and the resulting message — bound
for a log line and an HTTP response — would carry the credential out with it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agentspec.guardrails import merge_baseline, missing_baseline
from src.agentspec.secrets import Finding, scan_for_secrets
from src.agentspec.spec import AgentSpec, SpecStatus
from src.agentspec.tool_catalog import is_known_tool

_PYDANTIC_CODES = {
    "extra_forbidden": "unknown_field",
    "missing": "missing_field",
}


@dataclass(frozen=True)
class Problem:
    """One reason a spec was refused, addressed to whoever has to fix it.

    `path` points into the spec the caller sent, so the API can attach the message
    to the right field on the config form rather than to the form as a whole.
    """

    code: str
    path: str
    message: str


class Clarification(BaseModel):
    """A question the meta-agent had to guess the answer to.

    Not part of `AgentSpec` — it is a question *about* a spec, and it belongs beside
    the field it asks about on the config form rather than buried in a log (FR-14).
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(..., min_length=1, description="Trường trong spec mà câu hỏi này nói về")
    question: str = Field(..., min_length=1)
    why: str = Field(..., min_length=1, description="Vì sao câu trả lời làm thay đổi spec")


@dataclass(frozen=True)
class DraftedSpec:
    """What comes back from the meta-agent once the platform has had its say."""

    spec: AgentSpec
    clarifications: list[Clarification] = field(default_factory=list)


class SpecValidationError(Exception):
    """A spec did not pass the gate. Carries every problem found at the failing stage.

    Stages run in order and the first one with problems raises, so this never
    mixes "there is a credential in here" with "this field is misspelt" — the
    first is an incident, the second is a typo, and a caller that sees them in one
    list will treat both as a form error.
    """

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        super().__init__("; ".join(f"{problem.path}: {problem.message}" for problem in problems))


def validate_spec(raw: Mapping[str, Any]) -> AgentSpec:
    """Accept a spec, or refuse it with reasons. The strict reading.

    Used by `PATCH /agents/{id}/versions/{v}` and by the compiler — both handle
    a spec a human has already seen, so a missing baseline guardrail here is a
    removal, and removals are refused (FR-13). The meta-agent's own output goes
    through `validate_drafted_spec` instead, which unions the baseline in first.
    """
    _refuse_secrets(raw)
    spec = _parse(raw)
    _refuse_unknown_tools(spec)
    _refuse_baseline_removal(spec)
    return spec


def validate_drafted_spec(raw: Mapping[str, Any]) -> DraftedSpec:
    """Accept what the meta-agent just produced, or refuse it with reasons.

    Differs from `validate_spec` in exactly two places, both of them about the
    model's output not being a person's edit:

    - `clarifications` is lifted out. The prompt asks the model for them and the
      schema forbids unknown fields; the platform strips them before validation
      and shows them beside the fields they ask about (UC1-06).
    - the baseline is unioned in rather than demanded. A guardrail the model left
      out was never a decision anybody made.

    Everything else — the secret scan, the shape, and the baseline check itself —
    happens by delegating to `validate_spec`, so the draft door is a shortcut past
    one rule and not past the gate.
    """
    payload = dict(raw)
    _refuse_secrets(payload)

    clarifications = _clarifications_in(payload.pop("clarifications", []))
    if isinstance(payload.get("guardrails"), dict):
        payload["guardrails"] = merge_baseline(payload["guardrails"])
    if isinstance(payload.get("metadata"), dict):
        # Hard rule #6 of the meta-agent prompt: status belongs to the platform.
        # A model that returns "approved" is not granting itself an approval.
        payload["metadata"] = {**payload["metadata"], "status": SpecStatus.DRAFT.value}

    return DraftedSpec(spec=validate_spec(payload), clarifications=clarifications)


def _refuse_unknown_tools(spec: AgentSpec) -> None:
    """FR-15, T9 — every `capabilities[].tool` must name a tool the platform actually
    registered. Runs here rather than only in the compiler, so a spec inventing a
    tool is refused at the same gate as a spec with a missing field, not by a
    separate check a caller could forget to call (UC1-07 AC).
    """
    problems = [
        Problem(
            code="unknown_tool",
            path=f"capabilities[{index}].tool",
            message=(
                f"tool {capability.tool!r} không có trong tool catalog — "
                "agent mẹ không được phát minh tool (FR-15)"
            ),
        )
        for index, capability in enumerate(spec.capabilities)
        if not is_known_tool(capability.tool)
    ]
    if problems:
        raise SpecValidationError(problems)


def _refuse_baseline_removal(spec: AgentSpec) -> None:
    dropped_input, dropped_output = missing_baseline(spec.guardrails.input, spec.guardrails.output)
    problems = [
        Problem(
            code="baseline_guardrail_removed",
            path=f"guardrails.{listing}",
            message=(
                f"không gỡ được guardrail thuộc baseline bắt buộc: {sorted(guardrail.value for guardrail in dropped)}"
                " — thêm được, gỡ thì không (FR-13)"
            ),
        )
        for listing, dropped in (("input", dropped_input), ("output", dropped_output))
        if dropped
    ]
    if problems:
        raise SpecValidationError(problems)


def _clarifications_in(raw: Any) -> list[Clarification]:
    try:
        return [Clarification.model_validate(entry) for entry in raw]
    except (ValidationError, TypeError) as error:
        raise SpecValidationError(
            [
                Problem(
                    code="invalid_clarification",
                    path="clarifications",
                    message=f"agent mẹ trả về `clarifications` không đúng dạng {{field, question, why}}: {error}",
                )
            ]
        ) from None


def _refuse_secrets(raw: Mapping[str, Any]) -> None:
    findings = scan_for_secrets(raw)
    if findings:
        raise SpecValidationError([_problem_from_finding(finding) for finding in findings])


def _problem_from_finding(finding: Finding) -> Problem:
    return Problem(
        code="secret_shape",
        path=finding.path,
        message=(
            f"giá trị ở đây có hình dạng của một credential ({finding.rule}); "
            "spec chỉ được chứa con trỏ dạng vault://…, không bao giờ chứa secret"
        ),
    )


def _parse(raw: Mapping[str, Any]) -> AgentSpec:
    try:
        return AgentSpec.model_validate(raw)
    except ValidationError as error:
        raise SpecValidationError([_problem_from(detail) for detail in error.errors()]) from None


def _problem_from(detail: Any) -> Problem:
    return Problem(
        code=_PYDANTIC_CODES.get(detail["type"], "invalid_value"),
        path=_path_of(detail["loc"]),
        message=detail["msg"],
    )


def _path_of(loc: tuple[Any, ...]) -> str:
    """Pydantic's `loc` tuple as a path a form field can be looked up by."""
    rendered = ""
    for part in loc:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += f".{part}" if rendered else str(part)
    return rendered
