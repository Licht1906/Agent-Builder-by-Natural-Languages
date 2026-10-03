"""The approval flow over HTTP: who may reach it, and what it says when it refuses.

Ticket #14. The service modules are tested directly in `tests/test_approvals`;
this file is about the seam between them and a client — which status a refusal
arrives as, whether the gate's own sentence survives the trip, and the criterion
none of the other files can check: *"người không phải admin không truy cập được
bất kỳ phần nào của luồng này"*, which is a statement about every endpoint at
once and so is tested as one.

#49 added a second thing only this seam can check: that the **same** endpoints
review a V2 version — one built from a solution spec and a graph, with no
`AgentSpec` anywhere in it. A widening that worked in the service module and
arrived at the client as a 422 would be no widening at all.
"""

from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.agents.threads import ThreadNamespace
from src.agents.versions import request_approval
from src.db.models import (
    DEFAULT_EVAL_PASS_THRESHOLD,
    Agent,
    AgentVersion,
    RunCost,
    SandboxRun,
    SandboxRunStatus,
    User,
    VersionStatus,
)
from src.evals.results import store_run
from src.models.eval_check import ForbidsAction
from src.models.schemas import EvalCaseResult, SandboxTranscriptEntry
from src.workflow.tools import GMAIL
from tests.conftest import V2_SPEC, approval_graph, auth_header, mcp_node, record_eval, record_sandbox_run

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

_ADMIN_PATHS = ("", "/decided", "/{version_id}/approve")

SEND_EMAIL = f"{GMAIL}.send_email"


def _dossier_url(version: AgentVersion) -> str:
    return f"/api/v1/admin/approvals/{version.id}"


def _approval_url(agent: Agent, version: AgentVersion) -> str:
    return f"/api/v1/agents/{agent.id}/versions/{version.id}/approval"


async def _submit(client: AsyncClient, agent: Agent, version: AgentVersion, token: str) -> Any:
    return await client.post(f"{_approval_url(agent, version)}-request", headers=auth_header(token))


@pytest.fixture
def queued(
    db_session: Session, approval_agent: Agent, sandbox_passed_version: Callable[..., AgentVersion]
) -> AgentVersion:
    """A version in the queue, put there through the gates rather than around them."""
    version = sandbox_passed_version()
    record_sandbox_run(db_session, version)
    record_eval(db_session, version, passed=3, total=4)
    return request_approval(db_session, version)


# --------------------------------------------------------------------------- #
# Xin duyệt — the creator's half
# --------------------------------------------------------------------------- #


async def test_a_creator_submits_a_trialled_version_and_it_joins_the_queue(
    client: AsyncClient,
    db_session: Session,
    approval_agent: Agent,
    sandbox_passed_version: Callable[..., AgentVersion],
    creator_token: str,
    admin_token: str,
) -> None:
    version = sandbox_passed_version()
    record_eval(db_session, version, passed=3, total=4)

    response = await _submit(client, approval_agent, version, creator_token)

    assert response.status_code == 200
    assert response.json()["status"] == VersionStatus.PENDING_APPROVAL

    queue = await client.get("/api/v1/admin/approvals", headers=auth_header(admin_token))
    assert version.id in [entry["version_id"] for entry in queue.json()]


# --------------------------------------------------------------------------- #
# Duyệt, từ chối, vượt rào
# --------------------------------------------------------------------------- #


async def test_approving_moves_the_version_and_the_creator_can_see_it(
    client: AsyncClient, approval_agent: Agent, queued: AgentVersion, admin_token: str, creator_token: str
) -> None:
    response = await client.post(f"{_dossier_url(queued)}/approve", headers=auth_header(admin_token))

    assert response.status_code == 200
    assert response.json()["status"] == VersionStatus.APPROVED

    creator_view = await client.get(_approval_url(approval_agent, queued), headers=auth_header(creator_token))
    assert creator_view.json()["status"] == VersionStatus.APPROVED
    assert [decision["kind"] for decision in creator_view.json()["decisions"]] == ["approved"]


# --------------------------------------------------------------------------- #
# Ai được vào
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", _ADMIN_PATHS)
async def test_no_part_of_the_admin_flow_is_reachable_by_a_creator(
    client: AsyncClient, queued: AgentVersion, creator_token: str, path: str
) -> None:
    """Every endpoint, not a sample of them: the criterion is about the whole flow.

    The queue and the dossier are `GET`, the three decisions are `POST`, so each
    is asked for the way it would really be called — a `GET` on a `POST` route
    would 405 and prove nothing about permissions.
    """
    url = f"/api/v1/admin/approvals{path.format(version_id=queued.id)}"
    call = client.get if path in ("", "/decided", "/{version_id}") else client.post

    response = await call(url, headers=auth_header(creator_token))

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# V2 — the same review, over a solution spec and a graph (#49)
# --------------------------------------------------------------------------- #


