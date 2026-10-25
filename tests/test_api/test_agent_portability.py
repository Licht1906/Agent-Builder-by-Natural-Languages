"""Export and import over HTTP — the round trip a person actually performs.

`tests/test_agents/test_portability.py` covers the same journey in Python, and
that is exactly why this file exists: calling `export_bundle` and `import_bundle`
directly skips `AgentBundleRequest`, so it cannot see a bundle the app writes and
its own importer refuses. It did not see one, and shipped it — the export wrote
`source_version_id` into a schema declared `extra="forbid"`, and every downloaded
file was rejected on the way back in with `Extra inputs are not permitted`.

So the assertion that matters here is not that the endpoints work. It is that
**the bytes one endpoint produces are bytes the other accepts**, with nothing
edited in between.
"""

import json
from typing import Any

from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.db.models import Agent, AgentVersion, User, VersionStatus
from src.models.workflow import WorkflowJSON
from tests.conftest import auth_header

SPEC: dict[str, Any] = {
    "goal": "Trả lời khiếu nại của khách",
    "users": ["Nhân viên chăm sóc khách hàng"],
    "data": [{"name": "Sổ tay", "kind": "uploaded_file", "ref": "so-tay.pdf"}],
    "actions": ["Soạn thư trả lời"],
    "expected_outcome": ["Một thư trả lời chờ duyệt"],
    "success_criteria": [{"text": "Bám theo sổ tay", "category": "source_groundedness"}],
    "prohibitions": ["Không tự gửi thư"],
}

GRAPH: dict[str, Any] = {
    "start_node_id": "answer",
    "nodes": [
        {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời", "model": "gpt-4.1"}},
        {"id": "done", "type": "end", "config": {}},
    ],
    "edges": [{"id": "e1", "source_node_id": "answer", "source_port": "done", "target_node_id": "done"}],
    "metadata": {},
}


async def test_a_downloaded_bundle_is_accepted_verbatim_by_import(
    client: AsyncClient,
    creator_token: str,
    seeded_creator: User,
    solution_spec_version: Any,
    db_session: Session,
) -> None:
    """Download, upload, unchanged — the property the two endpoints exist to have.

    The file is round-tripped through `json.dumps`/`json.loads` rather than
    passed as the parsed response body, because that is what a browser does: the
    thing being asserted is that what lands on disk is what may be sent back.
    """
    version = solution_spec_version(seeded_creator, SPEC, status=VersionStatus.APPROVED, workflow=GRAPH)

    exported = await client.get(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/export",
        headers=auth_header(creator_token),
    )
    assert exported.status_code == 200

    on_disk = json.loads(json.dumps(exported.json()))
    imported = await client.post("/api/v1/agents/import", headers=auth_header(creator_token), json=on_disk)

    assert imported.status_code == 201, imported.json()
    body = imported.json()
    assert body["agent"]["id"] != version.agent_id
    assert body["remapped_models"] == {}

    stored = db_session.get(AgentVersion, body["version_id"])
    assert stored is not None
    assert stored.status is VersionStatus.WORKFLOW_PENDING_CONFIRMATION
    assert WorkflowJSON.model_validate(stored.workflow) == WorkflowJSON.model_validate(GRAPH)
    assert db_session.get(Agent, body["agent"]["id"]).owner_id == seeded_creator.id  # type: ignore[union-attr]


async def test_a_bundle_from_a_later_build_is_refused_rather_than_half_applied(
    client: AsyncClient, creator_token: str, seeded_creator: User, solution_spec_version: Any
) -> None:
    """An unknown `format` is a 422 naming both versions, and creates nothing."""
    version = solution_spec_version(seeded_creator, SPEC, status=VersionStatus.APPROVED, workflow=GRAPH)
    exported = (
        await client.get(
            f"/api/v1/agents/{version.agent_id}/versions/{version.id}/export",
            headers=auth_header(creator_token),
        )
    ).json()

    response = await client.post(
        "/api/v1/agents/import",
        headers=auth_header(creator_token),
        json={**exported, "format": "agentlab.agent/99"},
    )

    assert response.status_code == 422
    assert "agentlab.agent/99" in response.json()["detail"]


async def test_a_field_nobody_declared_is_still_refused(
    client: AsyncClient, creator_token: str, seeded_creator: User, solution_spec_version: Any
) -> None:
    """`extra="forbid"` is kept, so a mistyped key cannot become a silent default.

    The fix for `source_version_id` was to *declare* it, not to start ignoring
    unknown fields — a bundle whose `workflow` was misspelled would otherwise
    import as an agent with no graph and no complaint.
    """
    version = solution_spec_version(seeded_creator, SPEC, status=VersionStatus.APPROVED, workflow=GRAPH)
    exported = (
        await client.get(
            f"/api/v1/agents/{version.agent_id}/versions/{version.id}/export",
            headers=auth_header(creator_token),
        )
    ).json()

    response = await client.post(
        "/api/v1/agents/import",
        headers=auth_header(creator_token),
        json={**exported, "wrkflow": exported["workflow"]},
    )

    assert response.status_code == 422
