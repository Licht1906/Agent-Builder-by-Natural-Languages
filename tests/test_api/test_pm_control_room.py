"""The PM control room — `docs/PM-PLAYBOOK.md` §5 and ADR-0053 over HTTP (`/api/v1/pm/*`).

Five claims, observed through the API (ADR-0048):

1. **It crosses ownership on purpose, so it is gated — and the gate is the PM's,
   not the admin's.**
2. **The board is computed from the shared ledger** (`agent_runs`, both
   pipelines), with simulated rows out unless asked for, and every rate beside
   its `n`. The arithmetic that matters: spec_error vs system_error, cost per
   *successful* run, the outcome taxonomy decided at write time, and the
   transcript summarised into the columns the failure slices group on.
3. **Every lever writes the real row and refuses the wrong thing**: a thumbs
   lands in `quality_scores.run_id`; an expert label lands in `run_labels` and
   only with words the taxonomy knows; a promoted run prefills its input from
   the trace; a rejection without a reason is a 409; a simulated pause is never
   "resumed"; the kill switch flips the Deployment.
4. **The three graders meet on one run**: judge verdicts beside expert labels
   give a confusion matrix; the sampled judge writes one binary score per
   criterion and is never re-run on a run already judged.
5. **The version report reads candidate against baseline** from `eval_runs`,
   counts regressions by input, and names noise as noise.

Rows are arranged through `src.pm.ledger.record_agent_run` — the same writer
the three doors use — so the summaries and code checks under test are the
ones production rows get.
"""

from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.threads import caller_key
from src.db.models import (
    AgentRun,
    AgentVersion,
    EvalCase,
    EvalCaseOrigin,
    EvalRun,
    QualityScore,
    RunApproval,
    RunApprovalStatus,
    RunLabel,
    ScoreSource,
)
from src.models.agent_spec import AgentSpec
from src.models.schemas import SandboxTranscriptEntry
from src.pm.ledger import AgentRunRecord, record_agent_run
from src.pm.simulation import SIM_PREFIX
from tests.conftest import agent_spec, auth_header

pytestmark = pytest.mark.asyncio


def _spec() -> AgentSpec:
    return agent_spec(
        steps=[
            {
                "id": "s",
                "title": "Bước",
                "prompt": "p",
                "model": "gpt-4o-mini",
                "tools": [],
                "max_iterations": 1,
                "next": None,
            }
        ]
    )


def _transcript(
    *, answer: str, tool_ok: bool | None = True, tool_result: str = "3 thư: a, b, c"
) -> list[SandboxTranscriptEntry]:
    """A V2 workflow trace: one mcp node calling one tool, then an agent_llm node speaking `answer`."""
    lines = []
    if tool_ok is not None:
        lines += [
            SandboxTranscriptEntry(
                kind="tool", node_id="tim_thu", tool="gmail.search_emails", args={"query": "hoá đơn"}, simulated=False
            ),
            SandboxTranscriptEntry(
                kind="tool_result",
                node_id="tim_thu",
                tool="gmail.search_emails",
                ok=tool_ok,
                simulated=False,
                content=tool_result,
            ),
            SandboxTranscriptEntry(kind="node", node_id="tim_thu", node_type="mcp", content=tool_result),
        ]
    lines.append(SandboxTranscriptEntry(kind="node", node_id="tra_loi", node_type="agent_llm", content=answer))
    return lines


def _record(
    agent,
    *,
    outcome: str,
    trigger: str,
    cost: float,
    simulated: bool = False,
    model: str = "gpt-4o-mini",
    answer: str = "Có 3 thư: a, b, c.",
    tool_ok: bool | None = True,
    caller: str | None = None,
    trace_id: str | None = None,
) -> AgentRunRecord:
    error = (
        None
        if outcome in ("success", "needs_approval", "budget_stopped")
        else {
            "spec_error": "Node 'gui_thu': tool_id trống — bước chưa được nối với kết nối nào",
            "system_error": "Kết nối ERP hết hạn (HTTP 502) khi gọi query_overdue_invoices",
            "guardrail_block": "Hành động send_email bị chặn: người nhận ngoài miền tổ chức",
            "error": "RuntimeError: step 3 returned no output",
        }[outcome]
    )
    return AgentRunRecord(
        pipeline="v2",
        trace_id=trace_id or f"trace-{outcome}-{trigger}-{cost}-{simulated}".replace(".", ""),
        agent_key=str(agent.id),
        version_key=str(agent.deployment.agent_version_id),
        agent_name=agent.name,
        conversation_id=(SIM_PREFIX if simulated else "real-") + f"{outcome}-{trigger}-{cost}".replace(".", ""),
        caller=caller,
        trigger=trigger,
        input_message="Tìm thư hoá đơn tháng này",
        transcript=_transcript(answer=answer, tool_ok=tool_ok),
        error=error,
        cost_usd=cost,
        latency_seconds=1.5,
        input_tokens=100,
        output_tokens=20,
        usage_by_model={model: {"input_tokens": 100, "output_tokens": 20, "usd": cost}},
        agent_id=agent.id,
        version_id=agent.deployment.agent_version_id,
        simulated=simulated,
    )


