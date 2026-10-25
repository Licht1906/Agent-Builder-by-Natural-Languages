"""The demo agent this ticket proves is executable — see ADR-0003.

Mirrors `src.auth.seed`: one definition, shared by the Alembic migration that
seeds it and any test that needs the same payload, so a freshly migrated
database and a test's fixtures never disagree about what this agent runs.
"""

from src.models.examples import load_example_spec

#: `read_file` and `calculate` need no external infrastructure to run for
#: real — the two MVP tools this ticket proves are executable end-to-end.
TRACER_BULLET_EXAMPLE = "expense_helper"
TRACER_BULLET_AGENT_NAME = "Trợ lý tính chi phí (demo)"


def tracer_bullet_spec_payload() -> dict[str, object]:
    """The hand-written `AgentSpec` this ticket proves is executable, as a plain dict."""
    return load_example_spec(TRACER_BULLET_EXAMPLE).model_dump(mode="json")