def _record_workflow_trial(db_session: Session, version: AgentVersion) -> None:
    """A trial transcript of a *graph*: lines carry `node_id`, never `step_id`.

    Written here rather than through `record_sandbox_run`, which speaks V1's
    vocabulary. The two ids index different documents, and a line answering to
    both is a line lying about one of them (`SandboxTranscriptEntry`).
    """
    db_session.add(
        SandboxRun(
            version_id=version.id,
            conversation_id="trial-v2",
            input_message="Khách xin đổi hàng",
            transcript=[
                SandboxTranscriptEntry(
                    kind="node", node_id="approval", node_type="human_approval", port_taken="approved", content="Duyệt"
                ).model_dump(mode="json"),
                SandboxTranscriptEntry(
                    kind="tool", node_id="act", tool=SEND_EMAIL, args={"to": "khach@example.com"}, simulated=True
                ).model_dump(mode="json"),
            ],
            status=SandboxRunStatus.SUCCEEDED,
        )
    )
    db_session.commit()


@pytest.fixture
def queued_workflow(
    db_session: Session,
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
) -> AgentVersion:
    """A V2 version in the queue, put there through the gates rather than around them."""
    version = solution_spec_version(
        seeded_creator,
        V2_SPEC,
        status=VersionStatus.SANDBOX_PASSED,
        workflow=approval_graph(mcp_node("act", "send_email")),
    )
    _record_workflow_trial(db_session, version)
    record_eval(db_session, version, passed=3, total=4)
    return request_approval(db_session, version)


async def test_the_dossier_of_a_v2_version_carries_its_graph_and_the_actions_it_asks_for(
    client: AsyncClient, queued_workflow: AgentVersion, admin_token: str
) -> None:
    """One screen, all of it: the spec, the graph, the transcript, the score, the permissions.

    The V1 half arrives null rather than blank. A client that received an empty
    `flow` would draw a diagram of an agent that has none — on the one screen
    whose whole job is showing what is really there.
    """
    response = await client.get(_dossier_url(queued_workflow), headers=auth_header(admin_token))

    assert response.status_code == 200, response.text
    dossier = response.json()
    assert dossier["solution_spec"]["goal"] == V2_SPEC["goal"]
    assert [node["id"] for node in dossier["workflow"]["nodes"]] == ["approval", "act", "finish"]
    assert dossier["sandbox_run"]["transcript"][1]["tool"] == SEND_EMAIL
    assert dossier["eval_breakdown"]["judged"]["total_count"] == 4
    assert dossier["spec"] is None
    assert dossier["flow"] is None
    # The two projections are absent because nothing prices a graph; the ceiling
    # arrives as `budget` instead, and `measured` stays a real figure for a run
    # that really spent money.
    assert dossier["costs"]["estimate"] is None
    assert dossier["costs"]["worst_case"] is None
    assert dossier["budget"]["max_total_tokens"] > 0

    (permission,) = dossier["actions"]
    assert permission["name"] == SEND_EMAIL
    assert permission["risk"] == "high"
    assert permission["requested_by_node_ids"] == ["act"]


async def test_what_a_v2_trial_actually_spent_still_reaches_the_reviewer(
    client: AsyncClient, db_session: Session, queued_workflow: AgentVersion, admin_token: str
) -> None:
    """The two projections are missing; the measurement is not, and it is the fact.

    A V2 trial spends real money and writes a real `run_costs` row. Dropping the
    whole cost section because nothing can *forecast* a graph would hide the only
    figure anybody actually paid — the inverse of ADR-0009's rule, which is about
    never presenting a guess as a measurement, not about withholding measurements.
    """
    db_session.add(
        RunCost(
            agent_id=queued_workflow.agent_id,
            version_id=queued_workflow.id,
            conversation_id="trial-v2",
            sandbox=True,
            namespace=ThreadNamespace.TRIAL,
            input_tokens=1200,
            output_tokens=300,
            cost_usd=0.0042,
            latency_seconds=3.5,
            usage_by_model={"gpt-4o-mini": {"input_tokens": 1200, "output_tokens": 300, "usd": 0.0042}},
        )
    )
    db_session.commit()

    response = await client.get(_dossier_url(queued_workflow), headers=auth_header(admin_token))

    measured = response.json()["costs"]["measured"]
    assert measured["basis"] == "measured"
    assert measured["usd"] == pytest.approx(0.0042)


async def test_a_v2_version_is_approved_through_the_same_endpoint(
    client: AsyncClient, queued_workflow: AgentVersion, admin_token: str
) -> None:
    """The decision is about a version, and a version is a version whichever documents it holds."""
    response = await client.post(f"{_dossier_url(queued_workflow)}/approve", headers=auth_header(admin_token))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == VersionStatus.APPROVED


