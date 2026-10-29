"""A suite that follows its spec, and a reviewer who sees how it moved — EV-05 / ADR-0069 §4–6.

On the demo agent ("ERP mẫu", ≥100 live cases from EV-03), version 1 runs a
gate in which one hand-written case fails. Version 2 rewords that case's
criterion. The ticket's criteria:

- building version 2's suite retires the reworded criterion's old case, writes
  a case for the new wording, and the suite is still ≥100 live;
- the retired case was failing at the baseline, and the approval screen counts
  it among "K case đang rớt" — beside the creator's own edits and deletions,
  each with who did it;
- the retired case is still readable in the suite's history;
- the version report's delta leaves the new case out and lists it apart.

Plus the creator's read of a failing case ("agent sai" / "case sai"), kept with
who and when. Agent runs are stood in for (`_eval_run`); grading is real.
"""

# The ERP mẫu fixtures are `test_erp_mau_seed`'s and the bound agent is
# `test_demo_rehearsal`'s, imported so pytest finds them by name — which ruff reads
# as each test redefining the import it receives.
# ruff: noqa: F811

import copy
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from src.db.models import AgentVersion, EvalCase, EvalSuiteChange, SuiteChangeKind, User, VersionStatus
from src.evals import workflow_runner
from src.evals.cases import add_case
from src.pm.reports import version_report
from src.workflow.generator import workflow_of
from tests.conftest import auth_header, finished_eval_run, writes_json
from tests.test_api.test_data_query_node import SPEC
from tests.test_api.test_demo_rehearsal import _trusted_test_host, agent  # noqa: F401 — fixtures are found by name
from tests.test_api.test_workflow_eval import _Router, install
from tests.test_db.test_erp_mau_seed import erp_dsn, erp_source  # noqa: F401 — fixtures are found by name

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

OLD = SPEC["success_criteria"][0]["text"]
NEW = "Đúng số liệu công nợ của người hỏi"
FAILING = "Tổng công nợ quá hạn của cả công ty là bao nhiêu?"


class _Grader(_Router):
    """Judges fail the one hand-written case; generation writes one case for whatever criterion it is asked about."""

    def answer(self, prompt: str) -> AIMessage:
        if "Bạn là người chấm bài" in prompt and FAILING in prompt:
            self.judged.append(prompt)
            return writes_json({"passed": False, "reason": "lộ số của cả công ty"})
        if "Bạn đang viết test case" in prompt:
            return writes_json(
                [{"input_message": "Công nợ quá hạn của tôi là bao nhiêu?", "expected": "chỉ số của tôi"}]
            )
        return super().answer(prompt)


@pytest.fixture(autouse=True)
def _fake_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(message: str, subject: Any, *, scripted: Any) -> Any:
        return workflow_runner._RunRecord(answer=f"Trả lời cho: {message}")

    monkeypatch.setattr(workflow_runner, "_eval_run", run)


async def _build(client: AsyncClient, version: AgentVersion, token: str) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/eval-suite/build", headers=auth_header(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def versions(
    agent: Callable[[str], AgentVersion],
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
    db_session: Session,
) -> tuple[AgentVersion, Callable[[], AgentVersion]]:
    """Version 1, and a maker for version 2 — the same graph, `OLD` reworded to `NEW`."""
    first = agent("database_qa")

    def second() -> AgentVersion:
        spec = copy.deepcopy(SPEC)
        spec["success_criteria"][0]["text"] = NEW
        made = solution_spec_version(
            seeded_creator, spec, status=VersionStatus.WORKFLOW_CONFIRMED, workflow=first.workflow
        )
        made.agent_id = first.agent_id
        db_session.commit()
        return made

    return first, second


