"""UC1-24 (#140) — `GET /agents/{id}/spec-versions` + `PATCH
/agents/{id}/versions/{v}`, spec.md §13/§19, FR-13/FR-50, AC-24: "sửa spec 3
lần → 3 version độc lập truy vấn lại được, version cũ không đổi nội dung".
"""

from typing import Any

import pytest
from httpx import AsyncClient

from src.db.models import UC1AgentSpecVersion
from tests.conftest import auth_header
from tests.test_agentspec.conftest import spec_payload

pytestmark = pytest.mark.asyncio


def _agent_admin_user_id(agent_admin_token: str) -> int:
    import jwt as _jwt

    from src.config import get_settings

    claims = _jwt.decode(agent_admin_token, get_settings().jwt_secret, algorithms=["HS256"])
    return int(claims["sub"])


def _seed_spec_version(
    db_session, *, created_by_id: int, agent_id: str = "agt_ar_assistant", version: int = 1, **overrides: Any
) -> None:
    payload = spec_payload(metadata={**spec_payload()["metadata"], "id": agent_id, "version": version}, **overrides)
    db_session.add(UC1AgentSpecVersion(agent_id=agent_id, version=version, spec=payload, created_by_id=created_by_id))
    db_session.commit()


async def test_editing_a_spec_three_times_produces_three_independent_versions(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    agent_id = "agt_edit_thrice"
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id=agent_id)

    editing_version = 1
    for max_calls in (10, 20, 30):
        response = await client.patch(
            f"/api/v1/agents/{agent_id}/versions/{editing_version}",
            json={"authority": {"autonomy": "read_only", "budget": {"max_tool_calls_per_session": max_calls}}},
            headers=auth_header(agent_admin_token),
        )
        assert response.status_code == 200, response.text
        editing_version = response.json()["version"]  # edit the version each edit just produced, not v1 again

    listing = await client.get(f"/api/v1/agents/{agent_id}/spec-versions", headers=auth_header(agent_admin_token))
    assert listing.status_code == 200, listing.text
    versions = listing.json()
    assert [v["version"] for v in versions] == [1, 2, 3, 4]
    assert [v["status"] for v in versions] == ["draft", "draft", "draft", "draft"]

    # The original version's own content is unchanged — every edit landed as a
    # new row, never a mutation of version 1.
    v1 = db_session.query(UC1AgentSpecVersion).filter_by(agent_id=agent_id, version=1).one()
    assert v1.spec["authority"]["budget"]["max_tool_calls_per_session"] == 40  # spec_payload's own default, untouched


