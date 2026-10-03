"""Every run is traced, the trace resolves the run, and the trace can fail freely (#11).

Three claims, in order of how much they matter.

The trace has to exist **for every run**, trial and production, or the one an
investigator reaches for after an incident is the one that was never recorded.

It has to **resolve** — step by step, tool call by tool call. A trace saying only
"this run happened, it cost $0.02" answers nothing about the run that misbehaved.

And it must never be load-bearing. A run whose answer depends on an observability
backend being up has traded a real dependency for a reporting nicety, which is
strictly worse than not tracing at all.

The seam is `get_tracer`, replaced the way `get_chat_model_factory` is. What the
tests assert against is `EventTrace` — the production class that also backs the
Langfuse export — so a shape that passed here and failed in production would have
to be a shape neither of them builds.
"""

import pytest
from langchain_core.messages import AIMessage

from src.models.agent_spec import AgentSpec
from tests.conftest import (
    agent_spec,
    chat_with_agent,
)

pytestmark = pytest.mark.asyncio


def _spec(*, tools: list[str] | None = None, steps: int = 2) -> AgentSpec:
    """A linear spec whose steps have recognisable ids and titles."""
    return agent_spec(
        steps=[
            {
                "id": f"s{index}",
                "title": f"Bước {index}",
                "prompt": "p",
                "model": "gpt-4o",
                "tools": tools or [],
                "max_iterations": 3,
                "next": None,
            }
            for index in range(1, steps + 1)
        ]
    )


# --------------------------------------------------------------------------- #
# Every run appears
# --------------------------------------------------------------------------- #


async def test_a_production_run_is_traced(
    client, creator_token, seeded_creator, deployed_agent_of, scripted_chat_model_factory, recorded_runs
) -> None:
    """The runs nobody is watching are the ones a trace is for."""
    agent = deployed_agent_of(seeded_creator, _spec())
    scripted_chat_model_factory([[AIMessage(content="xong 1")], [AIMessage(content="xong 2")]])

    await chat_with_agent(client, agent, "chào", creator_token)

    assert len(recorded_runs) == 1
    assert recorded_runs[0].identity.thread.agent_id == agent.id
    assert recorded_runs[0].identity.is_trial is False


# --------------------------------------------------------------------------- #
# A V2 workflow run resolves node by node, not as a flat list of tool calls
# --------------------------------------------------------------------------- #

from tests.test_api.test_workflow_run import (  # noqa: E402 — fixtures and helpers of the workflow suite
    answers,
    confirmed,  # noqa: F401 — pytest fixture, used by name
    documents,  # noqa: F401 — pytest fixture, used by name
    run_workflow,
)


async def test_a_workflow_run_is_traced_node_by_node(
    client,
    creator_token,
    confirmed,  # noqa: F811 — the fixture imported above, by name
    documents,  # noqa: F811 — the fixture imported above, by name
    scripted_chat_model_factory,
    recorded_runs,
) -> None:
    """The canvas shows a graph; the trace must show the same graph (ADR-0053 trace work).

    Before the tracer learned V2's vocabulary this run recorded zero steps —
    its events carry `node_id`, not `step_id` — and its one tool call hung off
    the root. Now the `document_qa` stream resolves to `search` (with the
    knowledge search inside it) → `answer` → `finish`.
    """
    scripted_chat_model_factory([[answers("Hoàn tiền trong 7 ngày làm việc.")]])
    version = confirmed()

    response = await run_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    assert response.status_code == 200, response.text
    assert len(recorded_runs) == 1
    run = recorded_runs[0]
    assert [(s.step_id, s.node_type) for s in run.steps] == [
        ("search", "knowledge_search"),
        ("answer", "agent_llm"),
        ("finish", "end"),
    ]
    search, answer, _ = run.steps
    assert [t.tool for t in search.tools] == ["knowledge_search"] and search.tools[0].ok is True
    # `port_taken` is only written where a routing decision is made (ADR-0026
    # §3 — `IfElse`, `While`, `HumanApproval`); a search leaving by its one port
    # records none, and the trace must not invent one.
    assert search.port_taken is None
    assert answer.output == "Hoàn tiền trong 7 ngày làm việc."
    assert run.error is None and run.paused_at is None
