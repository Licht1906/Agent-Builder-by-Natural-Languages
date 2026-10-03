"""The admin config screen over HTTP — who may open it, and what it refuses (#12).

`tests/test_admin/test_platform_config.py` covers what the values mean. This
file covers the two things only a request can show: that a creator gets nowhere
near these settings, and that a bad value comes back as a sentence rather than a
stack trace.
"""

import pytest

from src.db.models import DEFAULT_EVAL_PASS_THRESHOLD
from src.models.model_catalog import SEEDED_MODEL_ALLOWLIST
from tests.conftest import auth_header

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

CONFIG_URL = "/api/v1/admin/config"


async def read_config(client, token: str):
    return await client.get(CONFIG_URL, headers=auth_header(token))


async def test_admin_reads_both_settings(client, admin_token) -> None:
    response = await read_config(client, admin_token)

    assert response.status_code == 200
    config = response.json()
    assert config["eval_pass_threshold"] == DEFAULT_EVAL_PASS_THRESHOLD
    assert config["model_allowlist"] == list(SEEDED_MODEL_ALLOWLIST)


async def test_a_creator_can_neither_read_nor_change_the_bar(client, creator_token) -> None:
    """These two settings decide how good every agent has to be and what it may be built from.

    Both verbs, because they fail for different reasons and only one of them is
    the dangerous one: a creator who could merely *read* the threshold learns
    the bar, and a creator who could *write* it sets their own.
    """
    assert (await read_config(client, creator_token)).status_code == 403

    response = await client.patch(CONFIG_URL, json={"eval_pass_threshold": 0.0}, headers=auth_header(creator_token))

    assert response.status_code == 403


async def test_changing_only_the_threshold_leaves_the_allowlist_alone(client, admin_token) -> None:
    """What the settings screen relies on: it sends one field and must not disturb the other.

    A screen that had to send the allowlist back to change the threshold would
    be writing a list it read some seconds earlier — and would silently undo
    whatever another admin changed in between.
    """
    response = await client.patch(CONFIG_URL, json={"eval_pass_threshold": 0.42}, headers=auth_header(admin_token))

    assert response.status_code == 200, response.text
    assert response.json()["eval_pass_threshold"] == 0.42
    assert response.json()["model_allowlist"] == list(SEEDED_MODEL_ALLOWLIST)


async def test_an_impossible_threshold_comes_back_as_a_sentence(client, admin_token) -> None:
    """The screen renders `detail` verbatim, so it has to be a sentence and not a stack trace."""
    response = await client.patch(CONFIG_URL, json={"eval_pass_threshold": 1.4}, headers=auth_header(admin_token))

    assert response.status_code == 422
    assert "Ngưỡng eval" in response.json()["detail"]
    # Refused means unchanged, not partly applied.
    assert (await read_config(client, admin_token)).json()["eval_pass_threshold"] == DEFAULT_EVAL_PASS_THRESHOLD