def _pending(agent, *, simulated: bool, created_at: datetime) -> RunApproval:
    return RunApproval(
        run_id=f"run-{created_at.timestamp()}",
        node_id="duyet",
        occurrence_index=0,
        version_id=agent.deployment.agent_version_id,
        agent_id=agent.id,
        conversation_id=(SIM_PREFIX if simulated else "real-") + f"p{int(created_at.timestamp())}",
        namespace="prod",
        sandbox=False,
        trace_id="t",
        approver_role="sales_manager",
        message="Gửi 3 thư nhắc nợ?",
        input_message="Nhắc nợ tuần này",
        created_at=created_at,
    )


@pytest.fixture
def board(db_session: Session, seeded_creator, deployed_agent_of):
    """Two published agents and a small, deliberately skewed ledger behind them."""
    cheap = deployed_agent_of(seeded_creator, _spec(), name="Agent rẻ mà hỏng")
    solid = deployed_agent_of(seeded_creator, _spec(), name="Agent ổn")
    now = datetime.now(UTC).replace(tzinfo=None)
    records = [
        # `cheap`: 3 real runs, 1 success → cost/run 0.01, cost/success 0.03
        _record(
            cheap,
            outcome="success",
            trigger="manual",
            cost=0.01,
            caller=caller_key(user_id=seeded_creator.id),
            trace_id="trace-owner-run",
        ),
        _record(cheap, outcome="spec_error", trigger="manual", cost=0.01, tool_ok=False, answer="Đã gửi xong."),
        _record(cheap, outcome="spec_error", trigger="chat_test", cost=0.01),
        _record(cheap, outcome="system_error", trigger="manual", cost=0.01, simulated=True),
        # `solid`: 2 runs, both fine, one a rehearsal; the manual one's answer invents a link
        _record(
            solid,
            outcome="success",
            trigger="manual",
            cost=0.02,
            model="gpt-4o",
            answer="Xem tại https://docs.google.com/document/d/your_document_id_here_000000",
        ),
        _record(solid, outcome="success", trigger="chat_test", cost=0.005),
    ]
    for record in records:
        record_agent_run(db_session, record)
    db_session.add_all(
        [
            _pending(cheap, simulated=True, created_at=now - timedelta(hours=30)),
            _pending(cheap, simulated=False, created_at=now - timedelta(minutes=5)),
        ]
    )
    db_session.commit()
    return {"cheap": cheap, "solid": solid}


# --------------------------------------------------------------------------- #
# 1. Gated
# --------------------------------------------------------------------------- #


async def test_every_door_is_closed_to_a_creator(client, creator_token, board) -> None:
    headers = auth_header(creator_token)
    cheap = board["cheap"]
    assert (await client.get("/api/v1/pm/overview", headers=headers)).status_code == 403
    assert (await client.get("/api/v1/pm/runs", headers=headers)).status_code == 403
    assert (await client.get("/api/v1/pm/review/next", headers=headers)).status_code == 403
    assert (
        await client.put(f"/api/v1/pm/deployments/{cheap.id}/status", json={"serving": False}, headers=headers)
    ).status_code == 403
    assert (await client.post("/api/v1/pm/gate/runs", json={"mode": "simulated"}, headers=headers)).status_code == 403
    assert (await client.post("/api/v1/pm/judge/sample", json={"limit": 1}, headers=headers)).status_code == 403


async def test_the_pm_is_not_an_admin_and_the_admin_is_still_let_in(
    client, pm_token, admin_token, agent_admin_token, board
) -> None:
    pm = auth_header(pm_token)
    assert (await client.get("/api/v1/pm/overview", headers=pm)).status_code == 200
    for admin_door in ("/api/v1/admin/users", "/api/v1/admin/config", "/api/v1/admin/approvals"):
        assert (await client.get(admin_door, headers=pm)).status_code == 403, admin_door
    for token in (admin_token, agent_admin_token):
        assert (await client.get("/api/v1/pm/overview", headers=auth_header(token))).status_code == 200


# --------------------------------------------------------------------------- #
# 2. The board
# --------------------------------------------------------------------------- #


