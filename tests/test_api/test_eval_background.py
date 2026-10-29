"""An eval run answered at once and run in the background, several cases at a time — EV-01 / ADR-0069 §7.

The ticket's criteria, each pinned here:

- the request that starts a run answers with it `running`, before any case has
  run, and `GET …/eval-runs/{id}` reads the run back finished;
- the same suite run eight at a time and one at a time is stored identically,
  in case order — not in the order cases happened to finish;
- no more than `eval_concurrency` cases are ever in flight;
- one case that raises fails that case, with the reason, and the rest score;
- a run that stops as a whole says so (`failed`, `error`), a run left
  `running` by a dead process is marked failed at boot, and neither a running
  nor a failed run is what the gate reads.

What it cannot pin is the wall clock against a real provider (a 100-case
suite in under 5 minutes): that is a property of the models' latency, measured
on the demo agent rather than here.
"""

# The demo agent and its Hệ CSDL are `test_sample_eval_suite`'s, imported so pytest
# finds the fixtures by name — which ruff reads as each test redefining the import.
# ruff: noqa: F811

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import AgentVersion, EvalRun, EvalRunStatus, User, VersionStatus
from src.evals import workflow_runner
from src.evals.cases import add_case
from src.evals.gate import version_blocker
from src.evals.results import INTERRUPTED_RUN, latest_run, open_run, recover_interrupted_eval_runs
from tests.conftest import auth_header, finished_eval_run
from tests.test_api.test_sample_eval_suite import (  # noqa: F401 — fixtures are found by name
    SPEC,
    _demo_views,
    _DemoRouter,
    _load,
    demo_agent,
    demo_source,
)
from tests.test_api.test_workflow_eval import install

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

ANSWER = "Theo v_ds22_ar, khách quá hạn của bạn được liệt kê ở trên."


