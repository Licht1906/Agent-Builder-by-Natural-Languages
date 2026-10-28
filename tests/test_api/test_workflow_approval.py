"""Human Approval pause/resume, and the `action_with_approval` demo end to end — #47.

The claim this file exists to pin down is the one the whole approval gate rests
on: **the side effect does not happen until a person says so.** So the tests do
not merely assert that a run paused — they assert that the `MCP` node produced
nothing at all on the first leg, that it produced something on the second only
after a real decision was recorded, and that a rejection routes the run away from
it entirely. A test that only checked for an `awaiting_approval` event would pass
against a runtime that emitted the event and then sent the email anyway.

The second claim is that a paused trial is **not** a finished trial. A run that
stops in front of somebody has not exercised the branch the approval guards, so
it stores no `sandbox_run` and earns no `sandbox_passed` — and when it does
finish, the transcript it stores spans both legs, because one run is one piece of
evidence.

Everything is observed through HTTP like the rest of the suite, with the
product's own two seams aimed at: the model factory, and a real Knowledge
Base the test ingested through the product's own pipeline.
"""

from collections.abc import Callable
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    Agent,
    AgentVersion,
    Deployment,
    DeploymentAccessMode,
    RunApproval,
    RunApprovalStatus,
    SandboxRunStatus,
    User,
    VersionStatus,
)
from src.workflow.tools import GMAIL
from tests.conftest import (
    DEPLOYMENT_API_KEY,
    FakeClock,
    ScriptedRun,
    auth_header,
    discard_agent,
    hash_api_key,
    parse_sse,
    parse_sse_stream,
    shelf_with,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Scenery — the second demo use case, wired exactly as the template draws it
# --------------------------------------------------------------------------- #

HANDBOOK = """Chính sách đổi trả

Khách được đổi trả trong vòng 30 ngày kể từ ngày nhận hàng.
"""

HANDBOOK_REF = "so-tay.md"

SPEC: dict[str, Any] = {
    "goal": "Xét khiếu nại rồi gửi thư trả lời khi được duyệt",
    "users": ["Nhân viên chăm sóc khách hàng"],
    "data": [
        {"name": "Sổ tay", "kind": "uploaded_file", "ref": HANDBOOK_REF},
        {"name": "Hộp thư hỗ trợ", "kind": "connected_account", "ref": "conn://gmail-1"},
    ],
    "actions": ["Tra sổ tay", "Gửi thư trả lời cho khách"],
    "expected_outcome": ["Một thư trả lời đã gửi"],
    "success_criteria": [{"text": "Thư nêu đúng chính sách", "category": "correctness"}],
    "prohibitions": ["Không tự bịa chính sách"],
}


def action_with_approval(
    shelf: str = "", *, budget: dict[str, Any] | None = None, action: str = "send_email"
) -> dict[str, Any]:
    """`KnowledgeSearch → AgentLlm → IfElse → HumanApproval → MCP → AgentLlm → End`.

    The template's own shape (`src.workflow.templates`), configured — not a
    reduced stand-in, because the claim being made is about *this* demo running.
    """
    return {
        "start_node_id": "search",
        "nodes": [
            {"id": "search", "type": "knowledge_search", "config": {"source_ids": [shelf]}},
            {
                "id": "assess",
                "type": "agent_llm",
                "config": {"prompt": "Xét xem khiếu nại này có đủ điều kiện không.", "model": "gpt-4o-mini"},
            },
            {"id": "gate", "type": "if_else", "config": {"condition": "Khách còn trong hạn 30 ngày"}},
            {
                "id": "approval",
                "type": "human_approval",
                "config": {"approver_role": "manager", "message_template": "Duyệt gửi thư cho khách?"},
            },
            {"id": "act", "type": "mcp", "config": {"tool_id": GMAIL, "action": action}},
            {
                "id": "report",
                "type": "agent_llm",
                "config": {"prompt": "Báo lại cho người dùng chuyện gì đã xảy ra.", "model": "gpt-4o-mini"},
            },
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "search", "source_port": "found", "target_node_id": "assess"},
            {"id": "e2", "source_node_id": "assess", "source_port": "done", "target_node_id": "gate"},
            {"id": "e3", "source_node_id": "gate", "source_port": "true", "target_node_id": "approval"},
            {"id": "e4", "source_node_id": "gate", "source_port": "false", "target_node_id": "report"},
            {"id": "e5", "source_node_id": "approval", "source_port": "approved", "target_node_id": "act"},
            {"id": "e6", "source_node_id": "approval", "source_port": "rejected", "target_node_id": "report"},
            {"id": "e7", "source_node_id": "act", "source_port": "done", "target_node_id": "report"},
            {"id": "e8", "source_node_id": "report", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {"budget": budget} if budget else {},
    }


def counting_loop(*, max_iterations: int = 2) -> dict[str, Any]:
    """`While → AgentLlm → (back) / End` — the only shape that can hit an iteration cap."""
    return {
        "start_node_id": "loop",
        "nodes": [
            {
                "id": "loop",
                "type": "while",
                "config": {"condition": "Còn khiếu nại chưa xử lý", "max_iterations": max_iterations},
            },
            {
                "id": "handle",
                "type": "agent_llm",
                "config": {"prompt": "Xử lý một khiếu nại.", "model": "gpt-4o-mini"},
            },
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "loop", "source_port": "loop", "target_node_id": "handle"},
            {"id": "e2", "source_node_id": "loop", "source_port": "exit", "target_node_id": "finish"},
            {"id": "e3", "source_node_id": "handle", "source_port": "done", "target_node_id": "loop"},
        ],
        "metadata": {},
    }


def nested_loops(*, outer: int = 2, inner: int = 1) -> dict[str, Any]:
    """A `While` inside a `While` — the shape that catches a loop counter nobody resets."""
    return {
        "start_node_id": "outer",
        "nodes": [
            {"id": "outer", "type": "while", "config": {"condition": "Còn lô chưa xử lý", "max_iterations": outer}},
            {"id": "inner", "type": "while", "config": {"condition": "Còn việc trong lô", "max_iterations": inner}},
            {"id": "work", "type": "agent_llm", "config": {"prompt": "Làm một việc.", "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "outer", "source_port": "loop", "target_node_id": "inner"},
            {"id": "e2", "source_node_id": "outer", "source_port": "exit", "target_node_id": "finish"},
            {"id": "e3", "source_node_id": "inner", "source_port": "loop", "target_node_id": "work"},
            {"id": "e4", "source_node_id": "inner", "source_port": "exit", "target_node_id": "outer"},
            {"id": "e5", "source_node_id": "work", "source_port": "done", "target_node_id": "inner"},
        ],
        "metadata": {},
    }


@pytest_asyncio.fixture
async def documents(client: AsyncClient, creator_token: str) -> str:
    """A real Knowledge Base carrying the handbook — see `test_workflow_run`."""
    return await shelf_with(client, creator_token, **{HANDBOOK_REF: HANDBOOK})


@pytest.fixture
def confirmed(
    solution_spec_version: Callable[..., AgentVersion], seeded_creator: User, documents: str
) -> Callable[..., AgentVersion]:
    """A version whose workflow has been confirmed — the stage a run starts from."""

    def _make(workflow: dict[str, Any] | None = None, *, owner: User | None = None) -> AgentVersion:
        return solution_spec_version(
            owner or seeded_creator,
            SPEC,
            status=VersionStatus.WORKFLOW_CONFIRMED,
            workflow=workflow if workflow is not None else action_with_approval(documents),
        )

    return _make


def answers(text: str) -> AIMessage:
    """One scripted model reply."""
    return AIMessage(content=text)


def decides(holds: bool, reason: str = "vì hồ sơ nói vậy") -> AIMessage:
    """What the platform's model says when asked to settle an `IfElse`/`While` condition."""
    return answers(f'{{"holds": {str(holds).lower()}, "reason": "{reason}"}}')


def fills_email() -> AIMessage:
    """The model filling `send_email`'s arguments from the conversation.

    Made by the `human_approval` node, before it stops (DS-08): the person
    approves the call itself, so it has to exist to be shown. The paused leg
    therefore ends with this call, and the resumed leg sends what it filled
    without asking the model again — its script is the `report` answer alone.
    """
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "send_email",
                "args": {"to": "khach@example.com", "subject": "Về khiếu nại", "body": "Đã xử lý."},
                "id": "fill-1",
                "type": "tool_call",
            }
        ],
    )