async def test_overview_hides_simulation_by_default_and_reads_the_ledger(client, pm_token, board) -> None:
    headers = auth_header(pm_token)
    body = (await client.get("/api/v1/pm/overview", headers=headers)).json()

    by_trigger = {row["trigger"]: row for row in body["success_by_trigger"]}
    assert set(by_trigger) == {"chat_test", "manual"}
    assert by_trigger["manual"]["runs"] == 3 and by_trigger["manual"]["successes"] == 2
    assert by_trigger["chat_test"]["runs"] == 2 and by_trigger["chat_test"]["rate"] == 0.5

    outcomes = {s["outcome"]: s["runs"] for s in body["outcomes"]["slices"]}
    assert outcomes == {"success": 3, "spec_error": 2}
    assert body["outcomes"]["self_fix_ratio"] == 1.0  # no system_error once the simulated row is out
    assert body["outcomes"]["unclassified_errors"] == 0

    cost = {row["agent_name"]: row for row in body["cost_by_agent"]}
    cheap = cost["Agent rẻ mà hỏng"]
    assert cheap["runs"] == 3 and cheap["cost_per_run"] == pytest.approx(0.01)
    assert cheap["cost_per_success"] == pytest.approx(0.03)
    assert cheap["deployment_status"] == "active" and cheap["latency_p90"] == pytest.approx(1.5)
    models = {m["model"]: m for m in body["cost_by_model"]}
    assert models["gpt-4o"]["runs"] == 1 and models["gpt-4o-mini"]["runs"] == 4

    assert body["simulation"] == {
        "simulated_runs": 1,
        "total_runs": 6,
        "simulated_pending_approvals": 1,
        "shown": False,
    }
    assert body["traces"] == {"runs": 5, "with_trace": 5}
    assert body["thresholds"]["production_success_min"] == 0.8
    assert "wrong_intent" in body["trace_codes"]

    funnel = body["approvals"]
    assert funnel["pending"] == 1 and [p["simulated"] for p in funnel["pending_list"]] == [False]

    shown = (await client.get("/api/v1/pm/overview?include_simulated=true", headers=headers)).json()
    assert {s["outcome"]: s["runs"] for s in shown["outcomes"]["slices"]} == {
        "success": 3,
        "spec_error": 2,
        "system_error": 1,
    }
    assert shown["outcomes"]["self_fix_ratio"] == pytest.approx(2 / 3)
    assert shown["simulation"]["shown"] is True and shown["approvals"]["pending"] == 2
    assert [p["simulated"] for p in shown["approvals"]["pending_list"]] == [True, False]  # oldest first


async def test_the_writer_decides_outcome_summarises_the_trace_and_grades_layer_one(
    client, pm_token, board, db_session
) -> None:
    """What `record_agent_run` did to the fixture's rows: taxonomy, summary columns, code scores."""
    headers = auth_header(pm_token)
    body = (await client.get("/api/v1/pm/overview", headers=headers)).json()

    # The spec_error run with a failed tool: first failure is the tool, node type is the mcp node.
    slices = body["failure_slices"]
    assert slices["failed_runs"] == 2
    assert {s["key"]: s["runs"] for s in slices["by_node_type"]} == {"mcp": 1, "agent_llm": 1}
    assert {s["key"]: s["runs"] for s in slices["by_action"]} == {"gmail.search_emails": 2}
    assert body["outcomes"]["success_with_tool_failures"] == 0

    runs = {r["agent_name"]: [] for r in (await client.get("/api/v1/pm/runs", headers=headers)).json()}
    for r in (await client.get("/api/v1/pm/runs", headers=headers)).json():
        runs[r["agent_name"]].append(r)
    assert all(r["has_trace"] and r["tool_calls"] == 1 for rows in runs.values() for r in rows)
    # Layer one caught the invented Drive link on `solid`'s manual run.
    solid_manual = next(r for r in runs["Agent ổn"] if r["trigger"] == "manual")
    assert solid_manual["tool_grounded"] == 0
    grounded = db_session.scalars(
        select(QualityScore).where(QualityScore.run_id == solid_manual["id"], QualityScore.name == "tool_groundedness")
    ).one()
    assert grounded.source is ScoreSource.CODE and "your_document_id_here" in (grounded.comment or "")
    # …and the failed tool on `cheap`'s spec_error run.
    failed = next(r for r in runs["Agent rẻ mà hỏng"] if r["outcome"] == "spec_error" and r["trigger"] == "manual")
    assert failed["tool_failures"] == 1 and failed["failure_node_type"] == "mcp"
    tools_ok = db_session.scalars(
        select(QualityScore).where(QualityScore.run_id == failed["id"], QualityScore.name == "tools_ok")
    ).one()
    assert tools_ok.value == 0.0

    # Alerts: Q3 names the hidden-failure / taxonomy gaps only when there are any; LAT/COST need ≥5 runs.
    assert all(a["code"] in {"Q1", "Q2", "Q3", "Q4", "LAT", "COST"} for a in body["alerts"])


