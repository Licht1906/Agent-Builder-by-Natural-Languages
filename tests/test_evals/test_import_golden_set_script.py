"""ADR-0054 §6 (EVAL-7 / #157) — `scripts/import_golden_set.py`'s own AC:
run twice, second run is all `skipped`."""

from scripts.import_golden_set import main
from src.db.base import SessionLocal
from src.evals.uc1_golden_set import DEFAULT_GOLDEN_SET_PATH, TOTAL_CASE_COUNT
from src.evals.uc1_suite import list_uc1_cases

_AGENT_ID = "agt_import_golden_set_script_test"


def test_running_the_script_twice_the_second_run_is_all_skipped(capsys) -> None:
    first = main(["--agent", _AGENT_ID, "--file", str(DEFAULT_GOLDEN_SET_PATH)])
    assert first == 0
    assert f"{TOTAL_CASE_COUNT} added, 0 skipped" in capsys.readouterr().out

    second = main(["--agent", _AGENT_ID, "--file", str(DEFAULT_GOLDEN_SET_PATH)])
    assert second == 0
    assert f"0 added, {TOTAL_CASE_COUNT} skipped" in capsys.readouterr().out

    db = SessionLocal()
    try:
        assert len(list_uc1_cases(db, _AGENT_ID)) == TOTAL_CASE_COUNT
    finally:
        db.close()
