"""Smoke and gate — two tiers of one suite, and three trials for safety — EV-04 / ADR-0069 §7–8.

The ticket's criteria, each pinned here:

- smoke asks about twenty cases the system picks: every safety case, then one
  per criterion in turn;
- a second smoke with nothing changed makes no model call and runs nothing;
- editing a node re-runs only the cases whose run went through it;
- the gate runs the whole set again, whatever smoke just cached, and a smoke
  run is never what the gate reads;
- an injection case passing two trials of three is a safety violation, which
  no override waives.

The agent's own runs are stood in for (`_eval_run`), so a test can say which
nodes a case went through and count how often a case ran; grading is real, and
so is the judge's model seam, which is how "no model call" is counted.
"""

import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.db.models import AgentVersion, User, VersionStatus
from src.evals import workflow_runner
from src.evals.cases import add_case
from src.evals.gate import unwaivable_blocker
from src.evals.results import latest_run
from src.evals.smoke import SAFETY_TRIALS, SMOKE_SIZE
from src.models.schemas import SandboxTranscriptEntry
from tests.conftest import finished_eval_run
from tests.test_api.test_workflow_eval import _Router, install

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

SPEC: dict[str, Any] = {
    "goal": "Trả lời câu hỏi về đơn hàng",
    "users": ["Nhân viên"],
    "data": [{"name": "Sổ đơn hàng", "kind": "uploaded_file", "ref": "don-hang.md"}],
    "actions": ["Trả lời"],
    "expected_outcome": ["Câu trả lời"],
    "success_criteria": [
        {"text": "Trả lời đúng trạng thái đơn", "category": "correctness"},
        {"text": "Trả lời đúng trọng tâm", "category": "relevance"},
        {"text": "Giữ giọng lịch sự", "category": "correctness"},
    ],
    "prohibitions": ["Không bịa trạng thái đơn"],
}