async def trial_workflow(client: AsyncClient, version: AgentVersion, message: str, token: str, **thread: Any) -> Any:
    """One sandbox run of a version's confirmed workflow."""
    return await client.post(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/workflow-sandbox-runs",
        json={"message": message, **thread},
        headers=auth_header(token),
    )


async def pending(client: AsyncClient, version: AgentVersion, token: str) -> list[dict[str, Any]]:
    """Everything this version's runs are currently waiting on a person for."""
    response = await client.get(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/workflow-approvals",
        headers=auth_header(token),
    )
    assert response.status_code == 200, response.text
    return list(response.json())


async def decide(
    client: AsyncClient,
    version: AgentVersion,
    approval_id: int,
    token: str,
    *,
    approved: bool,
    reason: str | None = None,
) -> Any:
    """Answer one pending approval and let the run it was holding up carry on."""
    return await client.post(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/workflow-approvals/{approval_id}",
        json={"approved": approved, "reason": reason},
        headers=auth_header(token),
    )


def mcp_calls(response: Any) -> list[dict[str, Any]]:
    """Every `MCP` action the run attempted — the lines that mean a side effect happened.

    Filtered by the connection's own name rather than by taking every `tool`
    line, because a `KnowledgeSearch` node reports on the same channel: asserting
    "no tools were called" would pass for the wrong reason on a graph whose
    search node had simply not run yet.
    """
    return [payload for payload in parse_sse(response.text, event="tool") if payload["tool"].startswith(GMAIL)]