async def test_the_patched_version_has_the_edit_applied_and_points_at_its_parent(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    agent_id = "agt_edit_content"
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id=agent_id)

    response = await client.patch(
        f"/api/v1/agents/{agent_id}/versions/1",
        json={"authority": {"autonomy": "propose", "budget": {"max_tool_calls_per_session": 15}}},
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 2
    assert body["status"] == "draft"
    assert body["parent_version"] == 1
    assert body["spec"]["authority"]["budget"]["max_tool_calls_per_session"] == 15


async def test_a_patch_cannot_remove_a_baseline_guardrail(client: AsyncClient, agent_admin_token: str, db_session) -> None:
    agent_id = "agt_edit_guardrail"
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id=agent_id)

    response = await client.patch(
        f"/api/v1/agents/{agent_id}/versions/1",
        json={"guardrails": {"input": [], "output": [], "fallback": "escalate_to_human"}},
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 422, response.text
    assert any(problem["code"] == "baseline_guardrail_removed" for problem in response.json()["detail"])

    listing = await client.get(f"/api/v1/agents/{agent_id}/spec-versions", headers=auth_header(agent_admin_token))
    assert [v["version"] for v in listing.json()] == [1]  # refused — no new version created


async def test_a_patch_cannot_set_identity_or_lifecycle_metadata_directly(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    """A client trying to sneak `metadata.status`/`version` into the payload is silently
    ignored — the platform, not the caller, controls identity and lifecycle state.
    """
    agent_id = "agt_edit_metadata_attempt"
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id=agent_id)

    response = await client.patch(
        f"/api/v1/agents/{agent_id}/versions/1",
        json={"metadata": {"id": agent_id, "name": "Trợ lý công nợ", "version": 999, "status": "deployed"}},
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 2  # not 1000 — the client-supplied version/status were ignored
    assert body["status"] == "draft"


async def test_a_patch_can_edit_the_ordinary_metadata_fields_fr13(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    """FR-13: "mọi trường trừ guardrail baseline" — `metadata.name`/`description`
    are ordinary fields, not identity/lifecycle state, so a PATCH must actually
    apply them, not silently drop the whole `metadata` object.
    """
    agent_id = "agt_edit_metadata_name"
    user_id = _agent_admin_user_id(agent_admin_token)
    _seed_spec_version(db_session, created_by_id=user_id, agent_id=agent_id)

    response = await client.patch(
        f"/api/v1/agents/{agent_id}/versions/1",
        json={"metadata": {"name": "Trợ lý công nợ v2", "description": "Bản cập nhật"}},
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["spec"]["metadata"]["name"] == "Trợ lý công nợ v2"
    assert body["spec"]["metadata"]["description"] == "Bản cập nhật"
    assert body["spec"]["metadata"]["id"] == agent_id  # identity still platform-controlled
    assert body["version"] == 2


async def test_patching_a_missing_version_is_a_404(client: AsyncClient, agent_admin_token: str) -> None:
    response = await client.patch(
        "/api/v1/agents/agt_never_created/versions/1", json={}, headers=auth_header(agent_admin_token)
    )
    assert response.status_code == 404


async def test_only_agent_admin_may_list_or_edit_versions(client: AsyncClient, creator_token: str, db_session) -> None:
    listing = await client.get("/api/v1/agents/agt_ar_assistant/spec-versions", headers=auth_header(creator_token))
    assert listing.status_code == 403

    patch = await client.patch(
        "/api/v1/agents/agt_ar_assistant/versions/1", json={}, headers=auth_header(creator_token)
    )
    assert patch.status_code == 403


# --------------------------------------------------------------------------- #
# UC1-04 (E2) — `POST /agents/{id}/spec-versions`: confirming a drafted spec as
# version 1. Until this existed, `POST /metaagent/draft-spec` produced a spec
# nothing could store, and every test here had to reach past HTTP and insert
# the row itself (`_seed_spec_version` above).
# --------------------------------------------------------------------------- #


async def test_confirming_a_drafted_spec_creates_version_1(client: AsyncClient, agent_admin_token: str) -> None:
    agent_id = "agt_confirmed_draft"

    response = await client.post(
        f"/api/v1/agents/{agent_id}/spec-versions",
        json=spec_payload(metadata={**spec_payload()["metadata"], "id": agent_id}),
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["agent_id"] == agent_id
    assert body["version"] == 1
    assert body["status"] == "draft"
    assert body["parent_version"] is None

    # Persisted, not just echoed — the whole point of the endpoint.
    listing = await client.get(f"/api/v1/agents/{agent_id}/spec-versions", headers=auth_header(agent_admin_token))
    assert [row["version"] for row in listing.json()] == [1]


async def test_the_confirmed_version_can_then_be_edited(client: AsyncClient, agent_admin_token: str) -> None:
    """The gap this closes, stated as the flow it unblocks: draft -> confirm -> edit."""
    agent_id = "agt_confirm_then_edit"
    await client.post(
        f"/api/v1/agents/{agent_id}/spec-versions",
        json=spec_payload(metadata={**spec_payload()["metadata"], "id": agent_id}),
        headers=auth_header(agent_admin_token),
    )

    patch = await client.patch(
        f"/api/v1/agents/{agent_id}/versions/1",
        json={"metadata": {"description": "Sửa sau khi xác nhận"}},
        headers=auth_header(agent_admin_token),
    )

    assert patch.status_code == 200, patch.text
    assert patch.json()["version"] == 2
    assert patch.json()["spec"]["metadata"]["parent_version"] == 1


async def test_the_platform_names_the_version_status_and_id_not_the_model(
    client: AsyncClient, agent_admin_token: str
) -> None:
    """A drafted document claiming version 7, `approved`, and somebody else's id
    is not granting itself any of the three — same pinning a human's PATCH gets.
    """
    agent_id = "agt_pinned_identity"

    response = await client.post(
        f"/api/v1/agents/{agent_id}/spec-versions",
        json=spec_payload(
            metadata={
                **spec_payload()["metadata"],
                "id": "agt_somebody_else",
                "version": 7,
                "status": "approved",
                "approved_by": ["E999"],
            }
        ),
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 201, response.text
    metadata = response.json()["spec"]["metadata"]
    assert metadata["id"] == agent_id
    assert metadata["version"] == 1
    assert metadata["status"] == "draft"
    assert metadata["approved_by"] == []


async def test_confirming_twice_is_a_409_pointing_at_patch(client: AsyncClient, agent_admin_token: str) -> None:
    """A double-click must not become a second version 1 — nor a 500 off the
    `(agent_id, version)` unique index.
    """
    agent_id = "agt_confirmed_twice"
    payload = spec_payload(metadata={**spec_payload()["metadata"], "id": agent_id})
    first = await client.post(
        f"/api/v1/agents/{agent_id}/spec-versions", json=payload, headers=auth_header(agent_admin_token)
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/api/v1/agents/{agent_id}/spec-versions", json=payload, headers=auth_header(agent_admin_token)
    )

    assert second.status_code == 409
    assert "PATCH" in second.json()["detail"]


async def test_an_invalid_spec_is_refused_with_field_level_problems(
    client: AsyncClient, agent_admin_token: str
) -> None:
    """The strict gate, not the draft door — `validate_spec`, with `Problem`s a
    form can render beside the field each one names.
    """
    agent_id = "agt_invalid_confirm"
    broken = spec_payload(metadata={**spec_payload()["metadata"], "id": agent_id})
    broken["capabilities"] = [{"tool": "erp.a_tool_that_does_not_exist", "scope": "read"}]

    response = await client.post(
        f"/api/v1/agents/{agent_id}/spec-versions", json=broken, headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 422, response.text
    assert any(problem["code"] == "unknown_tool" for problem in response.json()["detail"])

    listing = await client.get(f"/api/v1/agents/{agent_id}/spec-versions", headers=auth_header(agent_admin_token))
    assert listing.json() == []  # nothing half-written


async def test_a_baseline_guardrail_removal_is_refused_on_confirm(
    client: AsyncClient, agent_admin_token: str
) -> None:
    """`validate_spec`, not `validate_drafted_spec`: the draft door unions the
    baseline back in, and confirming must not become a way to launder a spec
    past the gate a human's edit is held to.
    """
    agent_id = "agt_confirm_no_baseline"
    stripped = spec_payload(metadata={**spec_payload()["metadata"], "id": agent_id})
    stripped["guardrails"] = {**stripped["guardrails"], "input": ["prompt_injection_detector"]}

    response = await client.post(
        f"/api/v1/agents/{agent_id}/spec-versions", json=stripped, headers=auth_header(agent_admin_token)
    )

    assert response.status_code == 422, response.text


async def test_an_agent_id_the_schema_refuses_is_a_422(client: AsyncClient, agent_admin_token: str) -> None:
    """`metadata.id` is also a Postgres role name and a container name — the URL
    cannot smuggle a value the pattern forbids past the gate.
    """
    response = await client.post(
        "/api/v1/agents/Not An Agent Id/spec-versions",
        json=spec_payload(),
        headers=auth_header(agent_admin_token),
    )

    assert response.status_code == 422, response.text


async def test_only_agent_admin_may_confirm_a_draft(client: AsyncClient, creator_token: str) -> None:
    response = await client.post(
        "/api/v1/agents/agt_creator_attempt/spec-versions",
        json=spec_payload(),
        headers=auth_header(creator_token),
    )

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# `GET /uc1-agents` — the catalog. Every other UC-1 endpoint is addressed by
# `(agent_id, version)`; this is the only thing that hands those out.
# --------------------------------------------------------------------------- #


async def test_the_catalog_lists_a_confirmed_agent_at_its_newest_version(
    client: AsyncClient, agent_admin_token: str
) -> None:
    agent_id = "agt_catalog_newest"
    await client.post(
        f"/api/v1/agents/{agent_id}/spec-versions",
        json=spec_payload(metadata={**spec_payload()["metadata"], "id": agent_id}),
        headers=auth_header(agent_admin_token),
    )
    await client.patch(
        f"/api/v1/agents/{agent_id}/versions/1",
        json={"metadata": {"description": "lần sửa thứ nhất"}},
        headers=auth_header(agent_admin_token),
    )

    response = await client.get("/api/v1/uc1-agents", headers=auth_header(agent_admin_token))

    assert response.status_code == 200, response.text
    entry = next(row for row in response.json() if row["agent_id"] == agent_id)
    assert entry["latest_version"] == 2
    assert entry["version_count"] == 2
    assert entry["status"] == "draft"
    assert entry["name"] == spec_payload()["metadata"]["name"]


async def test_the_catalog_has_one_row_per_agent_not_per_version(
    client: AsyncClient, agent_admin_token: str
) -> None:
    agent_id = "agt_catalog_one_row"
    await client.post(
        f"/api/v1/agents/{agent_id}/spec-versions",
        json=spec_payload(metadata={**spec_payload()["metadata"], "id": agent_id}),
        headers=auth_header(agent_admin_token),
    )
    editing = 1
    for _ in range(3):
        patch = await client.patch(
            f"/api/v1/agents/{agent_id}/versions/{editing}",
            json={"metadata": {"description": "sửa"}},
            headers=auth_header(agent_admin_token),
        )
        assert patch.status_code == 200, patch.text
        editing = patch.json()["version"]

    response = await client.get("/api/v1/uc1-agents", headers=auth_header(agent_admin_token))

    assert [row["agent_id"] for row in response.json()].count(agent_id) == 1


async def test_only_agent_admin_may_read_the_catalog(client: AsyncClient, creator_token: str) -> None:
    response = await client.get("/api/v1/uc1-agents", headers=auth_header(creator_token))
    assert response.status_code == 403
