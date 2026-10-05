"""`GET /workflow/node-types` — the table the canvas draws its palette and forms from.

One decoration is pinned here because its absence was a field bug: a creator
who dragged an `Agent/LLM` node onto the canvas got `model: ""` and a free-text
box, typed nothing (what would they type?), and met the confirm gate's
"chưa điền: model" — the report *"không có node search thì không confirm
được"* was a hand-built agent stuck on exactly this. The allowlist is platform
configuration the registry cannot know, so the route adds it on the way out.
"""

import pytest
from httpx import AsyncClient

from src.admin.config import read_config
from src.models.model_catalog import default_node_model
from src.models.node_registry import NODE_REGISTRY
from tests.conftest import auth_header

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]


async def test_the_model_field_offers_the_allowlist_and_a_valid_default(
    client: AsyncClient, creator_token: str, db_session
) -> None:
    response = await client.get("/api/v1/workflow/node-types", headers=auth_header(creator_token))

    assert response.status_code == 200, response.text
    agent_llm = next(entry for entry in response.json() if entry["type"] == "agent_llm")
    model = agent_llm["config_schema"]["properties"]["model"]
    allowlist = list(read_config(db_session).model_allowlist)
    assert model["enum"] == allowlist
    # A node dropped from the palette is born valid: the default is a real model.
    assert model["default"] == default_node_model(allowlist)
    assert model["default"] in allowlist


async def test_no_other_field_is_touched(client: AsyncClient, creator_token: str) -> None:
    """The decoration is surgical — the prompt stays prose, other types stay as declared."""
    response = await client.get("/api/v1/workflow/node-types", headers=auth_header(creator_token))

    entries = {entry["type"]: entry for entry in response.json()}
    assert "enum" not in entries["agent_llm"]["config_schema"]["properties"]["prompt"]
    assert "enum" not in entries["if_else"]["config_schema"]["properties"]["condition"]
    # The registry's own order, not a copy of it — a ninth type must appear here unasked.
    assert [entry["type"] for entry in response.json()] == [node_type.value for node_type in NODE_REGISTRY]