def nodes_that_ran(response: Any) -> list[str]:
    """The ids of every node that reported completing, in order."""
    return [payload["node_id"] for payload in parse_sse(response.text, event="node")]


def event_names(response: Any) -> list[str]:
    """Every SSE event name in arrival order — how a test asks how a run *ended*."""
    return [name for name, _payload in parse_sse_stream(response.text)]


# --------------------------------------------------------------------------- #
# AC 3–5 — the approval really stops the run, and the action waits for a person
# --------------------------------------------------------------------------- #


async def test_the_run_stops_at_the_approval_and_the_action_does_not_happen(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    db_session: Session,
) -> None:
    """AC 3. A sandbox run pauses for real — no auto-approval, and no MCP call."""
    scripted_chat_model_factory([[answers("Khiếu nại hợp lệ.")], [decides(True)]])
    version = confirmed()

    response = await trial_workflow(client, version, "khách khiếu nại đổi trả", creator_token)

    assert response.status_code == 200, response.text
    ran = nodes_that_ran(response)
    assert ran == ["search", "assess", "gate"], ran
    # The two things that make this a pause rather than a finished run: the
    # action never reported, and no trial was stored to earn `sandbox_passed`.
    assert "awaiting_approval" in event_names(response)
    assert "sandbox_run" not in event_names(response)
    assert not mcp_calls(response)
    db_session.refresh(version)
    assert version.status is VersionStatus.WORKFLOW_CONFIRMED