def _concurrency(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    monkeypatch.setattr(get_settings(), "eval_concurrency", n)


async def _start(client: AsyncClient, version: AgentVersion, token: str) -> Any:
    return await client.post(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/workflow-eval-runs", headers=auth_header(token)
    )


# --------------------------------------------------------------------------- #
# Answered at once, read back finished
# --------------------------------------------------------------------------- #


async def test_starting_a_run_answers_running_before_any_case_has_run(
    client: AsyncClient, creator_token: str, demo_agent: AgentVersion
) -> None:
    await _load(client, demo_agent, creator_token)
    install(_DemoRouter(answer_text=ANSWER))

    started = await _start(client, demo_agent, creator_token)
    finished = await client.get(
        f"/api/v1/agents/{demo_agent.agent_id}/versions/{demo_agent.id}/eval-runs/{started.json()['id']}",
        headers=auth_header(creator_token),
    )

    assert started.status_code == 200, started.text
    begun = started.json()
    assert (begun["status"], begun["done_count"], begun["total_count"], begun["results"]) == ("running", 0, 19, [])
    assert len(begun["skipped"]) == 6, "classification happens before the response — the skipped are known at once"
    run = finished.json()
    assert (run["status"], run["done_count"], run["total_count"]) == ("completed", 19, 19)
    assert len(run["results"]) == 19 and run["error"] is None


async def test_the_screen_reopens_on_a_running_run_while_the_gate_keeps_the_last_finished_one(
    client: AsyncClient, creator_token: str, demo_agent: AgentVersion, db_session: Session
) -> None:
    await _load(client, demo_agent, creator_token)
    install(_DemoRouter(answer_text=ANSWER))
    finished = await finished_eval_run(client, demo_agent.agent_id, demo_agent.id, creator_token)
    going = open_run(db_session, demo_agent.id, threshold=0.8, total=19, skipped=[], judge_model="judge")

    latest = await client.get(
        f"/api/v1/agents/{demo_agent.agent_id}/versions/{demo_agent.id}/eval-runs/latest",
        headers=auth_header(creator_token),
    )

    assert latest.json()["id"] == going.id and latest.json()["status"] == "running"
    gate_run = latest_run(db_session, demo_agent.id)
    assert gate_run is not None and gate_run.id == finished.json()["id"]
    assert version_blocker(db_session, demo_agent.id) == finished.json()["blocked_reason"]


# --------------------------------------------------------------------------- #
# Parallel and sequential agree — in case order
# --------------------------------------------------------------------------- #


async def test_the_same_suite_eight_at_a_time_and_one_at_a_time_is_stored_identically(
    client: AsyncClient, creator_token: str, demo_agent: AgentVersion, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real runs of the demo graph, real reads of the Hệ CSDL, the model routed on the prompt rather than on order."""
    await _load(client, demo_agent, creator_token)

    def run_with(n: int) -> Any:
        _concurrency(monkeypatch, n)
        install(_DemoRouter(answer_text=ANSWER))
        return finished_eval_run(client, demo_agent.agent_id, demo_agent.id, creator_token)

    parallel = (await run_with(8)).json()
    sequential = (await run_with(1)).json()

    assert parallel["results"] == sequential["results"]
    assert [result["suite_case_id"] for result in parallel["results"]] == sorted(
        result["suite_case_id"] for result in parallel["results"]
    )
    assert (parallel["passed_count"], parallel["total_count"]) == (sequential["passed_count"], 19)


# --------------------------------------------------------------------------- #
# A hundred cases: bounded, in order, whatever order they finish in
# --------------------------------------------------------------------------- #


@pytest.fixture
def hundred(
    solution_spec_version: Callable[..., AgentVersion], seeded_creator: User, db_session: Session, demo_source: Any
) -> AgentVersion:
    from tests.test_api.test_sample_eval_suite import _database_qa

    version = solution_spec_version(
        seeded_creator, SPEC, status=VersionStatus.WORKFLOW_CONFIRMED, workflow=_database_qa(demo_source)
    )
    for n in range(100):
        add_case(
            db_session,
            version.agent_id,
            version.id,
            criterion=SPEC["success_criteria"][0]["text"],
            input_message=f"câu hỏi số {n:03d}",
            expected="trả lời đúng",
            run_as=None,
        )
    return version


async def test_a_hundred_cases_never_exceed_the_limit_and_are_stored_in_case_order(
    client: AsyncClient,
    creator_token: str,
    hundred: AgentVersion,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Earlier cases are made slower, so they finish last — the stored order must still be the suite's."""
    _concurrency(monkeypatch, 8)
    in_flight = 0
    peak = 0
    finished: list[str] = []

    async def slow_run(message: str, subject: Any, *, scripted: Any) -> Any:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.001 * (100 - int(message.rsplit(" ", 1)[-1])))
        in_flight -= 1
        finished.append(message)
        return workflow_runner._RunRecord(answer=f"đáp {message}")

    monkeypatch.setattr(workflow_runner, "_eval_run", slow_run)
    install(_DemoRouter(answer_text=ANSWER))

    run = (await finished_eval_run(client, hundred.agent_id, hundred.id, creator_token)).json()

    assert run["status"] == "completed" and run["total_count"] == 100
    assert peak == 8
    stored = [result["input_message"] for result in run["results"]]
    assert stored == [f"câu hỏi số {n:03d}" for n in range(100)]
    assert finished != stored, "the cases really did finish out of order"
    assert [result["answer"] for result in run["results"]] == [f"đáp {message}" for message in stored]


# --------------------------------------------------------------------------- #
# One case raising fails only itself; a run that stops says why
# --------------------------------------------------------------------------- #


async def test_one_case_whose_grading_raises_fails_with_the_reason_and_the_rest_still_score(
    client: AsyncClient, creator_token: str, hundred: AgentVersion, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_judge = workflow_runner.judge_answer

    async def judge(**kwargs: Any) -> Any:
        if kwargs["input_message"] == "câu hỏi số 042":
            raise RuntimeError("nhà cung cấp model từ chối")
        return await real_judge(**kwargs)

    monkeypatch.setattr(workflow_runner, "judge_answer", judge)
    install(_DemoRouter(answer_text=ANSWER))

    run = (await finished_eval_run(client, hundred.agent_id, hundred.id, creator_token)).json()

    assert run["status"] == "completed" and len(run["results"]) == 100
    [broken] = [result for result in run["results"] if result["input_message"] == "câu hỏi số 042"]
    assert broken["passed"] is False
    assert "nhà cung cấp model từ chối" in broken["reason"]
    assert run["passed_count"] == 99


async def test_a_run_that_stops_as_a_whole_is_failed_with_its_reason_and_is_not_the_gates(
    client: AsyncClient,
    creator_token: str,
    demo_agent: AgentVersion,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    await _load(client, demo_agent, creator_token)
    install(_DemoRouter(answer_text=ANSWER))

    def full_disk(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("ổ đĩa đầy")

    monkeypatch.setattr(workflow_runner, "finish_run", full_disk)

    run = (await finished_eval_run(client, demo_agent.agent_id, demo_agent.id, creator_token)).json()

    assert (run["status"], run["error"]) == ("failed", "ổ đĩa đầy")
    assert latest_run(db_session, demo_agent.id) is None


def test_a_run_a_dead_process_left_running_is_marked_failed_at_boot(
    db_session: Session, solution_spec_version: Callable[..., AgentVersion], seeded_creator: User
) -> None:
    version = solution_spec_version(seeded_creator, SPEC)
    stalled = open_run(db_session, version.id, threshold=0.8, total=5, skipped=[], judge_model="judge")

    assert recover_interrupted_eval_runs() >= 1

    db_session.expire_all()
    row = db_session.get(EvalRun, stalled.id)
    assert row is not None and (row.status, row.error) == (EvalRunStatus.FAILED, INTERRUPTED_RUN)
