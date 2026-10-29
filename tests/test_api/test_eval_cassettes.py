"""Eval reads frozen data, never the live database — EV-02 / ADR-0069 §2–3.

On "ERP mẫu" (the demo database, DS-23), with the `database_qa` graph bound to
it and the template's sample suite loaded, the ticket's criteria:

- loading the suite freezes each view the graph reads, once per case, as the
  case's asker sees it — the rep's and the lead's cassettes differ;
- "khách nào quá hạn lâu nhất?" asked as the rep and as the lead has two
  answer keys, both computed from the query on the frozen rows, neither shown
  to a judge;
- changing the database between two runs of the suite changes no result, and
  the second run makes no read at all;
- a view whose columns changed makes its cassettes stale — those cases are
  skipped with the reason until "Ghi lại dữ liệu đóng băng" — and a case with
  no cassette is skipped, never read live;
- an answer key that is empty is not graded, unless the case asks about nothing.
"""

# The ERP mẫu fixtures are `test_erp_mau_seed`'s and the bound agent is
# `test_demo_rehearsal`'s, imported so pytest finds them by name — which ruff reads
# as each test redefining the import it receives.
# ruff: noqa: F811

import re
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from src.datasources import reader as live_reader
from src.db.models import AgentVersion, DatabaseSource, EvalCase, EvalCassette
from src.evals.cases import add_case
from src.evals.sample_suites import DEMO_ASKERS
from src.models.eval_check import AnswersFromQuery, check_payload
from tests.conftest import auth_header, finished_eval_run
from tests.test_api.test_demo_rehearsal import _trusted_test_host, agent  # noqa: F401 — fixtures are found by name
from tests.test_api.test_workflow_eval import _Router, install
from tests.test_db.test_erp_mau_seed import erp_dsn, erp_source  # noqa: F401 — fixtures are found by name

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

REP, LEAD = DEMO_ASKERS["nhan_vien_kd"], DEMO_ASKERS["truong_nhom_kd"]
MOST_OVERDUE = "Khách nào đang quá hạn lâu nhất?"
OVERDUE_LIST = {
    "view": "v_ar_open",
    "columns": ["customer_id", "customer_name", "days_overdue"],
    "filters": [{"column": "days_overdue", "op": ">", "value": 30}],
    "order_by": [{"column": "customer_id", "dir": "asc"}],
}
LONGEST = {
    "view": "v_ar_open",
    "columns": ["customer_name", "days_overdue"],
    "order_by": [{"column": "days_overdue", "dir": "desc"}],
    "limit": 1,
}


class _ErpRouter(_Router):
    """The demo agent on ERP mẫu: the query it writes follows the question, the answer repeats the rows it read."""

    def answer(self, prompt: str) -> AIMessage:
        if "Viết MỘT truy vấn có cấu trúc" in prompt:
            query = LONGEST if "lâu nhất" in prompt else OVERDUE_LIST
            return AIMessage(content="", tool_calls=[{"name": "data_query", "args": query, "id": "q1"}])
        if "Bạn là người chấm bài" in prompt or "Bạn đang viết test case" in prompt:
            return super().answer(prompt)
        table = re.search(r"customer_name \| days_overdue\n(.+)", prompt)
        if table:
            return AIMessage(content=f"Theo v_ar_open: {table.group(1)}")
        return AIMessage(content="Theo v_ar_open, đây là các khách quá hạn của bạn.")


@pytest.fixture
def demo(agent: Callable[[str], AgentVersion]) -> AgentVersion:
    return agent("database_qa")


async def _load_sample(client: AsyncClient, version: AgentVersion, token: str) -> None:
    loaded = await client.post(f"/api/v1/agents/{version.agent_id}/eval-suite/sample", headers=auth_header(token))
    assert loaded.status_code == 200, loaded.text


async def _run(client: AsyncClient, version: AgentVersion, token: str) -> dict[str, Any]:
    response = await finished_eval_run(client, version.agent_id, version.id, token)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "completed", run
    return run


def _cassette(db: Session, case: EvalCase, view: str) -> EvalCassette:
    return db.query(EvalCassette).filter(EvalCassette.case_id == case.id, EvalCassette.view == view).one()


