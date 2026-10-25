"""UC1-17 (#133) — `record_eval_run`/`latest_eval_run` round-trip."""

from src.db.base import SessionLocal
from src.evals.uc1_eval_runs import latest_eval_run, record_eval_run
from src.evals.uc1_golden_set import GoldenCaseGroup, GoldenCaseJudge
from src.evals.uc1_golden_set_runner import GoldenSetCaseResult, GoldenSetRunResult


def _result(pass_rate: float) -> GoldenSetRunResult:
    cases = (
        GoldenSetCaseResult(
            case_id="c1", group=GoldenCaseGroup.REFUSAL, judge=GoldenCaseJudge.RULE, passed=True, detail="ok"
        ),
    )
    return GoldenSetRunResult(passed_count=1, total_count=1, pass_rate=pass_rate, cases=cases)


def test_latest_eval_run_is_none_when_nothing_has_ever_been_recorded() -> None:
    with SessionLocal() as db:
        assert latest_eval_run(db, "agt_never_evaluated", 1) is None


def test_record_then_read_back_round_trips_the_case_log() -> None:
    with SessionLocal() as db:
        record_eval_run(db, agent_id="agt_eval_roundtrip", version=1, result=_result(0.9))

        run = latest_eval_run(db, "agt_eval_roundtrip", 1)

    assert run is not None
    assert run.pass_rate == 0.9
    assert run.case_results == [{"case_id": "c1", "group": "refusal", "judge": "rule", "passed": True, "detail": "ok"}]


def test_latest_eval_run_is_the_most_recently_recorded_one() -> None:
    with SessionLocal() as db:
        record_eval_run(db, agent_id="agt_eval_history", version=1, result=_result(0.5))
        record_eval_run(db, agent_id="agt_eval_history", version=1, result=_result(0.95))

        run = latest_eval_run(db, "agt_eval_history", 1)

    assert run is not None
    assert run.pass_rate == 0.95
