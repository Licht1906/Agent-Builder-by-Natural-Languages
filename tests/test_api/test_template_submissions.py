""" "Save as template" and the admin review queue over HTTP — #53, ADR-0025 §6-8.

The service layer (`src.workflow.submissions`) is covered directly in
`tests/test_workflow/test_submissions.py`; this file is the seam between it and
a client — which status a refusal arrives as, and that the admin half of the
flow is genuinely admin-only, the same split `test_workflow_draft.py` and
`test_approval_queue.py` draw around their own tickets.
"""

from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient

from src.db.models import AgentVersion, User, VersionStatus
from tests.conftest import CONFIRMED_WORKFLOW, auth_header

pytestmark = pytest.mark.asyncio

_BODY = {"name": "Trả lời sổ tay", "description": "Hợp với trợ lý tra cứu sổ tay nội bộ"}


@pytest.fixture
def confirmed(solution_spec_version: Callable[..., AgentVersion], seeded_creator: User) -> Callable[..., AgentVersion]:
    def _make(*, status: VersionStatus = VersionStatus.WORKFLOW_CONFIRMED) -> AgentVersion:
        return solution_spec_version(
            seeded_creator, {"goal": "Trả lời khách bằng sổ tay nội bộ"}, status=status, workflow=CONFIRMED_WORKFLOW
        )

    return _make


def _save_as_template_url(version: AgentVersion) -> str:
    return f"/api/v1/agents/{version.agent_id}/versions/{version.id}/save-as-template"


async def _submit(client: AsyncClient, version: AgentVersion, token: str, **body: Any) -> Any:
    return await client.post(_save_as_template_url(version), json=_BODY | body, headers=auth_header(token))


# --------------------------------------------------------------------------- #
# Criterion 1 — only from workflow_confirmed
# --------------------------------------------------------------------------- #


async def test_a_confirmed_workflow_can_be_saved_as_a_template(
    client: AsyncClient, confirmed: Callable[..., AgentVersion], creator_token: str
) -> None:
    response = await _submit(client, confirmed(), creator_token)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["name"] == _BODY["name"]


async def test_refuses_a_workflow_nobody_confirmed_yet(
    client: AsyncClient, confirmed: Callable[..., AgentVersion], creator_token: str
) -> None:
    version = confirmed(status=VersionStatus.WORKFLOW_PENDING_CONFIRMATION)

    response = await _submit(client, version, creator_token)

    assert response.status_code == 409, response.text
    assert "workflow_confirmed" in response.json()["detail"]


async def test_a_stranger_cannot_save_someone_elses_workflow_as_a_template(
    client: AsyncClient, confirmed: Callable[..., AgentVersion], admin_token: str
) -> None:
    """`OwnedVersion` is the gate — an admin's own token has no agent here to own."""
    response = await _submit(client, confirmed(), admin_token)

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Criteria 2-3 — stripped, and pending is not offered by anything a creator sees
# --------------------------------------------------------------------------- #


async def test_the_submitted_template_is_stripped_and_stays_out_of_sight_until_approved(
    client: AsyncClient, confirmed: Callable[..., AgentVersion], creator_token: str, admin_token: str
) -> None:
    response = await _submit(client, confirmed(), creator_token)
    body = response.json()
    nodes = {node["id"]: node["config"] for node in body["nodes"]}
    assert nodes["search"]["source_ids"] == []
    assert nodes["answer"]["prompt"] == ""
    assert nodes["loop"]["max_iterations"] == 3

    queue = await client.get("/api/v1/admin/templates", headers=auth_header(admin_token))
    assert body["id"] in [entry["id"] for entry in queue.json()]


# --------------------------------------------------------------------------- #
# Criterion 4 — the admin queue, and that it is admin-only
# --------------------------------------------------------------------------- #


async def test_a_creator_cannot_read_the_admin_review_queue(client: AsyncClient, creator_token: str) -> None:
    response = await client.get("/api/v1/admin/templates", headers=auth_header(creator_token))

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Criterion 5 — approve, reject, disable
# --------------------------------------------------------------------------- #


async def test_admin_approves_a_submission(
    client: AsyncClient, confirmed: Callable[..., AgentVersion], creator_token: str, admin_token: str
) -> None:
    submitted = (await _submit(client, confirmed(), creator_token)).json()

    response = await client.post(f"/api/v1/admin/templates/{submitted['id']}/approve", headers=auth_header(admin_token))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"

    queue = await client.get("/api/v1/admin/templates", headers=auth_header(admin_token))
    assert submitted["id"] not in [entry["id"] for entry in queue.json()]


async def test_admin_rejects_a_submission_with_a_mandatory_reason(
    client: AsyncClient, confirmed: Callable[..., AgentVersion], creator_token: str, admin_token: str
) -> None:
    submitted = (await _submit(client, confirmed(), creator_token)).json()

    refused = await client.post(
        f"/api/v1/admin/templates/{submitted['id']}/reject", json={}, headers=auth_header(admin_token)
    )
    assert refused.status_code == 422

    response = await client.post(
        f"/api/v1/admin/templates/{submitted['id']}/reject",
        json={"reason": "Không hợp catalog"},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert response.json()["rejection_reason"] == "Không hợp catalog"


async def test_admin_disables_an_approved_submission(
    client: AsyncClient, confirmed: Callable[..., AgentVersion], creator_token: str, admin_token: str
) -> None:
    submitted = (await _submit(client, confirmed(), creator_token)).json()
    await client.post(f"/api/v1/admin/templates/{submitted['id']}/approve", headers=auth_header(admin_token))

    response = await client.post(f"/api/v1/admin/templates/{submitted['id']}/disable", headers=auth_header(admin_token))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "disabled"


async def test_a_creator_cannot_decide_on_any_submission(
    client: AsyncClient, confirmed: Callable[..., AgentVersion], creator_token: str
) -> None:
    submitted = (await _submit(client, confirmed(), creator_token)).json()

    response = await client.post(
        f"/api/v1/admin/templates/{submitted['id']}/approve", headers=auth_header(creator_token)
    )

    assert response.status_code == 403