async def test_rewording_a_criterion_retires_its_failing_case_writes_a_new_one_and_the_reviewer_sees_it(
    client: AsyncClient,
    creator_token: str,
    admin_token: str,
    versions: tuple[AgentVersion, Callable[[], AgentVersion]],
    db_session: Session,
) -> None:
    first, make_second = versions
    install(_Grader(generation=[]))
    await _build(client, first, creator_token)
    failing = add_case(
        db_session, first.agent_id, first.id, criterion=OLD, input_message=FAILING, expected="—", run_as=None
    )
    install(_Grader(generation=[]))
    baseline = (await finished_eval_run(client, first.agent_id, first.id, creator_token)).json()
    [failed] = [result for result in baseline["results"] if result["suite_case_id"] == failing.id]
    assert failed["passed"] is False
    second = make_second()

    install(_Grader(generation=[]))
    built = await _build(client, second, creator_token)

    db_session.expire_all()
    retired = db_session.get(EvalCase, failing.id)
    assert retired is not None and retired.retired_in_version_id == second.id
    assert retired.retired_failing_at_baseline is True
    assert built["retired"] >= 1 and built["generated_added"] >= 1
    assert built["live_count"] >= 100
    fresh = db_session.query(EvalCase).filter(EvalCase.criterion == NEW, EvalCase.retired_in_version_id.is_(None)).all()
    assert fresh, "the reworded criterion has a case of its own"

    # The approval screen: K failing among the retired, beside the creator's own edits.
    listed = await client.get(
        f"/api/v1/agents/{second.agent_id}/versions/{second.id}/eval-cases", headers=auth_header(creator_token)
    )
    assert failing.id not in {case["id"] for case in listed.json()}, "a retired case is out of the suite"
    some = fresh[0]
    edited = await client.patch(
        f"/api/v1/agents/{second.agent_id}/versions/{second.id}/eval-cases/{some.id}",
        json={"expected": "chỉ số của chính người hỏi"},
        headers=auth_header(creator_token),
    )
    assert edited.status_code == 200, edited.text
    dossier = await _dossier(client, second, admin_token)
    changes = dossier["suite_changes"]
    assert changes["baseline_version_id"] == first.id
    assert failing.id in {case["id"] for case in changes["retired"]}
    assert changes["retired_failing_count"] >= 1
    [edit] = [entry for entry in changes["creator_changes"] if entry["case_id"] == some.id]
    assert edit["kind"] == "edited" and edit["actor_email"]
    assert some.id in {case["id"] for case in changes["new_cases"]}

    # History: the retired case is still readable.
    history = await client.get(
        f"/api/v1/agents/{second.agent_id}/versions/{second.id}/eval-suite/history", headers=auth_header(creator_token)
    )
    [kept] = [case for case in history.json()["retired"] if case["id"] == failing.id]
    assert kept["input_message"] == FAILING and kept["retired_failing_at_baseline"] is True

    # The delta: over the cases both runs asked; the new one is listed apart.
    install(_Grader(generation=[]))
    await finished_eval_run(client, second.agent_id, second.id, creator_token)
    report = version_report(db_session, version_id=second.id)
    assert report is not None and report.baseline_version_id == first.id
    assert {case.input_message for case in report.new_cases} >= {some.input_message}
    asked_by_both = report.baseline.n if report.baseline else 0
    assert sum(line.candidate.n for line in report.lines) == sum(
        line.baseline.n for line in report.lines if line.baseline
    )
    assert asked_by_both >= sum(line.candidate.n for line in report.lines)


async def test_a_case_unfit_before_the_change_is_not_retired_by_it(
    client: AsyncClient,
    creator_token: str,
    versions: tuple[AgentVersion, Callable[[], AgentVersion]],
    db_session: Session,
) -> None:
    """An uploaded case under a criterion neither version states was an orphan all along — not this change's doing."""
    first, make_second = versions
    stray = add_case(
        db_session, first.agent_id, first.id, criterion="Tiêu chí từ file khác", input_message="hỏi gì đó", expected="—"
    )
    second = make_second()
    install(_Grader(generation=[]))

    await _build(client, second, creator_token)

    db_session.expire_all()
    assert db_session.get(EvalCase, stray.id).retired_in_version_id is None


async def test_rerunning_an_older_version_retires_nothing(
    client: AsyncClient,
    creator_token: str,
    versions: tuple[AgentVersion, Callable[[], AgentVersion]],
    db_session: Session,
) -> None:
    first, make_second = versions
    case = add_case(db_session, first.agent_id, first.id, criterion=OLD, input_message="cũ", expected="—")
    make_second()
    install(_Grader(generation=[]))

    await finished_eval_run(client, first.agent_id, first.id, creator_token)

    db_session.expire_all()
    assert db_session.get(EvalCase, case.id).retired_in_version_id is None
    assert workflow_of(first) is not None


async def test_the_creators_read_of_a_failing_case_is_kept_with_who_and_when(
    client: AsyncClient,
    creator_token: str,
    versions: tuple[AgentVersion, Callable[[], AgentVersion]],
    db_session: Session,
) -> None:
    first, _ = versions
    case = add_case(db_session, first.agent_id, first.id, criterion=OLD, input_message=FAILING, expected="—")
    base = f"/api/v1/agents/{first.agent_id}/versions/{first.id}/eval-cases"

    read = await client.post(
        f"{base}/{case.id}/triage", json={"verdict": "case_wrong", "run_id": None}, headers=auth_header(creator_token)
    )
    listed = await client.get(base, headers=auth_header(creator_token))
    deleted = await client.delete(f"{base}/{case.id}", headers=auth_header(creator_token))

    assert read.status_code == 200, read.text
    assert read.json()["verdict"] == "case_wrong" and read.json()["actor_email"]
    [row] = [entry for entry in listed.json() if entry["id"] == case.id]
    assert row["triage"]["verdict"] == "case_wrong"
    assert deleted.status_code == 204
    kinds = [change.kind for change in db_session.query(EvalSuiteChange).filter(EvalSuiteChange.case_id == case.id)]
    assert kinds == [SuiteChangeKind.TRIAGED, SuiteChangeKind.DELETED], "the deletion outlives the case it records"


async def _dossier(client: AsyncClient, version: AgentVersion, token: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/admin/approvals/{version.id}", headers=auth_header(token))
    assert response.status_code == 200, response.text
    return response.json()
