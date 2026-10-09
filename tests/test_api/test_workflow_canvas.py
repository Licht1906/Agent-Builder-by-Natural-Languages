"""Editing a workflow over HTTP — the Workflow Canvas's three calls, #44.

The canvas is a screen, so what it needs from the backend is small and specific:
the table of node types it draws a palette and a property form from, a save that
never argues with a half-finished graph, and a confirm that argues with every
part of a broken one at once.

The acceptance criteria this file is measured against are the two that are
easiest to get backwards. **Saving a structurally invalid draft must succeed** —
validation blocks the confirm transition and nothing else. And **an edit after
confirmation must not touch the confirmed row** — it forks, and the response says
which version the creator is now editing.
"""

from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.db.models import AgentVersion, User, VersionStatus
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

SPEC: dict[str, Any] = {
    "goal": "Trả lời khiếu nại của khách",
    "users": ["Nhân viên chăm sóc khách hàng"],
    "data": [{"name": "Sổ tay", "kind": "uploaded_file", "ref": "blob://so-tay.pdf"}],
    "actions": ["Tra cứu đơn hàng"],
    "expected_outcome": ["Một thư trả lời"],
    "success_criteria": [{"text": "Nêu đúng mã đơn", "category": "correctness"}],
    "prohibitions": ["Không hứa hoàn tiền"],
}

VALID: dict[str, Any] = {
    "start_node_id": "answer",
    "nodes": [
        {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời", "model": "gpt-4o-mini"}},
        {"id": "done", "type": "end", "config": {}},
    ],
    "edges": [{"id": "e1", "source_node_id": "answer", "source_port": "done", "target_node_id": "done"}],
    "metadata": {},
}

#: One node dropped on the canvas and nothing else done to it — no wiring, no
#: prompt, no `End`. The state ADR-0022 §6 promises will save.
MID_EDIT: dict[str, Any] = {
    "start_node_id": "answer",
    "nodes": [{"id": "answer", "type": "agent_llm", "config": {"prompt": "", "model": ""}}],
    "edges": [],
    "metadata": {},
}


@pytest.fixture
def drafting(solution_spec_version: Callable[..., AgentVersion], seeded_creator: User) -> Callable[..., AgentVersion]:
    """A version holding a workflow, at whatever stage the test needs."""

    def _make(
        document: dict[str, Any] | None = None,
        *,
        owner: User | None = None,
        status: VersionStatus = VersionStatus.WORKFLOW_PENDING_CONFIRMATION,
    ) -> AgentVersion:
        return solution_spec_version(
            owner or seeded_creator,
            SPEC,
            status=status,
            workflow=document if document is not None else VALID,
        )

    return _make


async def save(client: AsyncClient, token: str, row: AgentVersion, document: dict[str, Any]) -> Any:
    return await client.put(
        f"/api/v1/agents/{row.agent_id}/versions/{row.id}/workflow",
        headers=auth_header(token),
        json=document,
    )


async def confirm(client: AsyncClient, token: str, row: AgentVersion) -> Any:
    return await client.post(
        f"/api/v1/agents/{row.agent_id}/versions/{row.id}/workflow/confirm", headers=auth_header(token)
    )


# --------------------------------------------------------------------------- #
# Saving — AC 4, the draft that always stores
# --------------------------------------------------------------------------- #


async def test_saving_a_structurally_invalid_draft_succeeds(
    client: AsyncClient, creator_token: str, drafting: Any
) -> None:
    """AC 4. The single most important behaviour on this screen."""
    response = await save(client, creator_token, drafting(), MID_EDIT)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == VersionStatus.WORKFLOW_PENDING_CONFIRMATION.value
    assert body["workflow"]["nodes"][0]["config"]["prompt"] == ""


# --------------------------------------------------------------------------- #
# Confirming — AC 5, the one gate the validator stands in front of
# --------------------------------------------------------------------------- #


async def test_editing_after_confirmation_forks_a_new_version(
    client: AsyncClient, creator_token: str, drafting: Any, db_session: Session
) -> None:
    """AC 5, second half — and the response says which version the canvas is now on."""
    row = drafting()
    await confirm(client, creator_token, row)

    body = (await save(client, creator_token, row, MID_EDIT)).json()

    assert body["version_id"] != row.id
    assert body["status"] == VersionStatus.WORKFLOW_PENDING_CONFIRMATION.value
    db_session.refresh(row)
    assert row.status is VersionStatus.WORKFLOW_CONFIRMED
    assert row.workflow == VALID