def _case(db: Session, message: str, run_as: str) -> EvalCase:
    return db.query(EvalCase).filter(EvalCase.input_message == message, EvalCase.run_as == run_as).one()


# --------------------------------------------------------------------------- #
# Recorded when the case is written, scoped to its asker
# --------------------------------------------------------------------------- #


async def test_loading_the_suite_freezes_each_view_once_per_case_as_its_asker_sees_it(
    client: AsyncClient, creator_token: str, demo: AgentVersion, db_session: Session
) -> None:
    await _load_sample(client, demo, creator_token)

    rep = _cassette(db_session, _case(db_session, MOST_OVERDUE, REP), "v_ar_open")
    lead = _cassette(db_session, _case(db_session, MOST_OVERDUE, LEAD), "v_ar_open")
    rep_index = rep.payload["columns"].index("sales_rep_id")
    lead_index = lead.payload["columns"].index("department")

    assert {row[rep_index] for row in rep.payload["rows"]} == {"E102"}
    assert {row[lead_index] for row in lead.payload["rows"]} == {"Kinh doanh"}
    assert len(lead.payload["rows"]) > len(rep.payload["rows"])
    assert (rep.run_as, lead.run_as) == (REP, LEAD)
    with_asker = db_session.query(EvalCase).filter(EvalCase.run_as.isnot(None)).count()
    assert db_session.query(EvalCassette).count() == 2 * with_asker, "one cassette per view the graph reads, per case"


# --------------------------------------------------------------------------- #
# The answer key is computed from the query — per asker, no judge
# --------------------------------------------------------------------------- #


async def test_the_most_overdue_customer_is_answered_from_the_query_for_each_asker_without_a_judge(
    client: AsyncClient, creator_token: str, demo: AgentVersion
) -> None:
    await _load_sample(client, demo, creator_token)
    router = install(_ErpRouter())

    run = await _run(client, demo, creator_token)

    longest = {
        result["evidence"][0]["args"]["run_as"]: result
        for result in run["results"]
        if result["input_message"] == MOST_OVERDUE
    }
    assert set(longest) == {REP, LEAD}
    assert (
        longest[REP]["passed"] and "Công ty CP Thành Đạt" in longest[REP]["reason"] and "62" in longest[REP]["reason"]
    )
    assert longest[LEAD]["passed"] and "80" in longest[LEAD]["reason"]
    assert "Công ty CP Thành Đạt" not in longest[LEAD]["reason"], (
        "the lead's answer key is the department's, not the rep's"
    )
    assert not any(MOST_OVERDUE in prompt for prompt in router.judged), "an answer key computed by code is not judged"


# --------------------------------------------------------------------------- #
# The database moves; the results do not, and nothing reads it
# --------------------------------------------------------------------------- #


