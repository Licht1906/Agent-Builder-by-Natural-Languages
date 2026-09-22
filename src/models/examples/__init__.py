"""Handwritten `AgentSpec` documents, shipped as the shared fixture.

The tracer bullet runs one of these through the runtime before any meta-agent
exists. That is what proves the contract is *executable* rather than merely
well-formed, so these files are production artifacts, not test data.
"""

import json
from pathlib import Path

from src.models.agent_spec import AgentSpec

_EXAMPLES_DIR = Path(__file__).parent


def list_example_specs() -> tuple[str, ...]:
    """Names of every shipped example, without the `.json` suffix."""
    return tuple(sorted(path.stem for path in _EXAMPLES_DIR.glob("*.json")))


def load_example_spec(name: str) -> AgentSpec:
    """Load and validate a shipped example by name."""
    path = _EXAMPLES_DIR / f"{name}.json"
    if not path.is_file():
        raise KeyError(f"không có example spec nào tên {name!r}; có: {list(list_example_specs())}")
    return AgentSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))
