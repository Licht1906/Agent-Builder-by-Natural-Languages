"""Eval (#13): the creator's criteria become tests, the creator owns the tests.

Every model in this file is a scripted double — the agent's models **and the
judge's**. That is the ticket's last acceptance criterion, and it is only
possible because the judge is built through the same `get_chat_model_factory`
seam as everything else; a scorer that reached for a model directly could not be
scripted, and every claim below would then rest on whatever a live provider felt
like saying that afternoon.

Read through HTTP like the rest of the suite. The one exception is the approval
gate, which has no endpoint until #14 — `tests/test_evals/test_approval_gate.py`
exercises that at the service seam, the way `test_versions.py` does.
"""

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from src.agents.versions import create_draft
from src.config import get_settings
from src.models.agent_spec import AgentSpec
from tests.conftest import agent_spec, auth_header

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

#: The two criteria most tests generate from. Real sentences, because they are
#: copied verbatim into every case and every result — a criterion of "c1" would
#: let a bug that paraphrases them go unnoticed.
CRITERIA = [
    "Thư trả lời nêu đúng mã đơn hàng khách nhắc tới",
    "Khi thiếu mã đơn hàng, agent hỏi lại thay vì tự suy diễn",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def eval_spec(*, criteria: list[str] | None = None, tools: list[str] | None = None) -> AgentSpec:
    """A one-step agent whose spec declares `criteria` as its definition of correct."""
    return agent_spec(
        steps=[
            {
                "id": "reply",
                "title": "Trả lời",
                "prompt": "p",
                "model": "gpt-4.1",
                "tools": tools or [],
                "max_iterations": 3,
                "next": None,
            }
        ],
        success_criteria=CRITERIA if criteria is None else criteria,
    )


def _base(agent: Any, version: Any) -> str:
    return f"/api/v1/agents/{agent.id}/versions/{version.id}"


async def generate_cases(client: Any, agent: Any, version: Any, token: str) -> Any:
    return await client.post(f"{_base(agent, version)}/eval-cases/generate", headers=auth_header(token))


async def run_eval(client: Any, agent: Any, version: Any, token: str) -> Any:
    return await client.post(f"{_base(agent, version)}/eval-runs", headers=auth_header(token))


def proposes(*cases: tuple[str, str]) -> AIMessage:
    """One generator reply: the JSON array a model returns for a criterion."""
    return AIMessage(
        content=json.dumps(
            [{"input_message": message, "expected": expected} for message, expected in cases], ensure_ascii=False
        )
    )


def verdict(*, passed: bool, reason: str = "vì câu trả lời nêu đúng mã đơn") -> AIMessage:
    """One judge reply."""
    return AIMessage(content=json.dumps({"passed": passed, "reason": reason}, ensure_ascii=False))


def answers(text: str) -> list[AIMessage]:
    """One candidate run of a one-step agent."""
    return [AIMessage(content=text)]


async def a_generated_set(client: Any, agent: Any, version: Any, token: str, factory: Any) -> list[dict[str, Any]]:
    """Generate one case per criterion and hand back the stored cases."""
    factory([[proposes(("đơn DH-1 của tôi đâu?", "nêu đúng DH-1")), proposes(("đơn tôi đâu?", "hỏi lại mã đơn"))]])
    response = await generate_cases(client, agent, version, token)
    assert response.status_code == 200, response.text
    return response.json()["cases"]


# --------------------------------------------------------------------------- #
# Running the set: a verdict and a reason for every case
# --------------------------------------------------------------------------- #


async def test_running_eval_reports_every_case_with_the_reason_it_passed_or_failed(
    client, creator_token, seeded_creator, drafted_agent_of, scripted_chat_model_factory
) -> None:
    """A bare score tells a creator nothing they can act on; the reasons do."""
    agent, version = drafted_agent_of(seeded_creator, eval_spec())
    cases = await a_generated_set(client, agent, version, creator_token, scripted_chat_model_factory)
    requested_models, _ = scripted_chat_model_factory(
        [
            answers("Đơn DH-1 đang giao"),
            [verdict(passed=True, reason="nêu đúng mã DH-1")],
            answers("Chắc là đơn DH-5"),
            [verdict(passed=False, reason="tự suy diễn mã đơn thay vì hỏi lại")],
        ]
    )

    response = await run_eval(client, agent, version, creator_token)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [result["passed"] for result in payload["results"]] == [True, False]
    assert [result["reason"] for result in payload["results"]] == [
        "nêu đúng mã DH-1",
        "tự suy diễn mã đơn thay vì hỏi lại",
    ]
    assert [result["criterion"] for result in payload["results"]] == CRITERIA
    assert [result["case_id"] for result in payload["results"]] == [case["id"] for case in cases]
    assert payload["passed_count"] == 1
    assert payload["total_count"] == 2
    # Two runs of the agent's own model, each followed by the platform's judge.
    # The judge is whichever model this deployment configured (ADR-0054 §4's
    # `JUDGE_MODEL`), not a hard-coded name — what the claim is about is that
    # the judge is asked once per case, on the judge's model rather than the
    # agent's.
    judge = get_settings().judge_model
    assert requested_models == ["gpt-4.1", judge, "gpt-4.1", judge]


# --------------------------------------------------------------------------- #
# ADR-0054 §1 — the suite belongs to the agent, not the version
# --------------------------------------------------------------------------- #


async def test_a_new_version_inherits_the_suite_without_regenerating(
    client, creator_token, seeded_creator, drafted_agent_of, scripted_chat_model_factory, db_session
) -> None:
    """AC: a version created from one with 10 cases sees them all, with nothing to press."""
    agent, version = drafted_agent_of(seeded_creator, eval_spec())
    cases = await a_generated_set(client, agent, version, creator_token, scripted_chat_model_factory)

    version2 = create_draft(db_session, agent, eval_spec())

    listing = await client.get(f"{_base(agent, version2)}/eval-cases", headers=auth_header(creator_token))
    assert listing.status_code == 200, listing.text
    assert {case["id"] for case in listing.json()} == {case["id"] for case in cases}
    assert len(listing.json()) == len(CRITERIA)


async def test_a_case_whose_criterion_left_a_later_versions_spec_still_runs_marked_orphan(
    client, creator_token, seeded_creator, drafted_agent_of, scripted_chat_model_factory, db_session
) -> None:
    """ADR-0054 §1: a criterion the creator reworded away does not take its old case's evidence with it."""
    agent, version = drafted_agent_of(seeded_creator, eval_spec())
    await a_generated_set(client, agent, version, creator_token, scripted_chat_model_factory)

    version2 = create_draft(db_session, agent, eval_spec(criteria=[CRITERIA[0]]))
    scripted_chat_model_factory(
        [
            answers("Đơn DH-1 đang giao"),
            [verdict(passed=True)],
            answers("Chắc là đơn DH-5"),
            [verdict(passed=True)],
        ]
    )

    response = await run_eval(client, agent, version2, creator_token)

    assert response.status_code == 200, response.text
    payload = response.json()
    by_criterion = {result["criterion"]: result["applicability"] for result in payload["results"]}
    assert by_criterion[CRITERIA[0]] == "live"
    assert by_criterion[CRITERIA[1]] == "orphan"
    assert payload["total_count"] == 2
    # An orphan case still counts toward the score — it is graded, not dropped.
    assert payload["passed_count"] == 2


async def test_generating_twice_does_not_duplicate_a_criterion_that_already_has_a_case(
    client, creator_token, seeded_creator, drafted_agent_of, scripted_chat_model_factory
) -> None:
    """ADR-0054 §1: "không sinh lại cho tiêu chí đã có case generated" — reported, not silently no-op'd."""
    agent, version = drafted_agent_of(seeded_creator, eval_spec())
    cases = await a_generated_set(client, agent, version, creator_token, scripted_chat_model_factory)

    scripted_chat_model_factory([[proposes(("đơn khác", "khác")), proposes(("đơn khác 2", "khác 2"))]])
    response = await generate_cases(client, agent, version, creator_token)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["cases"] == []
    assert payload["duplicate_count"] == len(CRITERIA)

    listing = await client.get(f"{_base(agent, version)}/eval-cases", headers=auth_header(creator_token))
    assert len(listing.json()) == len(cases)