async def test_changing_the_database_between_two_runs_changes_no_result_and_the_run_reads_nothing(
    client: AsyncClient,
    creator_token: str,
    demo: AgentVersion,
    erp_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _load_sample(client, demo, creator_token)
    install(_ErpRouter())
    first = await _run(client, demo, creator_token)

    with psycopg.connect(erp_dsn) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fin_invoice SET due_date = due_date - 100")
        cur.execute("DELETE FROM fin_invoice WHERE invoice_id IN (SELECT invoice_id FROM fin_invoice LIMIT 5)")
        conn.commit()

    def no_live_reads(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an eval run read the live database")

    monkeypatch.setattr(live_reader.PlatformDatabaseReader, "read", no_live_reads)
    monkeypatch.setattr(live_reader.PlatformDatabaseReader, "columns_of", no_live_reads)
    install(_ErpRouter())
    second = await _run(client, demo, creator_token)

    assert second["results"] == first["results"]
    assert second["skipped"] == first["skipped"]
    reads = [item for result in second["results"] for item in result["evidence"] if item["tool"] == "data_query"]
    assert reads and all(item["ok"] is not False for item in reads)


# --------------------------------------------------------------------------- #
# Stale or missing: skipped with the reason, never read live
# --------------------------------------------------------------------------- #


async def test_a_view_whose_columns_changed_is_stale_until_it_is_recorded_again(
    client: AsyncClient,
    creator_token: str,
    demo: AgentVersion,
    erp_source: DatabaseSource,
    db_session: Session,
) -> None:
    await _load_sample(client, demo, creator_token)
    install(_ErpRouter())
    columns = dict(erp_source.view_columns)
    erp_source.view_columns = {**columns, "v_ar_open": [*columns["v_ar_open"], "credit_note"]}
    db_session.commit()

    stale = await _run(client, demo, creator_token)
    erp_source.view_columns = columns
    db_session.commit()
    recorded = await client.post(
        f"/api/v1/agents/{demo.agent_id}/versions/{demo.id}/eval-cassettes/record", headers=auth_header(creator_token)
    )
    install(_ErpRouter())
    fresh = await _run(client, demo, creator_token)

    stale_reasons = [item["reason"] for item in stale["skipped"]]
    assert sum("Cấu trúc view v_ar_open đã đổi" in reason for reason in stale_reasons) == len(stale["skipped"]) - 4
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["recorded"] == db_session.query(EvalCase).filter(EvalCase.run_as.isnot(None)).count()
    assert not any("Cấu trúc view" in item["reason"] for item in fresh["skipped"])


async def test_a_case_without_its_cassette_is_skipped_with_the_reason_and_not_read_live(
    client: AsyncClient,
    creator_token: str,
    demo: AgentVersion,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _load_sample(client, demo, creator_token)
    case = _case(db_session, MOST_OVERDUE, REP)
    db_session.query(EvalCassette).filter(EvalCassette.case_id == case.id).delete()
    db_session.commit()

    def no_live_reads(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an eval run read the live database")

    monkeypatch.setattr(live_reader.PlatformDatabaseReader, "read", no_live_reads)
    install(_ErpRouter())
    run = await _run(client, demo, creator_token)

    [skipped] = [item for item in run["skipped"] if item["suite_case_id"] == case.id]
    assert "Chưa có dữ liệu đóng băng của view" in skipped["reason"]
    assert case.id not in {result["suite_case_id"] for result in run["results"]}


# --------------------------------------------------------------------------- #
# An empty answer key grades nothing — unless nothing is the point
# --------------------------------------------------------------------------- #


async def test_an_empty_answer_key_is_skipped_unless_the_case_asks_about_nothing(
    client: AsyncClient,
    creator_token: str,
    demo: AgentVersion,
    db_session: Session,
) -> None:
    nothing = {**LONGEST, "filters": [{"column": "days_overdue", "op": ">", "value": 10000}]}
    for message, check in [
        ("Khách nào quá hạn trên 10000 ngày?", AnswersFromQuery.model_validate({"query": nothing})),
        (
            "Có khách nào quá hạn trên 10000 ngày không?",
            AnswersFromQuery.model_validate({"query": nothing, "expects_no_rows": True}),
        ),
    ]:
        case = add_case(
            db_session,
            demo.agent_id,
            demo.id,
            criterion="Đúng số liệu",
            input_message=message,
            expected="—",
            run_as=REP,
        )
        case.expected_check = check_payload(check)
        db_session.commit()
    recorded = await client.post(
        f"/api/v1/agents/{demo.agent_id}/versions/{demo.id}/eval-cassettes/record", headers=auth_header(creator_token)
    )
    assert recorded.status_code == 200, recorded.text

    class _Nothing(_ErpRouter):
        def answer(self, prompt: str) -> AIMessage:
            if "Viết MỘT truy vấn có cấu trúc" in prompt:
                return AIMessage(content="", tool_calls=[{"name": "data_query", "args": nothing, "id": "q1"}])
            return super().answer(prompt)

    install(_Nothing())
    run = await _run(client, demo, creator_token)

    [trivial] = run["skipped"]
    assert "rỗng hoặc toàn số 0" in trivial["reason"]
    [empty] = run["results"]
    assert empty["input_message"] == "Có khách nào quá hạn trên 10000 ngày không?"
    assert empty["passed"] is True, empty["reason"]
