"""ADR-0054 §6 (EVAL-7 / #157) — `src.evals.uc1_suite`: UC-1's own suite
identity, and `import_golden_set_into_suite`'s idempotency by `case.id`
(not EVAL-2's `(input, criterion)`, which two of the golden set's cases
share — see #152's closing comment).

Each test uses its own `uc1_agent_id` (`_agent_id(request)` — the test's own
node id) rather than one shared constant: the test Postgres schema is reset
once per session (`tests.conftest._fresh_postgres_schema`), not per test, so
a shared id would let an earlier test's import leak into a later test's
"nothing imported yet" assumption.
"""

import re

from src.db.models import EvalCase, EvalCaseOrigin, EvalSuite
from src.evals.uc1_golden_set import TOTAL_CASE_COUNT, GoldenCaseGroup, load_golden_set
from src.evals.uc1_suite import (
    get_or_create_uc1_suite,
    get_uc1_suite,
    import_golden_set_into_suite,
    list_uc1_cases,
    load_uc1_golden_cases,
)


def _agent_id(request) -> str:
    return "agt_" + re.sub(r"[^a-z0-9]+", "_", request.node.name.lower())[:50]


def test_no_suite_exists_until_something_is_imported(db_session, request) -> None:
    assert get_uc1_suite(db_session, _agent_id(request)) is None


def test_get_or_create_is_idempotent_and_never_touches_agent_id(db_session, request) -> None:
    agent_id = _agent_id(request)
    first = get_or_create_uc1_suite(db_session, agent_id)
    second = get_or_create_uc1_suite(db_session, agent_id)

    assert first.id == second.id
    assert first.uc1_agent_id == agent_id
    assert first.agent_id is None


def test_importing_the_real_golden_file_adds_every_case_exactly_once(db_session, request) -> None:
    agent_id = _agent_id(request)
    cases = load_golden_set()

    report = import_golden_set_into_suite(db_session, agent_id, cases)

    assert report.added == TOTAL_CASE_COUNT
    assert report.skipped == 0
    assert len(list_uc1_cases(db_session, agent_id)) == TOTAL_CASE_COUNT


def test_importing_the_same_file_twice_skips_everything_the_second_time(db_session, request) -> None:
    agent_id = _agent_id(request)
    cases = load_golden_set()
    import_golden_set_into_suite(db_session, agent_id, cases)

    second = import_golden_set_into_suite(db_session, agent_id, cases)

    assert second.added == 0
    assert second.skipped == TOTAL_CASE_COUNT
    assert len(list_uc1_cases(db_session, agent_id)) == TOTAL_CASE_COUNT


def test_three_cases_sharing_literal_input_text_but_different_principals_all_land(db_session, request) -> None:
    """The gap EVAL-2's `(input, criterion)` key falls into (#152's closing
    comment): this module's key is `case.id`, so it does not collapse them.
    """
    agent_id = _agent_id(request)
    cases = load_golden_set()
    inputs = [case.input for case in cases]
    duplicated_inputs = {text for text in inputs if inputs.count(text) > 1}
    assert duplicated_inputs, "fixture assumption: the real golden set has >=1 duplicated `input` text"

    report = import_golden_set_into_suite(db_session, agent_id, cases)

    assert report.added == TOTAL_CASE_COUNT


def test_round_trips_every_case_back_to_its_exact_typed_shape(db_session, request) -> None:
    agent_id = _agent_id(request)
    cases = load_golden_set()
    import_golden_set_into_suite(db_session, agent_id, cases)

    loaded = load_uc1_golden_cases(db_session, agent_id)

    assert {case.id for case in loaded} == {case.id for case in cases}
    assert {case.group for case in loaded} == set(GoldenCaseGroup)
    correct_lookup_by_id = {case.id: case for case in cases if case.group is GoldenCaseGroup.CORRECT_LOOKUP}
    for case in loaded:
        if case.group is GoldenCaseGroup.CORRECT_LOOKUP:
            assert case.retrieval_context == correct_lookup_by_id[case.id].retrieval_context
            assert case.principal == correct_lookup_by_id[case.id].principal


def test_a_row_added_through_the_generic_importer_is_excluded_from_the_golden_read(db_session, request) -> None:
    """`extra=None` (or any shape that is not a `UC1GoldenCase`) is a plain
    suite row, not a golden-set case — `load_uc1_golden_cases` skips it
    rather than raising.
    """
    agent_id = _agent_id(request)
    suite = get_or_create_uc1_suite(db_session, agent_id)
    db_session.add(
        EvalCase(
            suite_id=suite.id,
            criterion="tự viết",
            input_message="câu khác",
            expected="đáp án khác",
            origin=EvalCaseOrigin.MANUAL,
        )
    )
    db_session.commit()

    assert load_uc1_golden_cases(db_session, agent_id) == []


def test_eval_suites_row_is_keyed_by_uc1_agent_id_not_the_v2_agent_table(db_session, request) -> None:
    agent_id = _agent_id(request)
    suite = get_or_create_uc1_suite(db_session, agent_id)
    fetched = db_session.get(EvalSuite, suite.id)
    assert fetched is not None
    assert fetched.agent_id is None
    assert fetched.uc1_agent_id == agent_id
