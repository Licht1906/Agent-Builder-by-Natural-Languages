"""The accounts-receivable sample eval suite — DS-22 / ADR-0059 §11.

UC-1's golden set, converted to the V2 suite schema (`golden/cong_no_v1.jsonl`)
and shipped with the `database_qa`/`database_outreach` templates. Against a
demo-shaped agent — a Hệ CSDL whose employee view holds `DEMO_ASKERS`, a
`database_qa` graph — the ticket's criterion, through the V2 eval doors:

- the Evaluation tab is offered the suite, and loading it adds all 23 cases,
  each asking as one of the demo employees (`run_as`); loading twice adds none;
- the whole suite runs through `workflow-eval-runs`: every judged case is read
  as its own asker (a rep sees their own customers, the lead the department),
  and the `takes_no_action` rules are skipped as ungrounded on a graph with no
  action to decline — and live on `database_outreach`, which has one;
- a template that ships no suite is told so.
"""

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.config import get_settings
from src.datasources.access import AccessRules
from src.datasources.sources import create_database_source, run_database_test, save_access_rules, set_allowed_views
from src.db.base import engine
from src.db.models import AgentVersion, DatabaseDialect, DatabaseSource, EvalCase, SecretStoreEntry, User, VersionStatus
from src.evals.applicability import Applicability, applicability_of
from src.evals.sample_suites import DEMO_ASKERS, SAMPLE_SUITES
from src.models.workflow import WorkflowJSON
from src.services.secret_store import EncryptedDbSecretStore
from src.workflow.templates import TEMPLATE_REGISTRY
from src.workflow.tools import GMAIL
from tests.conftest import auth_header
from tests.test_api.test_workflow_eval import _Router, install

pytestmark = pytest.mark.asyncio

REP, REP2, LEAD = DEMO_ASKERS["nhan_vien_kd"], DEMO_ASKERS["nhan_vien_kd_2"], DEMO_ASKERS["truong_nhom_kd"]
TRUSTED = frozenset({str(engine.url.host)})

RULES = AccessRules.model_validate(
    {
        "identity": {
            "view": "v_ds22_staff",
            "match_by": "email",
            "attributes": {"employee_code": "emp_id", "department": "dept_code", "position": "position"},
        },
        "rules": [
            {"position": "Nhân viên KD", "views": {"v_ds22_ar": {"where": {"sales_rep_id": "employee_code"}}}},
            {"position": "Trưởng nhóm KD", "views": {"v_ds22_ar": {"where": {"department": "department"}}}},
        ],
    }
)

SPEC: dict[str, Any] = {
    "goal": "Trả lời câu hỏi công nợ theo đúng phạm vi người hỏi",
    "users": ["Nhân viên kinh doanh", "Trưởng nhóm kinh doanh"],
    "data": [{"name": "ERP mẫu", "kind": "database", "ref": None}],
    "actions": ["Tra công nợ"],
    "expected_outcome": ["Câu trả lời có số liệu và trích dẫn"],
    "success_criteria": [
        {"text": "Trả lời đúng số liệu công nợ, chỉ trong phạm vi người hỏi được xem", "category": "correctness"}
    ],
    "prohibitions": ["Không xem khách của người khác"],
}


@pytest.fixture(autouse=True)
def _demo_views(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The demo ERP's shape, small: two reps and their lead in SALES, and a customer of another department."""
    monkeypatch.setattr(get_settings(), "database_source_trusted_hosts", str(engine.url.host))
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE OR REPLACE VIEW v_ds22_staff AS SELECT * FROM (VALUES "
                f"('{REP}', 'E1', 'SALES', 'Nhân viên KD'), ('{REP2}', 'E2', 'SALES', 'Nhân viên KD'), "
                f"('{LEAD}', 'E9', 'SALES', 'Trưởng nhóm KD')"
                ") AS t(email, emp_id, dept_code, position)"
            )
        )
        connection.execute(
            text(
                "CREATE OR REPLACE VIEW v_ds22_ar AS SELECT * FROM (VALUES "
                "('KH-AN-1', 'E1', 'SALES', 45), ('KH-AN-2', 'E1', 'SALES', 8), "
                "('KH-HAI-1', 'E2', 'SALES', 52), ('KH-KHAC-1', 'E7', 'OPS', 90)"
                ") AS t(customer_id, sales_rep_id, department, days_overdue)"
            )
        )
    yield
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW IF EXISTS v_ds22_staff"))
        connection.execute(text("DROP VIEW IF EXISTS v_ds22_ar"))


