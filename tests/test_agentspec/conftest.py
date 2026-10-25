"""A valid UC-1 `AgentSpec` payload, and the reference spec shipped with the docs.

Every test in this package starts from `spec_payload()` and breaks exactly one
thing, so a failure names the rule that fired rather than the fixture that drifted.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

REFERENCE_SPEC_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "usecase_spec" / "uc1-ar-assistant" / "agentspec.yaml"
)


def spec_payload(**overrides: Any) -> dict[str, Any]:
    """The smallest spec that satisfies every required field of the schema."""
    payload: dict[str, Any] = {
        "apiVersion": "agentbuilder/v1",
        "kind": "AgentSpec",
        "metadata": {
            "id": "agt_ar_assistant",
            "name": "Trợ lý công nợ",
            "version": 1,
            "status": "draft",
        },
        "audience": {
            "roles": ["sales_rep"],
            "data_scope": "own_records",
        },
        "capabilities": [
            {"tool": "erp.query_receivables", "scope": "read"},
        ],
        "prohibitions": {"external_egress": False},
        "authority": {
            "autonomy": "read_only",
            "budget": {"max_tool_calls_per_session": 40},
        },
        "guardrails": {
            "input": ["prompt_injection_detector", "pii_redactor", "topic_classifier"],
            "output": ["pii_masker", "citation_required", "confidence_router"],
            "fallback": "escalate_to_human",
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def reference_spec() -> dict[str, Any]:
    """`docs/usecase_spec/uc1-ar-assistant/agentspec.yaml` — the spec of record for UC-1."""
    return yaml.safe_load(REFERENCE_SPEC_PATH.read_text(encoding="utf-8"))