def _graph(extra_prompt: str) -> dict[str, Any]:
    """`answer → extra → finish`: a short case stops after `answer`, a long one goes through `extra` too."""
    return {
        "start_node_id": "answer",
        "nodes": [
            {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời.", "model": "gpt-4o-mini"}},
            {"id": "extra", "type": "agent_llm", "config": {"prompt": extra_prompt, "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "answer", "source_port": "done", "target_node_id": "extra"},
            {"id": "e2", "source_node_id": "extra", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {},
    }


@pytest.fixture
def version(
    solution_spec_version: Callable[..., AgentVersion], seeded_creator: User, db_session: Session
) -> AgentVersion:
    """Thirty cases over three criteria — half short, half long — and four injection cases."""
    made = solution_spec_version(
        seeded_creator, SPEC, status=VersionStatus.WORKFLOW_CONFIRMED, workflow=_graph("Bổ sung chi tiết.")
    )
    for n in range(30):
        criterion = SPEC["success_criteria"][n % 3]["text"]
        length = "ngắn" if n % 2 else "dài"
        add_case(
            db_session, made.agent_id, made.id, criterion=criterion, input_message=f"{length} {n:02d}", expected="ok"
        )
    for n in range(4):
        case = add_case(
            db_session,
            made.agent_id,
            made.id,
            criterion=SPEC["success_criteria"][0]["text"],
            input_message=f"inject {n}",
            expected="không làm theo",
        )
        case.tags = ["injection"]
    db_session.commit()
    return made


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Every agent run, by message — a short one visits `answer`, a long one `answer` and `extra`."""
    seen: Counter[str] = Counter()

    async def fake_run(message: str, subject: Any, *, scripted: Any) -> Any:
        seen[message] += 1
        path = ["answer", "finish"] if message.startswith("ngắn") else ["answer", "extra", "finish"]
        transcript = [SandboxTranscriptEntry(kind="node", node_id=node, content="") for node in path]
        transcript[0] = SandboxTranscriptEntry(kind="node", node_id="answer", content=f"đáp {message}")
        return workflow_runner._RunRecord(transcript=transcript, answer=f"đáp {message}")

    monkeypatch.setattr(workflow_runner, "_eval_run", fake_run)
    return seen


class _Counting(_Router):
    """The judge's model seam, counting every model it is asked to build."""

    def __init__(self) -> None:
        super().__init__(judge_passes=True)
        self.calls = 0

    def __call__(self, model_id: str) -> Any:
        self.calls += 1
        return super().__call__(model_id)


async def _smoke(client: AsyncClient, version: AgentVersion, token: str) -> dict[str, Any]:
    response = await finished_eval_run(client, version.agent_id, version.id, token, kind="smoke")
    assert response.status_code == 200, response.text
    return response.json()


async def _gate(client: AsyncClient, version: AgentVersion, token: str) -> dict[str, Any]:
    response = await finished_eval_run(client, version.agent_id, version.id, token)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Smoke: twenty the system picks, reused while nothing changed
# --------------------------------------------------------------------------- #


async def test_smoke_asks_every_safety_case_then_one_per_criterion_in_turn(
    client: AsyncClient, creator_token: str, version: AgentVersion, runs: Counter[str]
) -> None:
    install(_Counting())
    started = time.monotonic()

    run = await _smoke(client, version, creator_token)

    asked = [result["input_message"] for result in run["results"]]
    assert run["kind"] == "smoke" and len(asked) == SMOKE_SIZE
    assert {f"inject {n}" for n in range(4)} <= set(asked)
    per_criterion = Counter(
        result["criterion"] for result in run["results"] if not result["input_message"].startswith("inject")
    )
    assert sorted(per_criterion.values()) == [5, 5, 6], (
        "stratified: no criterion gets a second case before all have one… roughly"
    )
    assert all(count == 1 for count in runs.values()), "smoke is one trial per case"
    assert time.monotonic() - started < 60


async def test_a_second_smoke_with_nothing_changed_calls_no_model_and_runs_nothing(
    client: AsyncClient, creator_token: str, version: AgentVersion, runs: Counter[str]
) -> None:
    install(_Counting())
    first = await _smoke(client, version, creator_token)
    runs.clear()
    counting = install(_Counting())

    again = await _smoke(client, version, creator_token)

    assert counting.calls == 0
    assert sum(runs.values()) == 0
    assert all(result["cached"] for result in again["results"])
    strip = lambda results: [{**r, "cached": False} for r in results]  # noqa: E731
    assert strip(again["results"]) == strip(first["results"])


async def test_editing_a_node_reruns_only_the_cases_that_went_through_it(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    runs: Counter[str],
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
    db_session: Session,
) -> None:
    install(_Counting())
    await _smoke(client, version, creator_token)
    edited = solution_spec_version(
        seeded_creator,
        SPEC,
        status=VersionStatus.WORKFLOW_CONFIRMED,
        workflow=_graph("Bổ sung chi tiết, ngắn gọn hơn."),
    )
    edited.agent_id = version.agent_id
    db_session.commit()
    runs.clear()
    install(_Counting())

    run = await _smoke(client, edited, creator_token)

    rerun = set(runs)
    assert rerun and all(message.startswith(("dài", "inject")) for message in rerun), rerun
    assert all(result["cached"] for result in run["results"] if result["input_message"].startswith("ngắn"))
    assert not any(result["cached"] for result in run["results"] if result["input_message"] in rerun)


# --------------------------------------------------------------------------- #
# Gate: the whole set, never from cache; three trials for safety
# --------------------------------------------------------------------------- #


async def test_the_gate_runs_the_whole_set_again_right_after_a_smoke_and_reads_no_smoke(
    client: AsyncClient, creator_token: str, version: AgentVersion, runs: Counter[str], db_session: Session
) -> None:
    install(_Counting())
    smoke = await _smoke(client, version, creator_token)
    assert latest_run(db_session, version.id) is None, "a smoke run is not what the gate reads"
    runs.clear()
    install(_Counting())

    gate = await _gate(client, version, creator_token)

    assert gate["kind"] == "gate" and len(gate["results"]) == 34
    assert not any(result["cached"] for result in gate["results"])
    assert all(runs[f"inject {n}"] == SAFETY_TRIALS for n in range(4))
    assert all(runs[message] == 1 for message in runs if not message.startswith("inject"))
    assert len(runs) == 34
    gate_run = latest_run(db_session, version.id)
    assert gate_run is not None and gate_run.id == gate["id"] != smoke["id"]


async def test_an_injection_case_passing_two_trials_of_three_is_a_safety_violation_no_override_waives(
    client: AsyncClient,
    creator_token: str,
    version: AgentVersion,
    runs: Counter[str],
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    real_judge = workflow_runner.judge_answer
    judged: Counter[str] = Counter()

    async def judge(**kwargs: Any) -> Any:
        message = kwargs["input_message"]
        judged[message] += 1
        if message == "inject 0" and judged[message] == 2:
            return type("Verdict", (), {"passed": False, "reason": "đã làm theo chỉ dẫn chèn vào"})()
        return await real_judge(**kwargs)

    monkeypatch.setattr(workflow_runner, "judge_answer", judge)
    install(_Router(judge_passes=True))

    gate = await _gate(client, version, creator_token)

    [broken] = [result for result in gate["results"] if result["input_message"] == "inject 0"]
    assert (broken["trials"], broken["trials_passed"], broken["passed"]) == (3, 2, False)
    assert broken["safety_violation"] is True
    assert "2/3" in broken["reason"]
    blocker = unwaivable_blocker(db_session, version.id)
    assert blocker is not None and "inject" not in blocker and "không vượt rào được" in blocker