@pytest.fixture
def demo_source(db_session: Session, seeded_creator: User) -> Iterator[DatabaseSource]:
    store = EncryptedDbSecretStore(db=db_session)
    row = create_database_source(
        db_session,
        seeded_creator.id,
        name="ERP mẫu",
        dialect=DatabaseDialect.POSTGRESQL,
        dsn=engine.url.set(drivername="postgresql").render_as_string(hide_password=False),
        secret_store=store,
    )
    run_database_test(db_session, row, secret_store=store, trusted_hosts=TRUSTED)
    set_allowed_views(db_session, row, ["v_ds22_staff", "v_ds22_ar"])
    save_access_rules(db_session, row, RULES)
    yield row
    db_session.delete(row)
    db_session.query(SecretStoreEntry).filter(SecretStoreEntry.key == row.dsn_secret_ref).delete()
    db_session.commit()


def _database_qa(source: DatabaseSource, *, template_id: str | None = "database_qa") -> dict[str, Any]:
    """The `database_qa` template's graph, bound to the demo Hệ CSDL."""
    return {
        "start_node_id": "query",
        "nodes": [
            {"id": "query", "type": "data_query", "config": {"source_id": str(source.id), "views": ["v_ds22_ar"]}},
            {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời về công nợ.", "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "query", "source_port": "done", "target_node_id": "answer"},
            {"id": "e2", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {"template_id": template_id},
    }


@pytest.fixture
def demo_agent(
    solution_spec_version: Callable[..., AgentVersion], seeded_creator: User, demo_source: DatabaseSource
) -> AgentVersion:
    return solution_spec_version(
        seeded_creator, SPEC, status=VersionStatus.WORKFLOW_CONFIRMED, workflow=_database_qa(demo_source)
    )


class _DemoRouter(_Router):
    """`_Router`, plus the `data_query` node's own call: a list of overdue customers, scope left to the platform."""

    def answer(self, prompt: str) -> AIMessage:
        if "Viết MỘT truy vấn có cấu trúc" in prompt:
            query = {
                "view": "v_ds22_ar",
                "columns": ["customer_id", "days_overdue"],
                "order_by": [{"column": "customer_id", "dir": "asc"}],
            }
            return AIMessage(content="", tool_calls=[{"name": "data_query", "args": query, "id": "q1"}])
        return super().answer(prompt)


async def _load(client: AsyncClient, version: AgentVersion, token: str) -> Any:
    return await client.post(f"/api/v1/agents/{version.agent_id}/eval-suite/sample", headers=auth_header(token))


def test_the_sample_suite_is_the_uc1_golden_set_in_the_v2_schema() -> None:
    lines = [json.loads(line) for line in SAMPLE_SUITES["database_qa"].content().decode("utf-8").splitlines()]

    assert len(lines) == 23
    assert {line["run_as"] for line in lines} == set(DEMO_ASKERS.values())
    assert {line["source_case"].rsplit("_", 1)[0] for line in lines} == {
        "uc1_ar_v3_lookup",
        "uc1_ar_v3_refusal",
        "uc1_ar_v3_leak",
        "uc1_ar_v3_injection",
        "uc1_ar_v3_cross_tenant",
    }
    assert all(line["expected"] and line["criterion"] for line in lines)
    assert sum(1 for line in lines if line.get("expected_check") == {"kind": "takes_no_action"}) == 4


async def test_loading_the_sample_adds_every_case_asking_as_a_demo_employee_once(
    client: AsyncClient, creator_token: str, demo_agent: AgentVersion, db_session: Session
) -> None:
    offered = await client.get(
        f"/api/v1/agents/{demo_agent.agent_id}/eval-suite/sample", headers=auth_header(creator_token)
    )
    first = await _load(client, demo_agent, creator_token)
    again = await _load(client, demo_agent, creator_token)

    assert offered.json() == {"available": True, "name": "Bộ đề mẫu: trợ lý công nợ", "case_count": 23}
    assert first.status_code == 200, first.text
    assert (first.json()["added_count"], first.json()["error_count"]) == (23, 0)
    assert (again.json()["added_count"], again.json()["skipped_count"]) == (0, 23)
    cases = db_session.query(EvalCase).filter(EvalCase.run_as.in_(list(DEMO_ASKERS.values()))).all()
    assert len(cases) == 23
    assert all(case.extra and case.extra["source_case"].startswith("uc1_ar_v3_") for case in cases)


async def test_the_sample_runs_through_v2_eval_each_case_as_its_own_asker(
    client: AsyncClient, creator_token: str, demo_agent: AgentVersion
) -> None:
    await _load(client, demo_agent, creator_token)
    router = install(_DemoRouter(answer_text="Theo v_ds22_ar, khách quá hạn của bạn được liệt kê ở trên."))

    response = await client.post(
        f"/api/v1/agents/{demo_agent.agent_id}/versions/{demo_agent.id}/workflow-eval-runs",
        headers=auth_header(creator_token),
    )

    assert response.status_code == 200, response.text
    run = response.json()
    assert len(run["results"]) == 19 and len(run["skipped"]) == 4, "the four takes_no_action rules need an action"
    reads = {
        result["input_message"] + "|" + read["args"]["run_as"]: read["result"]
        for result in run["results"]
        for read in result["evidence"]
        if read["tool"] == "data_query"
    }
    rep_reads = [rows for key, rows in reads.items() if key.endswith(f"|{REP}")]
    lead_reads = [rows for key, rows in reads.items() if key.endswith(f"|{LEAD}")]
    rep2_reads = [rows for key, rows in reads.items() if key.endswith(f"|{REP2}")]
    assert rep_reads and all("KH-AN-1" in rows and "KH-HAI-1" not in rows for rows in rep_reads)
    assert rep2_reads and all("KH-HAI-1" in rows and "KH-AN-1" not in rows for rows in rep2_reads)
    assert lead_reads and all("KH-AN-1" in rows and "KH-HAI-1" in rows for rows in lead_reads)
    assert all("KH-KHAC-1" not in rows for rows in reads.values()), "nobody in SALES reads another department"
    assert router.judged, "the judged cases reached the judge"


def test_the_action_rules_count_on_the_outreach_template(demo_source: DatabaseSource) -> None:
    """`database_outreach`, its send bound as the demo's is, has an action to decline: `takes_no_action` is graded."""
    skeleton = TEMPLATE_REGISTRY["database_outreach"].skeleton().model_dump(mode="json")
    for node in skeleton["nodes"]:
        if node["id"] == "send":
            node["config"].update({"tool_id": GMAIL, "action": "send_email"})
    outreach = WorkflowJSON.model_validate(skeleton)
    rule = EvalCase(criterion="x", input_message="x", expected="x", expected_check={"kind": "takes_no_action"})

    assert applicability_of(rule, success_criteria=frozenset(), workflow=outreach) is not Applicability.UNGROUNDED


async def test_a_template_that_ships_no_suite_is_told_so(
    client: AsyncClient,
    creator_token: str,
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
    demo_source: DatabaseSource,
) -> None:
    version = solution_spec_version(
        seeded_creator,
        SPEC,
        status=VersionStatus.WORKFLOW_CONFIRMED,
        workflow=_database_qa(demo_source, template_id="direct_assistant"),
    )

    offered = await client.get(
        f"/api/v1/agents/{version.agent_id}/eval-suite/sample", headers=auth_header(creator_token)
    )
    refused = await _load(client, version, creator_token)

    assert offered.json() == {"available": False, "name": None, "case_count": 0}
    assert refused.status_code == 404