async def test_runs_list_filters_by_the_words_the_widgets_group_by(client, pm_token, board) -> None:
    headers = auth_header(pm_token)
    spec_errors = (await client.get("/api/v1/pm/runs?outcome=spec_error", headers=headers)).json()
    assert {r["outcome"] for r in spec_errors} == {"spec_error"} and len(spec_errors) == 2
    rehearsals = (await client.get("/api/v1/pm/runs?trigger=chat_test", headers=headers)).json()
    assert len(rehearsals) == 2 and all(r["trigger"] == "chat_test" for r in rehearsals)
    assert sum(r["simulated"] for r in (await client.get("/api/v1/pm/runs", headers=headers)).json()) == 0
    assert (
        sum(
            r["simulated"] for r in (await client.get("/api/v1/pm/runs?include_simulated=true", headers=headers)).json()
        )
        == 1
    )
    by_node = (await client.get("/api/v1/pm/runs?failure_node_type=mcp", headers=headers)).json()
    assert len(by_node) == 1 and by_node[0]["tool_failures"] == 1
    unreviewed = (await client.get("/api/v1/pm/runs?needs_review=true", headers=headers)).json()
    assert len(unreviewed) == 5


# --------------------------------------------------------------------------- #
# 3. The levers
# --------------------------------------------------------------------------- #