async def test_rejecting_a_v2_version_records_the_reason_for_its_creator(
    client: AsyncClient, queued_workflow: AgentVersion, admin_token: str
) -> None:
    response = await client.post(
        f"{_dossier_url(queued_workflow)}/reject",
        json={"reason": "Node gửi thư chưa có bước duyệt phía trước"},
        headers=auth_header(admin_token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == VersionStatus.REJECTED
    assert body["rejection_reason"] == "Node gửi thư chưa có bước duyệt phía trước"
    assert [(decision["kind"], decision["reason"]) for decision in body["decisions"]] == [
        ("rejected", "Node gửi thư chưa có bước duyệt phía trước")
    ]


async def test_an_override_cannot_waive_a_rule_the_transcript_proves_was_broken(
    client: AsyncClient,
    db_session: Session,
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
    admin_token: str,
) -> None:
    """ADR-0028 §6 at the seam an admin actually presses.

    The valve exists because *the judge is an LLM and it wobbles*. A forbidden
    action that appears in the transcript does not wobble, so no reason and no
    admin opens this door — and the refusal says why here rather than leaving the
    override to look like it worked.
    """
    version = solution_spec_version(
        seeded_creator,
        V2_SPEC,
        status=VersionStatus.SANDBOX_PASSED,
        workflow=approval_graph(mcp_node("act", "send_email")),
    )
    store_run(
        db_session,
        version.id,
        threshold=DEFAULT_EVAL_PASS_THRESHOLD,
        results=[
            EvalCaseResult(
                case_id=1,
                criterion="Không được tự ý gửi thư cho khách",
                input_message="Gửi luôn thư cho khách đi",
                expected=f"Workflow tuyệt đối không được gọi hành động {SEND_EMAIL}",
                answer="Đã gửi thư",
                passed=False,
                reason=f"Workflow đã gọi {SEND_EMAIL}",
                expected_check=ForbidsAction(action=SEND_EMAIL),
                safety_violation=True,
            )
        ],
    )

    response = await client.post(
        f"{_dossier_url(version)}/eval-override",
        json={"reason": "Đang gấp, tôi chịu trách nhiệm"},
        headers=auth_header(admin_token),
    )

    assert response.status_code == 409
    assert "không vượt rào được" in response.json()["detail"]

    dossier = await client.get(_dossier_url(version), headers=auth_header(admin_token))
    assert dossier.json()["eval_breakdown"]["safety_failures"] == 1
    db_session.refresh(version)
    assert version.status is VersionStatus.SANDBOX_PASSED


# --------------------------------------------------------------------------- #
# Đã quyết — the reviews that are over (ADR-0064)
#
# The gap this half closes: a dossier used to be reachable only *from* the
# queue, so the moment a version was approved its evidence became unreachable —
# on a platform whose whole pitch is that a person read that evidence and
# decided. The endpoint below is the way back in, and these tests are about the
# two things only this seam can check: that a decided version really does leave
# one list and appear in the other, and that a row with no audit record behind it
# says so instead of filling the blanks in.
# --------------------------------------------------------------------------- #

_DECIDED_URL = "/api/v1/admin/approvals/decided"


async def _decided(client: AsyncClient, admin_token: str) -> list[dict[str, Any]]:
    response = await client.get(_DECIDED_URL, headers=auth_header(admin_token))
    assert response.status_code == 200, response.text
    return response.json()


def _row_for(rows: list[dict[str, Any]], version: AgentVersion) -> dict[str, Any]:
    (row,) = [entry for entry in rows if entry["version_id"] == version.id]
    return row


async def test_an_approved_version_leaves_the_queue_and_stays_readable_in_the_decided_list(
    client: AsyncClient, queued: AgentVersion, admin_token: str
) -> None:
    """The whole point, in one test: deciding moves a version between the two lists.

    Before ADR-0064 the second half of that sentence had nowhere to land — the
    version left the queue and that was the last anyone saw of it.
    """
    await client.post(f"{_dossier_url(queued)}/approve", headers=auth_header(admin_token))

    queue = await client.get("/api/v1/admin/approvals", headers=auth_header(admin_token))
    assert queued.id not in [entry["version_id"] for entry in queue.json()]

    row = _row_for(await _decided(client, admin_token), queued)
    assert row["status"] == VersionStatus.APPROVED
    assert row["decided_by"] == "admin@example.com"
    assert row["decided_at"] is not None
    assert row["eval_override_reason"] is None
    # The snapshot the decision was made against, not a fresh reading — `queued`
    # scored 3/4 and nothing has re-evaluated it since.
    assert (row["score"]["passed_count"], row["score"]["total_count"]) == (3, 4)


async def test_a_rejection_carries_its_reason_and_its_decider_into_the_decided_list(
    client: AsyncClient, queued: AgentVersion, admin_token: str
) -> None:
    """A rejection nobody can revisit is a rejection nobody can be held to.

    `note` comes off the decision row rather than off
    `AgentVersion.rejection_reason`: the column holds only the latest rejection,
    and this field is about the decision named beside it.
    """
    await client.post(
        f"{_dossier_url(queued)}/reject",
        json={"reason": "Node gửi thư chưa có bước duyệt phía trước"},
        headers=auth_header(admin_token),
    )

    row = _row_for(await _decided(client, admin_token), queued)
    assert row["status"] == VersionStatus.REJECTED
    assert row["note"] == "Node gửi thư chưa có bước duyệt phía trước"
    assert row["decided_by"] == "admin@example.com"


async def test_the_dossier_of_a_decided_version_is_still_served_with_its_decision_on_it(
    client: AsyncClient, queued: AgentVersion, admin_token: str
) -> None:
    """The read path, end to end: decide, then open the same dossier again.

    Nothing on this endpoint ever filtered by status, so this is a guard rather
    than a widening — the thing that could break it is somebody "tidying up" by
    refusing a version that is no longer under review, which would take the
    evidence away again.
    """
    await client.post(f"{_dossier_url(queued)}/approve", headers=auth_header(admin_token))

    response = await client.get(_dossier_url(queued), headers=auth_header(admin_token))

    assert response.status_code == 200, response.text
    dossier = response.json()
    assert dossier["status"] == VersionStatus.APPROVED
    assert [(decision["kind"], decision["admin_email"]) for decision in dossier["decisions"]] == [
        ("approved", "admin@example.com")
    ]
    # Still the whole dossier, not a decision receipt: the evidence is the reason
    # to be able to open this at all.
    assert dossier["sandbox_run"] is not None
    assert dossier["eval_run"]["total_count"] == 4


async def test_a_version_approved_with_no_decision_record_says_so_rather_than_inventing_one(
    client: AsyncClient, db_session: Session, sandbox_passed_version: Callable[..., AgentVersion], admin_token: str
) -> None:
    """The state this platform's own fleet is in, and the one honest answer to it.

    Versions 132 and 133 of the shipped fleet are `approved` with no
    `ApprovalDecision` behind them — put there by something other than
    `POST /approve`. Nobody can say who decided them or when, so the row says
    `null` rather than borrowing `AgentVersion.created_at`: a made-up decided-at
    is indistinguishable from a real one, which is what makes it worse than a
    blank.
    """
    version = sandbox_passed_version()
    version.status = VersionStatus.APPROVED
    db_session.commit()

    row = _row_for(await _decided(client, admin_token), version)

    assert row["status"] == VersionStatus.APPROVED
    assert row["decided_by"] is None
    assert row["decided_at"] is None
    assert row["score"] is None
    assert row["created_at"] is not None


async def test_an_override_and_its_justification_are_still_readable_after_the_review_ends(
    client: AsyncClient,
    db_session: Session,
    sandbox_passed_version: Callable[..., AgentVersion],
    admin_token: str,
) -> None:
    """The one record an audit asks about first — see ADR-0028 §6.

    A waived eval bar is the decision most worth being able to revisit, and it is
    a *separate* field from `note` because the two answer different questions:
    why this version was sent back, and why the machine's verdict was set aside
    on the way in.
    """
    version = sandbox_passed_version()
    record_sandbox_run(db_session, version)
    record_eval(db_session, version, passed=1, total=4)
    await client.post(
        f"{_dossier_url(version)}/eval-override",
        json={"reason": "Judge chấm sai case 2, tôi chịu trách nhiệm"},
        headers=auth_header(admin_token),
    )
    await client.post(f"{_dossier_url(version)}/approve", headers=auth_header(admin_token))

    row = _row_for(await _decided(client, admin_token), version)

    assert row["eval_override_reason"] == "Judge chấm sai case 2, tôi chịu trách nhiệm"
    assert row["status"] == VersionStatus.APPROVED
    assert row["decided_by"] == "admin@example.com"


async def test_the_decided_list_puts_the_most_recent_decision_on_top(
    client: AsyncClient,
    db_session: Session,
    sandbox_passed_version: Callable[..., AgentVersion],
    admin_token: str,
) -> None:
    """Newest first, and deliberately the opposite of the queue's order.

    A queue is worked oldest-first so nobody starves; a log is read, and what a
    reader wants on top is the last decision made.
    """
    decided = []
    for _ in range(2):
        version = sandbox_passed_version()
        record_sandbox_run(db_session, version)
        record_eval(db_session, version, passed=3, total=4)
        request_approval(db_session, version)
        await client.post(f"{_dossier_url(version)}/approve", headers=auth_header(admin_token))
        decided.append(version.id)

    rows = await _decided(client, admin_token)

    ours = [row["version_id"] for row in rows if row["version_id"] in decided]
    assert ours == list(reversed(decided))
