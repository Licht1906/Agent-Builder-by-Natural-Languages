"""Trial runs in the sandbox (#9): what runs for real, what only says what it would do.

The claim this file exists to pin down is the one the whole approval flow rests
on: **a version nobody has approved cannot touch the outside world**. A test
that only asserts "the transcript says [SANDBOX]" would still pass against a
runtime that sent the email *and* wrote the line, so
`test_a_side_effecting_tool_never_reaches_its_real_implementation` makes the
real implementation's own infrastructure explode if it is ever reached.

Everything is observed through HTTP, like the rest of the suite — no test here
imports `src.agents.sandbox` or `src.agents.runtime`. The tool simulations are
the product's own (issue #2's testing policy: the simulations are a production
feature, not test scaffolding), so nothing here mocks a tool.
"""

import pytest
from langchain_core.messages import AIMessage

from src.models.agent_spec import AgentSpec
from tests.conftest import agent_spec, parse_sse, trial_run_version

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _one_step_spec(*, tools: list[str], allowed_domains: list[str] | None = None) -> AgentSpec:
    """One step that may reach for `tools` — the smallest thing worth trialling."""
    return agent_spec(
        steps=[
            {
                "id": "reply",
                "title": "Trả lời",
                "prompt": "p",
                "model": "gpt-4o",
                "tools": tools,
                "max_iterations": 3,
                "next": None,
            }
        ],
        allowed_domains=allowed_domains,
    )


#: Shared with the budget suite (#10), which starts trial runs for its own reasons.
run_sandbox = trial_run_version


def _calls_email(*, to: str = "khach@example.com") -> list[AIMessage]:
    """A model that reaches for `send_email`, then reports what it did."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "send_email",
                    "args": {"to": to, "subject": "Xin lỗi vì sự cố", "body": "Chúng tôi đã hoàn tiền."},
                    "id": "call-1",
                }
            ],
        ),
        AIMessage(content="Đã xử lý khiếu nại"),
    ]


# --------------------------------------------------------------------------- #
# Read-only tools run for real; side-effecting ones do not run at all
# --------------------------------------------------------------------------- #


async def test_a_read_only_tool_really_runs_during_a_trial_run(
    client, creator_token, seeded_creator, drafted_agent_of, scripted_chat_model_factory
) -> None:
    """The whole point of not just printing a plan: the creator sees a real answer."""
    agent, version = drafted_agent_of(seeded_creator, _one_step_spec(tools=["calculate"]))
    scripted_chat_model_factory(
        [
            [
                AIMessage(content="", tool_calls=[{"name": "calculate", "args": {"expression": "2+3*4"}, "id": "c1"}]),
                AIMessage(content="Kết quả là 14"),
            ]
        ]
    )

    response = await run_sandbox(client, agent, version, "2+3*4 bằng mấy?", creator_token)

    assert response.status_code == 200, response.text
    results = parse_sse(response.text, event="tool_result")
    assert [result["tool"] for result in results] == ["calculate"]
    assert results[0]["result"] == "14"
    assert results[0]["simulated"] is False
    assert results[0]["ok"] is True


async def test_a_side_effecting_tool_never_reaches_its_real_implementation(
    client,
    creator_token,
    seeded_creator,
    drafted_agent_of,
    scripted_chat_model_factory,
    smtp_that_must_never_be_reached,
) -> None:
    agent, version = drafted_agent_of(seeded_creator, _one_step_spec(tools=["send_email"]))
    scripted_chat_model_factory([_calls_email()])

    response = await run_sandbox(client, agent, version, "khách phàn nàn về đơn hàng", creator_token)

    assert response.status_code == 200, response.text
    results = parse_sse(response.text, event="tool_result")
    assert [result["tool"] for result in results] == ["send_email"]
    # `ok` and not a failure: the simulation succeeded, which is what a creator
    # needs to see. A failed tool here would look like a broken agent.
    assert results[0]["ok"] is True
    assert results[0]["simulated"] is True
    assert results[0]["result"].startswith("[SANDBOX] sẽ gửi email tới khach@example.com")