async def test_a_pending_approval_is_listable_and_carries_what_a_reviewer_reads(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """AC 4. `run_approvals` is the queryable surface a checkpoint cannot be."""
    scripted_chat_model_factory([[answers("Khiếu nại hợp lệ.")], [decides(True)]])
    version = confirmed()
    await trial_workflow(client, version, "khách khiếu nại đổi trả", creator_token)

    waiting = await pending(client, version, creator_token)

    assert len(waiting) == 1, waiting
    assert waiting[0]["node_id"] == "approval"
    assert waiting[0]["approver_role"] == "manager"
    assert waiting[0]["occurrence_index"] == 0
    # The rendered template, plus the answer the reviewer is being asked about.
    assert "Duyệt gửi thư cho khách?" in waiting[0]["message"]
    assert "Khiếu nại hợp lệ." in waiting[0]["message"]


async def test_approving_lets_the_action_run_and_stores_one_transcript_for_the_whole_run(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    db_session: Session,
) -> None:
    """AC 5. The demo runs end to end: the MCP action fires only after a real decision."""
    scripted_chat_model_factory([[answers("Khiếu nại hợp lệ.")], [decides(True)], [fills_email()]])
    version = confirmed()
    await trial_workflow(client, version, "khách khiếu nại đổi trả", creator_token)
    waiting = await pending(client, version, creator_token)

    scripted_chat_model_factory([[answers("Đã gửi thư cho khách.")]])
    resumed = await decide(client, version, waiting[0]["approval_id"], creator_token, approved=True)

    assert resumed.status_code == 200, resumed.text
    assert nodes_that_ran(resumed) == ["approval", "act", "report", "finish"]
    # The action ran mocked, because `send_email` is `high` risk and a rehearsal
    # must not send a real email — and `simulated` says so on the line itself.
    called = mcp_calls(resumed)
    assert [payload["tool"] for payload in called] == [f"{GMAIL}.send_email"]
    assert called[0]["simulated"] is True

    record = parse_sse(resumed.text, event="sandbox_run")[0]
    assert record["status"] == SandboxRunStatus.SUCCEEDED.value
    stored = await client.get(
        f"/api/v1/agents/{version.agent_id}/sandbox-runs/{record['run_id']}",
        headers=auth_header(creator_token),
    )
    # One transcript spanning both legs: the nodes before the pause and after it.
    ran = [line["node_id"] for line in stored.json()["transcript"] if line["kind"] == "node"]
    assert ran == ["search", "assess", "gate", "approval", "act", "report", "finish"]
    db_session.refresh(version)
    assert version.status is VersionStatus.SANDBOX_PASSED


async def test_rejecting_routes_the_run_away_from_the_action_and_says_why(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    db_session: Session,
) -> None:
    """AC 5. A `no` skips the side effect, and the reason reaches the node after the gate."""
    scripted_chat_model_factory([[answers("Khiếu nại hợp lệ.")], [decides(True)]])
    version = confirmed()
    await trial_workflow(client, version, "khách khiếu nại đổi trả", creator_token)
    waiting = await pending(client, version, creator_token)

    _ids, models = scripted_chat_model_factory([[answers("Rất tiếc, yêu cầu không được duyệt.")]])
    resumed = await decide(
        client, version, waiting[0]["approval_id"], creator_token, approved=False, reason="Quá hạn đổi trả"
    )

    assert nodes_that_ran(resumed) == ["approval", "report", "finish"], resumed.text
    assert not mcp_calls(resumed)
    # ADR-0024 §6: the branch after the gate can act on the decision, because the
    # reason was written into the state it reads — not merely into an audit row.
    conversation = "\n".join(str(message.content) for message in models[0].received_message_batches[0])
    assert "Quá hạn đổi trả" in conversation

    row = db_session.scalars(select(RunApproval).where(RunApproval.version_id == version.id)).one()
    assert row.status is RunApprovalStatus.REJECTED
    assert row.reason == "Quá hạn đổi trả"


async def test_a_rejection_with_no_reason_is_refused_and_the_run_stays_paused(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """A `no` nobody explained is not something the `rejected` branch can be written against."""
    scripted_chat_model_factory([[answers("Khiếu nại hợp lệ.")], [decides(True)]])
    version = confirmed()
    await trial_workflow(client, version, "khách khiếu nại đổi trả", creator_token)
    waiting = await pending(client, version, creator_token)

    refused = await decide(client, version, waiting[0]["approval_id"], creator_token, approved=False)

    assert refused.status_code == 409, refused.text
    assert len(await pending(client, version, creator_token)) == 1


async def test_an_approval_cannot_be_decided_twice(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """The second decider is told the run has moved on rather than resuming it again."""
    scripted_chat_model_factory([[answers("Khiếu nại hợp lệ.")], [decides(True)], [fills_email()]])
    version = confirmed()
    await trial_workflow(client, version, "khách khiếu nại đổi trả", creator_token)
    waiting = await pending(client, version, creator_token)
    scripted_chat_model_factory([[answers("Đã gửi thư.")]])
    await decide(client, version, waiting[0]["approval_id"], creator_token, approved=True)

    again = await decide(client, version, waiting[0]["approval_id"], creator_token, approved=True)

    assert again.status_code == 409, again.text


# --------------------------------------------------------------------------- #
# AC 6 — waiting on a person does not spend the run's clock
# --------------------------------------------------------------------------- #


async def test_time_spent_waiting_for_a_decision_does_not_count_against_the_wall_clock(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    fake_clock: FakeClock,
) -> None:
    """AC 6. A slow reviewer must not be able to fail a run that is behaving perfectly.

    The ceiling is one second and the reviewer takes longer than that. Measured
    from the run's start the budget would be long gone; measured as ADR-0024 §7
    requires — elapsed *running* time — the resumed leg has barely spent
    anything, and the run finishes.

    `fake_clock` stands in for the reviewer's real minutes (#58): `BudgetLedger`
    is driven by `get_clock`, so `advance` moves exactly the run's own clock, and
    only that clock, past the ceiling. A real `asyncio.sleep` here proved the
    same claim only when the machine running the suite was otherwise idle — under
    a loaded run, the two `agent_llm` steps' own real wall time could alone push
    the ledger past a one-second ceiling before the reviewer was ever modelled,
    failing the assertion below for a reason that has nothing to do with what it
    tests. A clock only the test moves is what keeps the assertion about the one
    thing ADR-0024 §7 promises, and about nothing the test suite's load did.
    """
    scripted_chat_model_factory([[answers("Khiếu nại hợp lệ.")], [decides(True)], [fills_email()]])
    version = confirmed(action_with_approval(documents, budget={"max_total_tokens": 100_000, "max_wall_seconds": 1}))
    await trial_workflow(client, version, "khách khiếu nại đổi trả", creator_token)
    waiting = await pending(client, version, creator_token)

    fake_clock.advance(1.2)
    scripted_chat_model_factory([[answers("Đã gửi thư cho khách.")]])
    resumed = await decide(client, version, waiting[0]["approval_id"], creator_token, approved=True)

    assert "budget_exceeded" not in event_names(resumed), resumed.text
    assert nodes_that_ran(resumed) == ["approval", "act", "report", "finish"]


# --------------------------------------------------------------------------- #
# AC 2 — branch and loop routing
# --------------------------------------------------------------------------- #


async def test_if_else_routes_down_the_branch_the_condition_chose(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """AC 2. A false condition skips the approval *and* the action it was guarding."""
    scripted_chat_model_factory(
        [[answers("Khiếu nại đã quá hạn.")], [decides(False, "quá 30 ngày")], [answers("Rất tiếc, đã quá hạn.")]]
    )
    version = confirmed()

    response = await trial_workflow(client, version, "khách khiếu nại đổi trả", creator_token)

    assert nodes_that_ran(response) == ["search", "assess", "gate", "report", "finish"], response.text
    assert not mcp_calls(response)
    gate = next(payload for payload in parse_sse(response.text, event="node") if payload["node_id"] == "gate")
    assert gate["port_taken"] == "false"


async def test_a_loop_stops_at_its_cap_and_the_transcript_says_the_cap_did_it(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """AC 2 / ADR-0026 §2–4. The cap forces `exit`, the run finishes, and nobody is misled.

    A condition that is always true is what makes the cap the only thing that can
    stop the loop. `forced_exit` is what stops a reader taking the final `exit`
    as evidence the loop's own condition can ever go false (ADR-0026 §5).
    """
    scripted_chat_model_factory([[decides(True)], [answers("Xong một cái.")]] * 2 + [[decides(True)]])
    version = confirmed(counting_loop(max_iterations=2))

    response = await trial_workflow(client, version, "xử lý khiếu nại tồn", creator_token)

    assert nodes_that_ran(response) == ["loop", "handle", "loop", "handle", "loop", "finish"], response.text
    exits = [payload for payload in parse_sse(response.text, event="node") if payload["node_id"] == "loop"]
    assert [payload["port_taken"] for payload in exits] == ["loop", "loop", "exit"]
    assert exits[-1]["forced_exit"] is True
    # Every pass through the body is its own line, tagged with which pass it was.
    passes = [
        payload["iteration"] for payload in parse_sse(response.text, event="node") if payload["node_id"] == "handle"
    ]
    assert passes == [1, 2]


async def test_an_inner_loop_gets_its_whole_ceiling_back_on_every_outer_pass(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """`max_iterations` is a ceiling *per entry*, not a ration for the whole run.

    An inner loop whose count survived the outer loop's first pass would sit at
    its cap before the second pass had executed anything: it would force-exit
    immediately, the body would run once instead of twice, and the transcript
    would record a ceiling stopping a loop that never ran. Two passes of the
    outer loop and one of the inner means `work` must run exactly twice.
    """
    scripted_chat_model_factory([[decides(True)], [decides(True)], [answers("Xong.")]] * 2)
    version = confirmed(nested_loops(outer=2, inner=1))

    response = await trial_workflow(client, version, "xử lý các lô tồn", creator_token)

    assert nodes_that_ran(response).count("work") == 2, response.text


# ── Deciding a run that paused on a *published* agent ─────────────────────────


async def test_a_deployed_agents_paused_run_can_be_decided_and_resumed(
    client: AsyncClient,
    creator_token: str,
    seeded_creator: User,
    db_session: Session,
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """The regression: an `approved` version's pause had no door back in.

    Deciding always validated with `runnable_workflow`, whose set stops at
    `pending_approval` because it is the creator's door onto their own
    *unapproved* draft. A published agent runs through the Deployment instead,
    so a run that paused there was refused on resume with a sentence about
    trialling an unfinished workflow — shown to somebody approving a live send.
    """
    agent = Agent(owner_id=seeded_creator.id, name="Agent da publish")
    db_session.add(agent)
    db_session.flush()
    version = AgentVersion(
        agent_id=agent.id,
        spec=None,
        solution_spec=SPEC,
        workflow=action_with_approval(documents),
        status=VersionStatus.APPROVED,
    )
    db_session.add(version)
    db_session.flush()
    # `deployed_version_id` is read through the Deployment, so publishing means
    # writing one — which is also what makes this test about the real condition
    # rather than about a field somebody set.
    db_session.add(
        Deployment(
            agent_id=agent.id,
            agent_version_id=version.id,
            access_mode=DeploymentAccessMode.PRIVATE,
            api_key_hash=hash_api_key(DEPLOYMENT_API_KEY),
            created_by=seeded_creator.id,
        )
    )
    db_session.commit()
    db_session.refresh(agent)
    assert agent.deployed_version_id == version.id

    # The pause has to be made through the door a published agent actually
    # uses: the trial door refuses an `approved` version outright, which is the
    # very asymmetry this test is about.
    scripted_chat_model_factory([[answers("Khiếu nại hợp lệ.")], [decides(True)], [fills_email()]])
    started = await client.post(
        f"/api/v1/agents/{agent.id}/deployed-workflow-chat",
        json={"message": "khách khiếu nại đổi trả"},
        headers=auth_header(creator_token),
    )
    assert started.status_code == 200, started.text
    waiting = await pending(client, version, creator_token)
    assert waiting, "the run did not pause — the rest of this test proves nothing"

    scripted_chat_model_factory([[answers("Đã gửi thư cho khách.")]])
    try:
        resumed = await decide(client, version, waiting[0]["approval_id"], creator_token, approved=True)

        assert resumed.status_code == 200, resumed.text
        assert "approved" not in resumed.text or "phải xác nhận workflow" not in resumed.text
        assert nodes_that_ran(resumed) == ["approval", "act", "report", "finish"]
    finally:
        # Built by hand rather than through a fixture, so torn down by hand too:
        # the approval row cascades off the version, and the next file's
        # "no RunApproval left" assertion is about *its* runs, not this one's.
        discard_agent(db_session, agent)


async def test_a_reopened_conversation_tells_speech_from_what_the_run_did(
    client: AsyncClient,
    creator_token: str,
    seeded_creator: User,
    db_session: Session,
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """What the chat sidebar reads back carries the same labels the live stream had.

    Reported from the field: reopening a conversation showed the tool's raw
    `{"message_id": …}` and "Người duyệt đã đồng ý" as things the agent had
    said, each in its own bubble. The history was a flat list of messages with
    nothing to tell an `agent_llm` line from an `mcp` note, so the screen could
    only draw them all the same way. Now each line says which node type wrote
    it (and, for a tool, which tool and whether it succeeded), and a paused
    conversation says what it is waiting on.
    """
    agent = Agent(owner_id=seeded_creator.id, name="Agent da publish")
    db_session.add(agent)
    db_session.flush()
    version = AgentVersion(
        agent_id=agent.id,
        spec=None,
        solution_spec=SPEC,
        workflow=action_with_approval(documents),
        status=VersionStatus.APPROVED,
    )
    db_session.add(version)
    db_session.flush()
    db_session.add(
        Deployment(
            agent_id=agent.id,
            agent_version_id=version.id,
            access_mode=DeploymentAccessMode.PRIVATE,
            api_key_hash=hash_api_key(DEPLOYMENT_API_KEY),
            created_by=seeded_creator.id,
        )
    )
    db_session.commit()
    conversation = "hoi-thoai-mo-lai"
    history = f"/api/v1/agents/{agent.id}/conversations/{conversation}/messages"

    try:
        scripted_chat_model_factory([[answers("Khiếu nại hợp lệ.")], [decides(True)], [fills_email()]])
        started = await client.post(
            f"/api/v1/agents/{agent.id}/deployed-workflow-chat",
            json={"message": "khách khiếu nại đổi trả", "conversation_id": conversation},
            headers=auth_header(creator_token),
        )
        assert started.status_code == 200, started.text
        waiting = await pending(client, version, creator_token)
        assert waiting

        # Paused: the history says so, with the row the buttons need.
        paused = await client.get(history, headers=auth_header(creator_token))
        assert paused.status_code == 200, paused.text
        assert paused.json()["pending_approval"] == {
            "approval_id": waiting[0]["approval_id"],
            "node_id": "approval",
            "approver_role": waiting[0]["approver_role"],
            "message": waiting[0]["message"],
            "actions": waiting[0]["actions"],
        }
        # DS-08: the card shows the call itself, as it will be made.
        [shown] = waiting[0]["actions"]
        assert (shown["node_id"], shown["tool"]) == ("act", f"{GMAIL}.send_email")
        # Without `to`: the user never typed that address, so the fill's guess at
        # it is dropped (`ToolAction.verbatim`) — and the card says so by not
        # showing a recipient, rather than showing one that will not be sent.
        assert shown["args"] == {"subject": "Về khiếu nại", "body": "Đã xử lý."}

        scripted_chat_model_factory([[answers("Đã gửi thư cho khách.")]])
        resumed = await decide(client, version, waiting[0]["approval_id"], creator_token, approved=True)
        assert resumed.status_code == 200, resumed.text

        # Finished: every agent line is labelled, and nothing is waiting. The
        # tool line says `ok: False` because this is the production door and no
        # Google account is connected here — which is exactly the fact a
        # reopened conversation must be able to show as a failed chip rather
        # than as a sentence the agent said.
        read = await client.get(history, headers=auth_header(creator_token))
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["pending_approval"] is None
        labelled = [(turn["role"], turn["node_type"], turn["tool"], turn["ok"]) for turn in body["turns"]]
        assert labelled == [
            ("user", None, None, None),
            ("agent", "agent_llm", None, None),
            ("agent", "human_approval", None, True),
            ("agent", "mcp", f"{GMAIL}.send_email", False),
            ("agent", "agent_llm", None, None),
        ]
        assert body["turns"][3]["content"].startswith("Không thực hiện được hành động")
        assert body["turns"][-1]["content"] == "Đã gửi thư cho khách."
    finally:
        discard_agent(db_session, agent)