async def test_thumbs_land_in_quality_scores_and_show_on_the_run(client, pm_token, board, db_session) -> None:
    headers = auth_header(pm_token)
    run = (await client.get("/api/v1/pm/runs?outcome=spec_error", headers=headers)).json()[0]
    response = await client.post(
        f"/api/v1/pm/runs/{run['id']}/feedback",
        json={"thumbs_up": False, "comment": "sai node gui_thu"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    score = db_session.scalars(
        select(QualityScore).where(QualityScore.run_id == run["id"], QualityScore.name == "thumbs_up")
    ).one()
    assert score.value == 0.0 and score.source is ScoreSource.HUMAN and score.given_by is not None
    assert score.comment == "sai node gui_thu" and score.run_cost_id is None  # arranged rows have no bill

    again = (await client.get("/api/v1/pm/runs?outcome=spec_error", headers=headers)).json()
    assert next(r for r in again if r["id"] == run["id"])["thumbs"] == 0
    overview = (await client.get("/api/v1/pm/overview", headers=headers)).json()
    assert overview["feedback_notes"][0]["comment"] == "sai node gui_thu"
    assert overview["feedback_by_week"][0]["thumbs_down"] == 1
    assert (
        await client.post("/api/v1/pm/runs/999999/feedback", json={"thumbs_up": True}, headers=headers)
    ).status_code == 404


async def test_the_person_who_got_the_answer_can_thumb_it_by_trace_id(
    client, creator_token, second_creator_token, board, db_session
) -> None:
    """`POST /runs/feedback` — the chat window's door, keyed by the `thread` event's `trace_id`."""
    mine = await client.post(
        "/api/v1/runs/feedback",
        json={"trace_id": "trace-owner-run", "thumbs_up": True, "comment": "đúng thư"},
        headers=auth_header(creator_token),
    )
    assert mine.status_code == 201, mine.text
    score = db_session.get(QualityScore, mine.json()["score_id"])
    assert score.run_id == mine.json()["run_id"] and score.value == 1.0 and score.source is ScoreSource.HUMAN
    # Somebody else's run is a 404, never a 403: a run id is not a thing to enumerate.
    theirs = await client.post(
        "/api/v1/runs/feedback",
        json={"trace_id": "trace-owner-run", "thumbs_up": False},
        headers=auth_header(second_creator_token),
    )
    assert theirs.status_code == 404
    assert (
        await client.post(
            "/api/v1/runs/feedback",
            json={"trace_id": "nope-nope", "thumbs_up": True},
            headers=auth_header(creator_token),
        )
    ).status_code == 404


async def test_expert_labels_need_taxonomy_words_and_drive_the_review_queue(
    client, pm_token, board, db_session
) -> None:
    headers = auth_header(pm_token)
    overview = (await client.get("/api/v1/pm/overview", headers=headers)).json()
    assert overview["review_queue"] == 3  # manual runs with a trace, none labelled yet

    nxt = (await client.get("/api/v1/pm/review/next", headers=headers)).json()
    assert nxt is not None and nxt["trigger"] == "manual" and nxt["judge_hidden_until_labelled"] is True
    assert nxt["transcript"] and nxt["input_message"] == "Tìm thư hoá đơn tháng này" and nxt["queue_remaining"] == 3
    # The failed run is preferred over a plain success when nothing is judged or thumbed.
    assert nxt["outcome"] == "spec_error"

    unknown = await client.post(
        f"/api/v1/pm/runs/{nxt['id']}/label", json={"passed": False, "codes": ["made_up_code"]}, headers=headers
    )
    assert unknown.status_code == 422
    labelled = await client.post(
        f"/api/v1/pm/runs/{nxt['id']}/label",
        json={"passed": False, "codes": ["tool_failure_hidden"], "note": "tool hỏng mà nói đã gửi"},
        headers=headers,
    )
    assert labelled.status_code == 201, labelled.text
    assert labelled.json()["queue_remaining"] == 2 and labelled.json()["codes"] == ["tool_failure_hidden"]
    row = db_session.scalars(select(RunLabel).where(RunLabel.run_id == nxt["id"])).one()
    assert row.passed is False and row.reviewer_id is not None

    detail = (await client.get(f"/api/v1/pm/runs/{nxt['id']}", headers=headers)).json()
    assert detail["labels"][0]["codes"] == ["tool_failure_hidden"] and detail["judge_hidden_until_labelled"] is False
    listed = next(r for r in (await client.get("/api/v1/pm/runs", headers=headers)).json() if r["id"] == nxt["id"])
    assert listed["expert_passed"] is False and listed["expert_codes"] == ["tool_failure_hidden"]
    slices = (await client.get("/api/v1/pm/overview", headers=headers)).json()["failure_slices"]
    assert slices["by_trace_code"] == [{"key": "tool_failure_hidden", "runs": 1}]
    # A failed label with no code is filed `other`; a pass carries no codes.
    passed = await client.post(
        f"/api/v1/pm/runs/{nxt['id']}/label", json={"passed": True, "codes": ["format"]}, headers=headers
    )
    assert passed.json()["codes"] == []
    assert (
        await client.post("/api/v1/pm/runs/999999/label", json={"passed": True}, headers=headers)
    ).status_code == 404


async def test_calibration_is_a_confusion_matrix_over_runs_both_graders_saw(
    client, pm_token, board, db_session
) -> None:
    headers = auth_header(pm_token)
    runs = (await client.get("/api/v1/pm/runs?trigger=manual", headers=headers)).json()
    assert len(runs) == 3
    # Judge said: pass, pass, fail. Expert says: pass, fail, fail.
    judge_says = [True, True, False]
    expert_says = [True, False, False]
    for run, j in zip(runs, judge_says, strict=True):
        db_session.add(
            QualityScore(
                run_id=run["id"],
                agent_id=run["agent_id"],
                version_id=run["version_id"],
                name="judge_pass",
                value=1.0 if j else 0.0,
                source=ScoreSource.JUDGE,
                comment="x",
            )
        )
    db_session.commit()
    for run, e in zip(runs, expert_says, strict=True):
        assert (
            await client.post(
                f"/api/v1/pm/runs/{run['id']}/label",
                json={"passed": e, "codes": [] if e else ["dropped_results"]},
                headers=headers,
            )
        ).status_code == 201

    cal = (await client.get("/api/v1/pm/overview", headers=headers)).json()["calibration"]
    assert (cal["tp"], cal["fn"], cal["fp"], cal["tn"]) == (1, 0, 1, 1)
    assert cal["tpr"] == 1.0 and cal["tnr"] == 0.5 and cal["agreement"] == pytest.approx(2 / 3)
    assert cal["labelled"] == 3 and cal["trusted"] is False and cal["min_labels"] == 30
    assert (
        len(cal["disagreements"]) == 1
        and cal["disagreements"][0]["judge_passed"]
        and not cal["disagreements"][0]["expert_passed"]
    )
    # And the version row shows all three graders side by side.
    quality = {
        (q["agent_key"], q["version_key"]): q
        for q in (await client.get("/api/v1/pm/overview", headers=headers)).json()["quality_by_version"]
    }
    cheap = board["cheap"]
    row = quality[(str(cheap.id), str(cheap.deployment.agent_version_id))]
    assert row["online"]["n"] == 2 and row["judge"]["n"] == 2 and row["expert"]["n"] == 2 and row["deployed"] is True
    # ADR-0065 retired the scenario grid, so the board no longer carries a
    # coverage column for it. A row that still reported one would be offering a
    # number nothing can produce.
    assert "grid_coverage" not in row


async def test_the_sampled_judge_writes_one_binary_score_per_criterion_and_skips_judged_runs(
    client, pm_token, board, db_session, scripted_chat_model_factory
) -> None:
    headers = auth_header(pm_token)
    verdict = AIMessage(content='{"passed": true, "reason": "bám đúng kết quả tool"}')
    # 2 candidate runs (manual + success + answer) × 2 platform criteria = 4 judge calls.
    scripted_chat_model_factory([[verdict]] * 4)
    response = await client.post("/api/v1/pm/judge/sample", json={"limit": 10}, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["judged"]) == 2 and body["passed"] == 2 and body["skipped"] == 0
    assert {v["category"] for v in body["judged"][0]["verdicts"]} == {"correctness", "relevance"}
    names = sorted(
        db_session.scalars(
            select(QualityScore.name).where(
                QualityScore.run_id == body["judged"][0]["run_id"], QualityScore.source == ScoreSource.JUDGE
            )
        )
    )
    assert names == ["judge:correctness", "judge:relevance", "judge_pass"]
    # Nothing left to judge: a second call spends no tokens.
    scripted_chat_model_factory([])
    again = (await client.post("/api/v1/pm/judge/sample", json={"limit": 10}, headers=headers)).json()
    assert again["judged"] == [] and again["skipped"] == 0
    quality = (await client.get("/api/v1/pm/overview", headers=headers)).json()["quality_by_version"]
    assert sum(q["judge"]["n"] for q in quality) == 2


async def test_deciding_a_pause_records_it_once_and_never_resumes_a_simulated_one(
    client, pm_token, board, db_session
) -> None:
    headers = auth_header(pm_token)
    cheap = board["cheap"]
    pending = (await client.get("/api/v1/pm/overview?include_simulated=true", headers=headers)).json()["approvals"][
        "pending_list"
    ]
    simulated, real = pending[0], pending[1]
    assert simulated["simulated"] and not real["simulated"]

    refused = await client.post(
        f"/api/v1/pm/agents/{cheap.id}/approvals/{simulated['id']}", json={"approved": False}, headers=headers
    )
    assert refused.status_code == 409

    approved = await client.post(
        f"/api/v1/pm/agents/{cheap.id}/approvals/{simulated['id']}", json={"approved": True}, headers=headers
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["resumed"] is False
    row = db_session.get(RunApproval, simulated["id"])
    db_session.refresh(row)
    assert row.status is RunApprovalStatus.APPROVED and row.decided_by is not None

    assert (
        await client.post(
            f"/api/v1/pm/agents/{cheap.id}/approvals/{simulated['id']}", json={"approved": True}, headers=headers
        )
    ).status_code == 409
    assert (
        await client.post(
            f"/api/v1/pm/agents/{board['solid'].id}/approvals/{real['id']}", json={"approved": True}, headers=headers
        )
    ).status_code == 409

    funnel = (await client.get("/api/v1/pm/overview?include_simulated=true", headers=headers)).json()["approvals"]
    assert funnel["pending"] == 1 and funnel["decided"] == 1 and funnel["approval_rate"] == 1.0
    assert funnel["avg_wait_minutes"] > 29 * 60


async def test_kill_switch_flips_the_deployment_and_refuses_an_unpublished_agent(
    client, pm_token, creator_token, board
) -> None:
    headers = auth_header(pm_token)
    cheap = board["cheap"]
    paused = await client.put(f"/api/v1/pm/deployments/{cheap.id}/status", json={"serving": False}, headers=headers)
    assert paused.status_code == 200 and paused.json()["status"] == "disabled"
    mine = await client.get(f"/api/v1/agents/{cheap.id}/deployment", headers=auth_header(creator_token))
    assert mine.json()["status"] == "disabled"
    board_view = (await client.get("/api/v1/pm/overview", headers=headers)).json()
    assert next(r for r in board_view["cost_by_agent"] if r["agent_id"] == cheap.id)["deployment_status"] == "disabled"
    resumed = await client.put(f"/api/v1/pm/deployments/{cheap.id}/status", json={"serving": True}, headers=headers)
    assert resumed.json()["status"] == "active"
    assert (
        await client.put("/api/v1/pm/deployments/999999/status", json={"serving": False}, headers=headers)
    ).status_code == 404


async def test_promoting_a_run_prefills_its_input_from_the_trace(client, pm_token, board, db_session) -> None:
    headers = auth_header(pm_token)
    run = (await client.get("/api/v1/pm/runs?outcome=spec_error", headers=headers)).json()[0]
    response = await client.post(
        f"/api/v1/pm/runs/{run['id']}/promote", json={"expected": "Không nói đã gửi khi tool hỏng"}, headers=headers
    )
    assert response.status_code == 201, response.text
    body = response.json()
    case = db_session.get(EvalCase, body["case_id"])
    assert case.introduced_in_version_id == run["version_id"] and case.origin is EvalCaseOrigin.PROMOTED
    assert case.input_message == "Tìm thư hoá đơn tháng này" and "spec_error" in case.criterion
    assert '"source": "real_failure"' in body["golden_line"] and '"answer_was"' in body["golden_line"]
    assert (
        await client.post(f"/api/v1/pm/runs/{run['id']}/promote", json={"expected": "   "}, headers=headers)
    ).status_code == 422

    # A UC-1 run has no version row: the golden line only, and the reply says so.
    uc1 = record_agent_run(
        db_session,
        AgentRunRecord(
            pipeline="uc1",
            trace_id="req-1",
            agent_key="agt_ar_assistant",
            version_key="3",
            agent_name="AR Assistant",
            conversation_id="thread-1",
            trigger="manual",
            input_message="Khách nào quá hạn?",
            transcript=[],
            answer="Hai khách.",
        ),
    )
    db_session.commit()
    promoted = (
        await client.post(f"/api/v1/pm/runs/{uc1.id}/promote", json={"expected": "Liệt kê đủ"}, headers=headers)
    ).json()
    assert promoted["case_id"] is None and '"pipeline": "uc1"' in promoted["golden_line"]


# --------------------------------------------------------------------------- #
# 4. The version report
# --------------------------------------------------------------------------- #


def _eval_run(version_id: int, results: list[tuple[str, str, bool]], *, threshold: float = 0.75) -> EvalRun:
    payload = [
        {
            "case_id": i,
            "criterion": criterion,
            "input_message": text,
            "expected": "ok",
            "answer": "a",
            "passed": passed,
            "reason": "vì thế" if passed else "trượt vì sai",
            "category": "correctness",
        }
        for i, (text, criterion, passed) in enumerate(results, start=1)
    ]
    return EvalRun(
        version_id=version_id,
        threshold=threshold,
        passed_count=sum(1 for r in payload if r["passed"]),
        total_count=len(payload),
        results=payload,
    )


async def test_version_report_reads_candidate_against_baseline_and_names_regressions(
    client, pm_token, board, db_session, seeded_creator
) -> None:
    headers = auth_header(pm_token)
    cheap = board["cheap"]
    baseline_version_id = cheap.deployment.agent_version_id
    candidate = AgentVersion(
        agent_id=cheap.id, spec=db_session.get(AgentVersion, baseline_version_id).spec, status="approved"
    )
    db_session.add(candidate)
    db_session.flush()
    db_session.add_all(
        [
            _eval_run(
                baseline_version_id,
                [("A", "đúng", True), ("B", "đúng", True), ("C", "đúng", False), ("D", "đúng", True)],
            ),
            _eval_run(
                candidate.id, [("A", "đúng", True), ("B", "đúng", False), ("C", "đúng", True), ("D", "đúng", True)]
            ),
        ]
    )
    db_session.commit()

    report = (await client.get(f"/api/v1/pm/versions/{candidate.id}/report", headers=headers)).json()
    assert report["baseline_version_id"] == baseline_version_id
    assert report["candidate"] == {"n": 4, "passed": 3, "rate": 0.75} and report["baseline"]["rate"] == 0.75
    line = report["lines"][0]
    assert line["category"] == "correctness" and line["delta"] == 0.0 and line["noise"] is True
    assert [r["input_message"] for r in report["regressions"]] == ["B"]
    assert report["verdict"] == "iterate" and any("từng đạt" in n for n in report["notes"])
    assert any("dải nhiễu" in n for n in report["notes"])
    assert (await client.get("/api/v1/pm/versions/999999/report", headers=headers)).status_code == 404

    db_session.delete(candidate)
    db_session.commit()


async def test_version_report_suppresses_delta_when_judge_model_changed(client, pm_token, board, db_session) -> None:
    """ADR-0054 §4 / EVAL-3 AC: a judge swap is a new baseline, not a regression source."""
    headers = auth_header(pm_token)
    cheap = board["cheap"]
    baseline_version_id = cheap.deployment.agent_version_id
    candidate = AgentVersion(
        agent_id=cheap.id, spec=db_session.get(AgentVersion, baseline_version_id).spec, status="approved"
    )
    db_session.add(candidate)
    db_session.flush()
    baseline_run = _eval_run(baseline_version_id, [("A", "đúng", True), ("B", "đúng", True)])
    baseline_run.judge_model = "gpt-4.1-2025-04-14"
    candidate_run = _eval_run(candidate.id, [("A", "đúng", True), ("B", "đúng", False)])
    candidate_run.judge_model = "gpt-5-mini"
    db_session.add_all([baseline_run, candidate_run])
    db_session.commit()

    report = (await client.get(f"/api/v1/pm/versions/{candidate.id}/report", headers=headers)).json()
    assert report["judge_changed"] is True
    assert report["baseline_judge_model"] == "gpt-4.1-2025-04-14"
    assert report["candidate_judge_model"] == "gpt-5-mini"
    assert report["regressions"] == []
    assert all(line["delta"] is None for line in report["lines"])
    assert any("Judge đổi" in n for n in report["notes"])

    db_session.delete(candidate)
    db_session.commit()


async def test_regressions_match_by_suite_case_id_even_when_the_wording_changed(
    client, pm_token, board, db_session
) -> None:
    """ADR-0054 §1 / EVAL-6: `_regressions` keys on `suite_case_id`, so a case caught
    wobbling is caught even though the creator edited its câu chữ between the two runs
    — case ids never survived a version before this, so the string match this replaces
    would have missed it."""
    headers = auth_header(pm_token)
    cheap = board["cheap"]
    baseline_version_id = cheap.deployment.agent_version_id
    candidate = AgentVersion(
        agent_id=cheap.id, spec=db_session.get(AgentVersion, baseline_version_id).spec, status="approved"
    )
    db_session.add(candidate)
    db_session.flush()

    def result(case_id: int, text: str, passed: bool) -> dict:
        return {
            "case_id": case_id,
            "suite_case_id": case_id,
            "criterion": "đúng",
            "input_message": text,
            "expected": "ok",
            "answer": "a",
            "passed": passed,
            "reason": "vì thế" if passed else "trượt vì sai",
            "category": "correctness",
        }

    baseline_results = [result(7, "Khách nào quá hạn?", True), result(8, "Huỷ đơn được không?", True)]
    candidate_results = [
        result(7, "Khách nào đang quá hạn thanh toán?", False),  # #7: wording changed, still the same case → regression
        result(8, "Huỷ đơn được không?", True),
    ]
    db_session.add_all(
        [
            EvalRun(
                version_id=baseline_version_id,
                threshold=0.75,
                passed_count=2,
                total_count=2,
                results=baseline_results,
            ),
            EvalRun(
                version_id=candidate.id,
                threshold=0.75,
                passed_count=1,
                total_count=2,
                results=candidate_results,
            ),
        ]
    )
    db_session.commit()

    report = (await client.get(f"/api/v1/pm/versions/{candidate.id}/report", headers=headers)).json()
    assert [r["input_message"] for r in report["regressions"]] == ["Khách nào đang quá hạn thanh toán?"]
    assert not any("ghép" in n.lower() for n in report["notes"])  # new-style run: no fallback note

    db_session.delete(candidate)
    db_session.commit()


async def test_regressions_fall_back_to_string_matching_for_a_run_written_before_suite_case_id(
    client, pm_token, board, db_session
) -> None:
    """A baseline written before ADR-0054 §1 stores `suite_case_id: 0` on every result
    (the schema's read-back default) — `_regressions` must still catch a regression on
    such a run, by the old `(input_message, criterion)` match, and say it did."""
    headers = auth_header(pm_token)
    cheap = board["cheap"]
    baseline_version_id = cheap.deployment.agent_version_id
    candidate = AgentVersion(
        agent_id=cheap.id, spec=db_session.get(AgentVersion, baseline_version_id).spec, status="approved"
    )
    db_session.add(candidate)
    db_session.flush()
    db_session.add_all(
        [
            _eval_run(baseline_version_id, [("A", "đúng", True), ("B", "đúng", True)]),
            _eval_run(candidate.id, [("A", "đúng", True), ("B", "đúng", False)]),
        ]
    )
    db_session.commit()

    report = (await client.get(f"/api/v1/pm/versions/{candidate.id}/report", headers=headers)).json()
    assert [r["input_message"] for r in report["regressions"]] == ["B"]
    assert any("ghép" in n.lower() and "chuỗi" in n.lower() for n in report["notes"])

    db_session.delete(candidate)
    db_session.commit()


# --------------------------------------------------------------------------- #
# 5. The gate (unchanged by ADR-0053)
# --------------------------------------------------------------------------- #


async def test_simulated_gate_run_is_stored_classified_and_names_the_decks_own_defects(client, pm_token, board) -> None:
    headers = auth_header(pm_token)
    response = await client.post(
        "/api/v1/pm/gate/runs", json={"mode": "simulated", "fidelity": 1.0, "seed": 1, "repeats": 2}, headers=headers
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["runs"]) == 2
    assert set(body["unsatisfiable"]) == {"multi-turn-01", "multi-turn-04", "diagnose-03"}
    for run in body["runs"]:
        assert run["mode"] == "simulated" and run["passed"] is False
        failed = {v["case_id"]: v for v in run["verdicts"] if not v["passed"]}
        assert set(failed) == set(body["unsatisfiable"])
        assert {v["classification"] for v in failed.values()} == {"case_issue"}
        assert run["per_family"]["multi_turn"] == {"passed": 3, "total": 5, "wanted": 4, "out_of": 5}
        assert "GATE: FAIL" in run["report"]
    assert len(body["always_passed"]) == 16
    history = (await client.get("/api/v1/pm/overview", headers=headers)).json()["gate_history"]
    assert [g["id"] for g in history[:2]] == [body["runs"][1]["id"], body["runs"][0]["id"]]
    assert history[0]["failure_classes"] == {"case_issue": 3}
    detail = await client.get(f"/api/v1/pm/gate/runs/{body['runs'][0]['id']}", headers=headers)
    assert detail.status_code == 200 and len(detail.json()["verdicts"]) == 19
    assert (await client.get("/api/v1/pm/gate/runs/999999", headers=headers)).status_code == 404


async def test_a_noisy_gate_run_pins_its_extra_failures_on_the_builder(client, pm_token, board) -> None:
    headers = auth_header(pm_token)
    response = await client.post(
        "/api/v1/pm/gate/runs", json={"mode": "simulated", "fidelity": 0.3, "seed": 7}, headers=headers
    )
    assert response.status_code == 201, response.text
    run = response.json()["runs"][0]
    classes = run["failure_classes"]
    assert classes.get("case_issue") == 3
    assert classes.get("builder_issue", 0) >= 1
    assert all(
        v["classification"] == ("case_issue" if v["case_id"] in response.json()["unsatisfiable"] else "builder_issue")
        for v in run["verdicts"]
        if not v["passed"]
    )


# --------------------------------------------------------------------------- #
# Cleanup of rows the fixtures do not own
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _drop_ledger_rows(db_session: Session):
    yield
    db_session.rollback()
    for row in db_session.scalars(select(AgentRun)).all():
        db_session.delete(row)
    db_session.commit()
