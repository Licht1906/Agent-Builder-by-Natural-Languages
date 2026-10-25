"""UC1-05 (#119) — the meta-agent's `draft_spec` node: a natural-language
description in, a validated `AgentSpec` draft plus `clarifications` out (FR-10,
FR-11), retried against UC1-03's gate when the model's draft does not pass it
(§11, rủi ro R1).

Reuses `src.services.model_document.write_document` — the exact retry loop
`src.interview.generation.write_spec` (V2's meta-agent) already runs the same
call through, per spec.md §20's testing decision ("Seam thứ hai: model factory
... kế thừa quyết định ở V1 và V2"). What UC-1 adds is the prompt
(`platform/meta-agent-system-prompt.md`, rendered with Jinja) and the parser
(`validate_drafted_spec` — UC1-03's gate, not V2's `AgentSpec.model_validate`).

Not a `StateGraph` of its own: spec.md §20 tests this platform at the HTTP/
service seam, never the shape of a LangGraph graph internally, and this function
*is* what a one-node graph would wrap — a caller compiling UC-1's full graph
(UC1-10) can add it as a node the way any callable becomes one.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jinja2 import Template

from src.agentspec.guardrails import BASELINE_INPUT, BASELINE_OUTPUT
from src.agentspec.tool_catalog import catalog_entries
from src.agentspec.validation import DraftedSpec, SpecValidationError, validate_drafted_spec
from src.db.models import PlatformCallPurpose, UserRole
from src.evals.model_json import json_object
from src.services.llm import ChatModelFactory
from src.services.model_document import DocumentRejectedError, write_document

_DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs" / "usecase_spec" / "platform"
_PROMPT_TEMPLATE_PATH = _DOCS_ROOT / "meta-agent-system-prompt.md"
_SCHEMA_PATH = _DOCS_ROOT / "agentspec.schema.json"

#: What the creator reads when every attempt failed the gate (AC: "báo lỗi dễ
#: hiểu, không crash"). The validation detail travels on `DraftSpecError.reason`.
_EXHAUSTED = (
    "Chưa soạn được cấu hình agent hợp lệ từ mô tả này sau {attempts} lần thử. "
    "Bạn thử mô tả lại rõ hơn — agent cần làm gì, ai được dùng, và có hành động "
    "nào gửi ra ngoài tổ chức hay không — rồi thử lại."
)

_NO_CONNECTIONS_YET = "(chưa có kết nối nào được cấu hình)"


class DraftSpecError(Exception):
    """The meta-agent could not produce a spec that passes the gate within the
    attempt budget. `args[0]` is user-facing; `reason` is the last validation
    error, for logs and for whoever is debugging the prompt.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _load_template_source() -> str:
    """The prompt template body — the file minus the developer-facing header above its `---`.

    Lines 1-6 of `meta-agent-system-prompt.md` document how the file is used;
    they are not part of the prompt itself. `partition` falls back to the whole
    file if the separator is ever removed, so a header edit degrades rather than
    breaks rendering.
    """
    text = _PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    _, separator, body = text.partition("\n---\n")
    return body if separator else text


def _tool_catalog_block() -> str:
    return "\n".join(f"- {entry.name} ({entry.scope.value}) — {entry.description}" for entry in catalog_entries())


def _role_catalog_block() -> str:
    return "\n".join(f"- {role.value}" for role in UserRole)


def _baseline_guardrails_block() -> str:
    return "\n".join(
        (
            f"input: {', '.join(guardrail.value for guardrail in BASELINE_INPUT)}",
            f"output: {', '.join(guardrail.value for guardrail in BASELINE_OUTPUT)}",
        )
    )


def render_system_prompt(
    *,
    principal: Mapping[str, Any],
    connection_catalog: str = _NO_CONNECTIONS_YET,
    role_catalog: str | None = None,
) -> str:
    """Render `platform/meta-agent-system-prompt.md` with the catalogs the drafter needs.

    `agentspec_schema` is `agentspec.schema.json` verbatim — the "published
    contract" `src.agentspec.spec`'s own docstring names — handed to the model
    exactly as written rather than re-derived from the Pydantic model, so the two
    can only drift by being edited, never by a serialisation difference.
    """
    template = Template(_load_template_source())
    return template.render(
        tool_catalog=_tool_catalog_block(),
        connection_catalog=connection_catalog,
        role_catalog=role_catalog or _role_catalog_block(),
        baseline_guardrails=_baseline_guardrails_block(),
        principal=json.dumps(dict(principal), ensure_ascii=False),
        agentspec_schema=_SCHEMA_PATH.read_text(encoding="utf-8"),
    )


def _build_prompt(
    description: str,
    *,
    principal: Mapping[str, Any],
    connection_catalog: str,
    role_catalog: str | None,
) -> str:
    system_prompt = render_system_prompt(
        principal=principal, connection_catalog=connection_catalog, role_catalog=role_catalog
    )
    return f"{system_prompt}\n\n## Yêu cầu của người dùng\n\n{description.strip()}"


def _parse(reply: str) -> DraftedSpec:
    """One reply as a `DraftedSpec`, or a retryable reason it is not one yet.

    `json_object` is what copes with a model that wrapped its answer in ```
    fences despite Hard rule #1 — one of the prompt's own "Failure modes to
    avoid". `SpecValidationError` is re-raised as `DocumentRejectedError`
    because it is exactly that error's own case: a well-formed JSON document
    failing a rule above the JSON shape (UC1-03's gate), and the loop only
    retries what is in `RETRYABLE`.
    """
    document = json_object(reply)
    try:
        return validate_drafted_spec(document)
    except SpecValidationError as exc:
        raise DocumentRejectedError(str(exc)) from exc


async def draft_spec(
    description: str,
    *,
    model_factory: ChatModelFactory,
    principal: Mapping[str, Any],
    connection_catalog: str = _NO_CONNECTIONS_YET,
    role_catalog: str | None = None,
) -> DraftedSpec:
    """LangGraph node `draft_spec` (§6.2's `metaagent` service).

    Retries on the same budget every other document this platform writes gets
    (`MAX_ATTEMPTS = 3`, `src.services.model_document`) — the model's own
    validation error is fed back on each attempt, so a rule it broke is a rule
    it is shown, not just told to try again blind. Runs on `meta_agent_model()`
    only, never an admin-configured allowlist entry (ADR-0010 §1): which model
    drafts a spec for this platform is not a setting.
    """
    prompt = _build_prompt(
        description, principal=principal, connection_catalog=connection_catalog, role_catalog=role_catalog
    )
    return await write_document(
        build_prompt=lambda: prompt,
        parse=_parse,
        model_factory=model_factory,
        exhausted=_EXHAUSTED,
        failure=lambda message, reason: DraftSpecError(message, reason=reason),
        purpose=PlatformCallPurpose.SOLUTION_SPEC,
    )
