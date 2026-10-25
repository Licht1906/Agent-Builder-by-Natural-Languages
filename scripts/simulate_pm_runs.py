"""Four weeks of invented runs, so the PM control room can be driven before the first real one.

    python scripts/simulate_pm_runs.py            # seed (idempotent: resets its own rows first)
    python scripts/simulate_pm_runs.py --reset    # remove everything it ever wrote
    python scripts/simulate_pm_runs.py --seed 7 --days 28 --runs-per-day 60

**Everything this writes is marked.** Agents are named `[SIM] …`, every run's
`conversation_id` starts with `sim-`, every gate row says `simulated` — the
marks in `src.pm.simulation`, which the dashboard reads back to say "N of M
runs on this board are invented". `--reset` deletes by the same marks and
touches nothing else: the `[SIM]` agents cascade to their versions,
deployments, run_costs, run_approvals, quality_scores and eval_cases.

**What it invents, and why those shapes.** The point is not volume but the
*patterns* a PM should be able to see on the board:

- four agents across the playbook's departments, one per failure story — a
  healthy one; one that sails through its creator's own rehearsals and fails
  for real users (widget 1's whole reason to keep the two apart); one whose v2
  regressed against v1 (widget 4's "quality by version"); one cheap-per-run but
  dear-per-success (widget 3's whole reason to exist);
- outcomes drawn from the *full* taxonomy (`spec_error`, `system_error`,
  `guardrail_block`, `needs_approval`, `budget_stopped`) even though the
  runtime only writes the coarse three today — so widget 2 shows what step 7
  of the playbook is for, and the coarse `error` rows it also writes show what
  is still unclassified;
- an approval queue with a realistic tail: most decided within the hour, a
  few pending for days, one rejected with a reason;
- judge scores on a sample, human thumbs on a smaller one, a handful of notes
  — enough for the judge-vs-human column to have something to disagree about.

Deterministic under `--seed`, so two people looking at the same board see the
same numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select  # noqa: E402

from src.agents.threads import ThreadNamespace  # noqa: E402
from src.db.base import SessionLocal  # noqa: E402
from src.db.models import (  # noqa: E402
    Agent,
    AgentRun,
    AgentVersion,
    Deployment,
    DeploymentAccessMode,
    DeploymentStatus,
    MetaAgentGateRun,
    QualityScore,
    RunApproval,
    RunApprovalStatus,
    RunCost,
    RunOutcome,
    ScoreSource,
    User,
    VersionStatus,
)
from src.models.schemas import SandboxTranscriptEntry  # noqa: E402
from src.pm import gate  # noqa: E402
from src.pm.ledger import AgentRunRecord, record_agent_run  # noqa: E402
from src.pm.simulation import SIM_AGENT_PREFIX, SIM_PREFIX  # noqa: E402

# -- The cast -------------------------------------------------------------- #


@dataclass(frozen=True)
class Story:
    """One invented agent and the failure pattern it exists to show."""

    name: str
    department: str
    versions: int
    published: bool
    #: Success probability per (trigger) for the *latest* version.
    success: dict[str, float]
    #: How much worse (or better) the previous version was: multiplier on failure rate.
    previous_version_failure_multiplier: float
    #: Share of runs per trigger.
    trigger_mix: dict[str, float]
    #: Average model bill per run in USD and how noisy it is.
    cost_mean: float
    cost_sd: float
    #: Judge faithfulness centre for latest version (1-5) and previous.
    faithfulness: tuple[float, float]
    #: Share of runs that pause on a HumanApproval node.
    approval_share: float
    daily_volume: float


STORIES: tuple[Story, ...] = (
    Story(
        name="Trợ lý quy chế Nhân sự",
        department="Nhân sự",
        versions=2,
        published=True,
        success={"chat_test": 0.90, "manual": 0.94},
        previous_version_failure_multiplier=1.6,
        trigger_mix={"chat_test": 0.25, "manual": 0.75},
        cost_mean=0.012,
        cost_sd=0.004,
        faithfulness=(4.4, 3.9),
        approval_share=0.0,
        daily_volume=0.30,
    ),
    Story(
        name="Nhắc công nợ quá hạn",
        department="Kinh doanh",
        versions=3,
        published=True,
        success={"chat_test": 0.88, "manual": 0.63},
        previous_version_failure_multiplier=1.2,
        trigger_mix={"chat_test": 0.2, "manual": 0.8},
        cost_mean=0.045,
        cost_sd=0.02,
        faithfulness=(4.1, 4.0),
        approval_share=0.45,
        daily_volume=0.35,
    ),
    Story(
        name="Tổng hợp hộp thư hỗ trợ",
        department="CNTT",
        versions=2,
        published=True,
        success={"chat_test": 0.80, "manual": 0.72},
        previous_version_failure_multiplier=0.55,  # v1 was *better*: the regression story
        trigger_mix={"chat_test": 0.3, "manual": 0.7},
        cost_mean=0.030,
        cost_sd=0.012,
        faithfulness=(3.4, 4.3),
        approval_share=0.10,
        daily_volume=0.25,
    ),
    Story(
        name="Đối soát hoá đơn Kế toán",
        department="Kế toán",
        versions=1,
        published=False,
        success={"chat_test": 0.55, "manual": 0.5},
        previous_version_failure_multiplier=1.0,
        trigger_mix={"chat_test": 1.0, "manual": 0.0},
        cost_mean=0.006,
        cost_sd=0.002,
        faithfulness=(3.0, 3.0),
        approval_share=0.0,
        daily_volume=0.10,
    ),
)

MODELS = (("gpt-4o-mini", 0.55), ("gpt-4o", 0.25), ("gemini-2.0-flash", 0.20))

FAILURE_TAXONOMY = (
    (RunOutcome.SPEC_ERROR.value, 0.42),
    (RunOutcome.SYSTEM_ERROR.value, 0.18),
    (RunOutcome.GUARDRAIL_BLOCK.value, 0.12),
    (RunOutcome.BUDGET_STOPPED.value, 0.10),
    (RunOutcome.ERROR.value, 0.18),  # still unclassified — what step 7 has left to do
)

ERROR_SENTENCES = {
    RunOutcome.SPEC_ERROR.value: "Node 'gui_thu': tool_id trống — bước chưa được nối với kết nối nào",
    RunOutcome.SYSTEM_ERROR.value: "Kết nối ERP hết hạn (HTTP 502) khi gọi query_overdue_invoices",
    RunOutcome.GUARDRAIL_BLOCK.value: "Hành động send_email bị chặn: người nhận ngoài miền tổ chức",
    RunOutcome.BUDGET_STOPPED.value: "Dừng vì vượt trần 6 vòng lặp ở node 'lap'",
    RunOutcome.ERROR.value: "RuntimeError: step 3 returned no output",
}

THUMBS_NOTES = (
    (False, "Trả lời sai hẳn điều khoản thai sản — nó lấy quy chế cũ năm 2022"),
    (False, "Gửi mail nhắc nợ cho khách đã thanh toán rồi"),
    (False, "Tóm tắt bỏ sót 3 ticket khẩn cấp của Kinh doanh"),
    (True, "Đúng và có dẫn nguồn, đỡ phải mở file"),
    (False, "Hỏi về công tác phí mà nó trả lời về nghỉ phép"),
    (True, "Danh sách nhắc nợ tuần này chuẩn, trưởng nhóm chỉ cần bấm duyệt"),
    (False, "Hỏi hai lần một câu mà ra hai kết quả khác nhau"),
)

APPROVAL_MESSAGES = (
    "Gửi thư nhắc nợ tới {n} khách hàng quá hạn trên 30 ngày?",
    "Tạo bản nháp thư trả lời cho ticket #{n} (khẩn cấp)?",
    "Ghi kết quả đối soát lên Google Drive (thư mục Kế toán/{n})?",
)


# -- Helpers --------------------------------------------------------------- #


SIM_QUESTIONS = (
    "Nhắc những khách quá hạn trên 30 ngày giúp tôi",
    "Khách nào còn nợ hơn 50 triệu?",
    "Tìm hoá đơn tháng này của công ty Minh Phát",
    "Soạn thư nhắc nợ cho khách quá hạn tuần này",
    "Tổng hợp công nợ phòng kinh doanh",
)

SIM_TOOL_RESULTS = (
    '[{"customer": "Minh Phát", "overdue_days": 41, "amount": 52000000}, {"customer": "An Khang", "overdue_days": 33, "amount": 12500000}]',
    '[{"customer": "Hoàng Gia", "overdue_days": 35, "amount": 8000000}]',
    "[]",
)


def _sim_transcript(rng: random.Random, story, *, outcome: str, success: bool) -> list[SandboxTranscriptEntry]:
    """A short, plausible V2 trace: one mcp read, maybe an approval, an answer — so the review screen has something to show."""
    result = rng.choice(SIM_TOOL_RESULTS)
    tool_ok = outcome != RunOutcome.SYSTEM_ERROR.value and not (success and rng.random() < 0.04)
    lines = [
        SandboxTranscriptEntry(
            kind="tool",
            node_id="lay_cong_no",
            tool="erp_sales.query_overdue_invoices",
            args={"days": 30},
            simulated=False,
        ),
        SandboxTranscriptEntry(
            kind="tool_result",
            node_id="lay_cong_no",
            tool="erp_sales.query_overdue_invoices",
            ok=tool_ok,
            simulated=False,
            content=result if tool_ok else "Kết nối ERP hết hạn (HTTP 502) khi gọi query_overdue_invoices",
        ),
        SandboxTranscriptEntry(
            kind="node", node_id="lay_cong_no", node_type="mcp", content=result if tool_ok else "(lỗi)"
        ),
    ]
    if outcome in (
        RunOutcome.SPEC_ERROR.value,
        RunOutcome.SYSTEM_ERROR.value,
        RunOutcome.GUARDRAIL_BLOCK.value,
        RunOutcome.ERROR.value,
    ):
        return lines  # the run died here — no answer, no end node
    if outcome == RunOutcome.NEEDS_APPROVAL.value:
        lines.append(
            SandboxTranscriptEntry(
                kind="node",
                node_id="soan_thu",
                node_type="agent_llm",
                content="Đã soạn thư nhắc nợ cho 2 khách — chờ duyệt trước khi gửi.",
            )
        )
        return lines
    if not tool_ok:
        answer = (
            "Hiện không kết nối được ERP, tôi chưa lấy được danh sách công nợ."
            if rng.random() < 0.6
            else "Đã gửi thư nhắc nợ cho các khách quá hạn."
        )
    elif result == "[]":
        answer = "Không có khách nào quá hạn trên 30 ngày."
    else:
        answer = (
            "Có 2 khách quá hạn: Minh Phát (41 ngày, 52 triệu) và An Khang (33 ngày, 12,5 triệu)."
            if "An Khang" in result
            else "Có 1 khách quá hạn: Hoàng Gia (35 ngày, 8 triệu)."
        )
        if not success and rng.random() < 0.5:
            answer = "Không tìm thấy khách nào quá hạn."
    lines.append(SandboxTranscriptEntry(kind="node", node_id="tra_loi", node_type="agent_llm", content=answer))
    lines.append(SandboxTranscriptEntry(kind="node", node_id="ket_thuc", node_type="end", content=""))
    return lines


def _pick(rng: random.Random, weighted: dict[str, float] | tuple[tuple[str, float], ...]) -> str:
    items = list(weighted.items()) if isinstance(weighted, dict) else list(weighted)
    total = sum(w for _, w in items)
    roll = rng.random() * total
    for key, weight in items:
        roll -= weight
        if roll <= 0:
            return key
    return items[-1][0]


def _sim_id(rng: random.Random) -> str:
    return SIM_PREFIX + hashlib.sha1(str(rng.random()).encode()).hexdigest()[:20]


def _graph_for(story: Story) -> dict:
    """A small, plausible WorkflowJSON for the version row — enough to open on the canvas."""
    if story.approval_share > 0:
        return {
            "start_node_id": "lay_cong_no",
            "nodes": [
                {
                    "id": "lay_cong_no",
                    "type": "mcp",
                    "config": {"tool_id": "erp_sales", "action": "query_overdue_invoices", "require_approval": "auto"},
                },
                {
                    "id": "soan_thu",
                    "type": "agent_llm",
                    "config": {"prompt": "Soạn thư nhắc cho từng khách hàng quá hạn.", "model": "gpt-4o-mini"},
                },
                {
                    "id": "duyet",
                    "type": "human_approval",
                    "config": {"approver_role": "sales_manager", "message_template": "Gửi {n} thư nhắc nợ?"},
                },
                {
                    "id": "gui_thu",
                    "type": "mcp",
                    "config": {"tool_id": "gmail", "action": "send_email", "require_approval": "always"},
                },
                {"id": "ket_thuc", "type": "end", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source_node_id": "lay_cong_no", "source_port": "done", "target_node_id": "soan_thu"},
                {"id": "e2", "source_node_id": "soan_thu", "source_port": "done", "target_node_id": "duyet"},
                {"id": "e3", "source_node_id": "duyet", "source_port": "approved", "target_node_id": "gui_thu"},
                {"id": "e4", "source_node_id": "duyet", "source_port": "rejected", "target_node_id": "ket_thuc"},
                {"id": "e5", "source_node_id": "gui_thu", "source_port": "done", "target_node_id": "ket_thuc"},
            ],
        }
    return {
        "start_node_id": "tra_cuu",
        "nodes": [
            {"id": "tra_cuu", "type": "knowledge_search", "config": {"source_ids": []}},
            {
                "id": "tra_loi",
                "type": "agent_llm",
                "config": {"prompt": "Trả lời ngắn gọn, có dẫn nguồn.", "model": "gpt-4o-mini"},
            },
            {"id": "ket_thuc", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "tra_cuu", "source_port": "found", "target_node_id": "tra_loi"},
            {"id": "e2", "source_node_id": "tra_loi", "source_port": "done", "target_node_id": "ket_thuc"},
        ],
    }


# -- Reset ----------------------------------------------------------------- #


def reset(db) -> dict[str, int]:
    """Remove every row this script ever wrote; nothing else."""
    agents = list(db.scalars(select(Agent).where(Agent.name.like(f"{SIM_AGENT_PREFIX}%"))))
    counts = {"agents": len(agents)}
    agent_ids = [a.id for a in agents]
    if agent_ids:
        # Explicit deletes for the tables whose FKs to agents do not cascade
        # in the database (versions), then the agents — whose `ondelete=CASCADE`
        # children (run_costs, run_approvals, quality_scores, deployments) go with them.
        version_ids = list(db.scalars(select(AgentVersion.id).where(AgentVersion.agent_id.in_(agent_ids))))
        counts["agent_runs"] = db.execute(delete(AgentRun).where(AgentRun.simulated.is_(True))).rowcount
        counts["run_costs"] = db.execute(delete(RunCost).where(RunCost.agent_id.in_(agent_ids))).rowcount
        counts["run_approvals"] = db.execute(delete(RunApproval).where(RunApproval.agent_id.in_(agent_ids))).rowcount
        counts["quality_scores"] = db.execute(delete(QualityScore).where(QualityScore.agent_id.in_(agent_ids))).rowcount
        db.execute(delete(Deployment).where(Deployment.agent_id.in_(agent_ids)))
        if version_ids:
            from src.db.models import EvalCase, SandboxRun

            db.execute(delete(EvalCase).where(EvalCase.version_id.in_(version_ids)))
            db.execute(delete(SandboxRun).where(SandboxRun.version_id.in_(version_ids)))
            db.execute(delete(AgentVersion).where(AgentVersion.id.in_(version_ids)))
        db.execute(delete(Agent).where(Agent.id.in_(agent_ids)))
    counts["gate_runs"] = db.execute(delete(MetaAgentGateRun).where(MetaAgentGateRun.mode == gate.SIMULATED)).rowcount
    db.commit()
    return counts


# -- Seed ------------------------------------------------------------------ #


def seed(db, *, rng: random.Random, days: int, runs_per_day: int, gate_runs: int) -> dict[str, int]:
    creator = db.scalar(select(User).where(User.email == "creator@example.com")) or db.scalar(select(User))
    # Decisions, thumbs and gate runs are filed under the PM account (ADR-0051)
    # when the migration has seeded it, so the board's audit columns read the
    # way they will in use; the V2 admin is the fallback on an older database.
    admin = (
        db.scalar(select(User).where(User.email == "pm@example.com"))
        or db.scalar(select(User).where(User.email == "admin@example.com"))
        or creator
    )
    if creator is None:
        raise SystemExit("Không có user nào trong DB — chạy `alembic upgrade head` trước")
    now = datetime.now(UTC).replace(tzinfo=None)
    start = now - timedelta(days=days)
    counts = {
        "agents": 0,
        "versions": 0,
        "run_costs": 0,
        "agent_runs": 0,
        "run_approvals": 0,
        "quality_scores": 0,
        "gate_runs": 0,
    }

    # Agents, versions, deployments — all created_at back-dated so the
    # activation funnel reads as a history rather than a burst.
    cast: list[tuple[Story, Agent, list[AgentVersion]]] = []
    for story in STORIES:
        agent = Agent(
            owner_id=creator.id,
            name=f"{SIM_AGENT_PREFIX}{story.name}",
            created_at=start - timedelta(days=rng.randint(3, 20)),
        )
        db.add(agent)
        db.flush()
        versions = []
        for i in range(story.versions):
            last = i == story.versions - 1
            status = (
                VersionStatus.APPROVED
                if story.published and last
                else (VersionStatus.APPROVED if story.published else VersionStatus.WORKFLOW_CONFIRMED)
            )
            version = AgentVersion(
                agent_id=agent.id,
                workflow=_graph_for(story),
                status=status,
                created_at=start + timedelta(days=int(days * i / max(story.versions, 1))) - timedelta(days=2),
            )
            db.add(version)
            db.flush()
            versions.append(version)
            # Spec-lint scores for the version (code tier): the draft had a few
            # problems before confirm, none at confirm.
            problems_before = rng.randint(0, 3)
            for moment, problems in (("save", problems_before), ("save", max(problems_before - 1, 0)), ("confirm", 0)):
                db.add_all(
                    [
                        QualityScore(
                            agent_id=agent.id,
                            version_id=version.id,
                            name="spec_lint_pass",
                            value=0.0 if problems else 1.0,
                            comment=f"{moment}",
                            source=ScoreSource.CODE,
                            created_at=version.created_at,
                        ),
                        QualityScore(
                            agent_id=agent.id,
                            version_id=version.id,
                            name="graph_problems",
                            value=float(problems),
                            source=ScoreSource.CODE,
                            created_at=version.created_at,
                        ),
                    ]
                )
                counts["quality_scores"] += 2
        if story.published:
            db.add(
                Deployment(
                    agent_id=agent.id,
                    agent_version_id=versions[-1].id,
                    status=DeploymentStatus.ACTIVE,
                    access_mode=DeploymentAccessMode.PRIVATE,
                    api_key_hash="sim-not-a-real-key",
                    created_by=creator.id,
                    created_at=versions[0].created_at,
                )
            )
        cast.append((story, agent, versions))
        counts["agents"] += 1
        counts["versions"] += len(versions)
    db.flush()

    # Runs. Volume per day is split across agents by `daily_volume`; weekends
    # are quieter for real traffic than for the creator's own testing, which is
    # part of why the two populations are worth keeping apart.
    for day in range(days):
        day_start = start + timedelta(days=day)
        weekend = day_start.weekday() >= 5
        for story, agent, versions in cast:
            n = int(runs_per_day * story.daily_volume * (0.45 if weekend else 1.0) + rng.random())
            for _ in range(n):
                trigger = _pick(rng, story.trigger_mix)
                if weekend and trigger == "manual" and rng.random() < 0.5:
                    continue
                # Which version served: the newest for most of the window, the
                # previous one early on — so "quality by version" has both.
                version = versions[-1]
                if len(versions) > 1 and day < days * 0.45 and rng.random() < 0.8:
                    version = versions[-2]
                is_latest = version is versions[-1]
                p_success = story.success[trigger]
                if not is_latest:
                    p_success = max(0.05, min(0.99, 1 - (1 - p_success) * story.previous_version_failure_multiplier))
                success = rng.random() < p_success
                outcome = RunOutcome.SUCCESS.value if success else _pick(rng, FAILURE_TAXONOMY)
                paused = story.approval_share > 0 and trigger != "chat_test" and rng.random() < story.approval_share
                if paused and success and rng.random() < 0.15:
                    outcome = RunOutcome.NEEDS_APPROVAL.value  # still waiting at window end
                sandbox = trigger == "chat_test"
                namespace = ThreadNamespace.TRIAL if sandbox else ThreadNamespace.PRODUCTION
                model = _pick(rng, dict(MODELS))
                cost = max(0.0005, rng.gauss(story.cost_mean, story.cost_sd)) * (1.0 if success else 0.7)
                if model == "gpt-4o":
                    cost *= 2.2
                tokens_in = int(cost / 0.0000025) if model != "gpt-4o" else int(cost / 0.00001)
                tokens_out = int(tokens_in * rng.uniform(0.15, 0.4))
                at = day_start + timedelta(
                    hours=rng.randint(8, 19),
                    minutes=rng.randint(0, 59),
                    seconds=rng.randint(0, 59),
                )
                conversation_id = _sim_id(rng)
                error = (
                    None
                    if outcome
                    in (RunOutcome.SUCCESS.value, RunOutcome.NEEDS_APPROVAL.value, RunOutcome.BUDGET_STOPPED.value)
                    else ERROR_SENTENCES[outcome]
                )
                run = RunCost(
                    agent_id=agent.id,
                    version_id=version.id,
                    conversation_id=conversation_id,
                    sandbox=sandbox,
                    namespace=namespace,
                    input_tokens=tokens_in,
                    output_tokens=tokens_out,
                    cost_usd=round(cost, 6),
                    latency_seconds=round(
                        max(0.8, rng.gauss(9.0 if outcome != RunOutcome.BUDGET_STOPPED.value else 40.0, 4.0)), 2
                    ),
                    usage_by_model={
                        model: {"input_tokens": tokens_in, "output_tokens": tokens_out, "usd": round(cost, 6)}
                    },
                    trace_id=None,
                    error=error,
                    trigger=trigger,
                    outcome=outcome,
                    created_at=at,
                )
                db.add(run)
                db.flush()
                counts["run_costs"] += 1
                ledger_row = record_agent_run(
                    db,
                    AgentRunRecord(
                        pipeline="v2",
                        trace_id=f"sim-trace-{conversation_id[4:]}",
                        agent_key=str(agent.id),
                        version_key=str(version.id),
                        agent_name=agent.name,
                        conversation_id=conversation_id,
                        caller=None if sandbox else f"u{rng.choice([creator.id, admin.id])}",
                        trigger=trigger,
                        input_message=rng.choice(SIM_QUESTIONS),
                        transcript=_sim_transcript(rng, story, outcome=outcome, success=success),
                        error=error,
                        ending=RunOutcome(outcome)
                        if outcome in (RunOutcome.NEEDS_APPROVAL.value, RunOutcome.BUDGET_STOPPED.value)
                        else None,
                        cost_usd=run.cost_usd,
                        latency_seconds=run.latency_seconds,
                        input_tokens=tokens_in,
                        output_tokens=tokens_out,
                        usage_by_model=run.usage_by_model,
                        agent_id=agent.id,
                        version_id=version.id,
                        run_cost_id=run.id,
                        simulated=True,
                    ),
                )
                ledger_row.created_at = at
                counts["agent_runs"] += 1

                if paused:
                    still_pending = outcome == RunOutcome.NEEDS_APPROVAL.value
                    wait_minutes = rng.choice([3, 8, 15, 25, 40, 55, 90, 180, 400, 1440]) * rng.uniform(0.6, 1.4)
                    approved = rng.random() < 0.86
                    approval = RunApproval(
                        run_id=f"run-{conversation_id}",
                        node_id="duyet",
                        occurrence_index=0,
                        version_id=version.id,
                        agent_id=agent.id,
                        conversation_id=conversation_id,
                        namespace=namespace.value,
                        sandbox=sandbox,
                        trace_id=f"sim-trace-{conversation_id[4:]}",
                        approver_role="sales_manager",
                        message=rng.choice(APPROVAL_MESSAGES).format(n=rng.randint(2, 40)),
                        input_message="Nhắc những khách quá hạn trên 30 ngày giúp tôi",
                        status=RunApprovalStatus.PENDING
                        if still_pending
                        else (RunApprovalStatus.APPROVED if approved else RunApprovalStatus.REJECTED),
                        decided_by=None if still_pending else admin.id,
                        decided_at=None if still_pending else at + timedelta(minutes=wait_minutes),
                        reason=None if (still_pending or approved) else "Danh sách có khách đã thanh toán — không gửi",
                        elapsed_seconds=round(rng.uniform(3, 20), 1),
                        created_at=at,
                    )
                    db.add(approval)
                    counts["run_approvals"] += 1

                # Judge tier on a sample of production runs; human tier on a smaller one.
                if not sandbox and rng.random() < 0.22:
                    centre = story.faithfulness[0 if is_latest else 1]
                    faith = min(5.0, max(1.0, rng.gauss(centre if success else centre - 1.2, 0.6)))
                    grounded = faith >= 3.5
                    relevant = rng.random() < (0.92 if success else 0.55)
                    for name, ok, why in (
                        (
                            "judge:source_groundedness",
                            grounded,
                            "mọi mệnh đề đều có nguồn đỡ" if grounded else "một mệnh đề không tìm thấy trong nguồn",
                        ),
                        ("judge:relevance", relevant, "đúng việc được hỏi" if relevant else "lạc sang việc khác"),
                        (
                            "judge_pass",
                            grounded and relevant,
                            f"source_groundedness: {'đạt' if grounded else 'trượt'}; relevance: {'đạt' if relevant else 'trượt'}",
                        ),
                    ):
                        db.add(
                            QualityScore(
                                agent_id=agent.id,
                                version_id=version.id,
                                run_cost_id=run.id,
                                run_id=ledger_row.id,
                                name=name,
                                value=1.0 if ok else 0.0,
                                comment=why,
                                source=ScoreSource.JUDGE,
                                created_at=at + timedelta(minutes=2),
                            )
                        )
                    counts["quality_scores"] += 3
                if not sandbox and rng.random() < 0.11:
                    up = rng.random() < (0.82 if success else 0.15)
                    note = None
                    if rng.random() < 0.35:
                        candidates = [text for ok, text in THUMBS_NOTES if ok == up]
                        note = rng.choice(candidates) if candidates else None
                    db.add(
                        QualityScore(
                            agent_id=agent.id,
                            version_id=version.id,
                            run_cost_id=run.id,
                            run_id=ledger_row.id,
                            name="thumbs_up",
                            value=1.0 if up else 0.0,
                            comment=note,
                            source=ScoreSource.HUMAN,
                            given_by=rng.choice([creator.id, admin.id]),
                            created_at=at + timedelta(minutes=rng.randint(1, 30)),
                        )
                    )
                    counts["quality_scores"] += 1
    db.commit()

    # The gate, a few times, so the history widget has a spread — and so the
    # deck's unsatisfiable cases are on the board from day one.
    for i in range(gate_runs):
        row = gate.run_and_record_sync(
            db, mode=gate.SIMULATED, triggered_by=admin.id, fidelity=gate.DEFAULT_FIDELITY, seed=rng.randint(1, 10_000)
        )
        row.created_at = now - timedelta(days=gate_runs - i, hours=rng.randint(0, 8))
        db.commit()
        counts["gate_runs"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="chỉ xoá dữ liệu giả lập, không seed lại")
    parser.add_argument("--seed", type=int, default=65, help="seed RNG (mặc định 65)")
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--runs-per-day", type=int, default=60, help="tổng run/ngày trước khi chia cho các agent")
    parser.add_argument("--gate-runs", type=int, default=3, help="số lần chạy gate giả lập ghi vào lịch sử")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        removed = reset(db)
        print("Đã xoá dữ liệu giả lập cũ:", removed)
        if args.reset:
            return 0
        rng = random.Random(args.seed)
        written = seed(db, rng=rng, days=args.days, runs_per_day=args.runs_per_day, gate_runs=args.gate_runs)
        print("Đã seed:", written)
        print(
            f"Mọi agent tên bắt đầu bằng {SIM_AGENT_PREFIX!r}, mọi run có conversation_id bắt đầu bằng {SIM_PREFIX!r}."
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
