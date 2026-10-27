"""Quy tắc truy cập drafted from the creator's words — DS-12 / ADR-0059 §6.

The ticket's three acceptance criteria, against a real Hệ CSDL (the test
Postgres, `test_access_rules`'s scenery) and a scripted model:

- the demo description drafts exactly its two rules;
- a draft is never applied by itself: nothing is saved by drafting, and
  whatever the model names that the Hệ CSDL does not have is cut out and
  reported — a filter that loses its condition is dropped, never widened;
- the creator edits the draft and saves it through the ordinary door.
"""

import json
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage

from tests.conftest import ScriptedMetaAgent, auth_header, install_chat_model_factory
from tests.test_api.test_access_rules import (
    RULES,
    _fresh_test_limiter,
    _no_source_outlives_its_test,
    _staff_view,
    _trusted_test_host,
    source_url,
)

pytestmark = pytest.mark.asyncio

# Scenery borrowed from `test_access_rules`, named so pytest sees it.
__all__ = ["_fresh_test_limiter", "_no_source_outlives_its_test", "_staff_view", "_trusted_test_host", "source_url"]

DEMO = "Nhân viên chỉ xem công nợ của khách mình phụ trách, trưởng nhóm xem cả phòng."
IDENTITY = RULES["identity"]

#: The spec's two rules, exactly — what a model reading `DEMO` should propose.
DEMO_RULES = [
    {"position": "Nhân viên KD", "views": {"v_ar_open": {"where": {"sales_rep_id": "employee_code"}}}},
    {"position": "Trưởng nhóm KD", "views": {"v_ar_open": {"where": {"department": "department"}}}},
]


def _model_says(*documents: Any) -> ScriptedMetaAgent:
    model = ScriptedMetaAgent(
        *(
            AIMessage(content=document if isinstance(document, str) else json.dumps(document, ensure_ascii=False))
            for document in documents
        )
    )
    install_chat_model_factory(model)
    return model


async def _draft(client: AsyncClient, token: str, base: str, *, description: str = DEMO, identity: Any = None) -> Any:
    return await client.post(
        f"{base}/access-rules/draft",
        json={"description": description, "identity": identity or IDENTITY},
        headers=auth_header(token),
    )


async def test_the_demo_description_drafts_its_two_rules_and_saves_nothing(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    _model_says({"rules": DEMO_RULES})

    response = await _draft(client, creator_token, source_url)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"rules": {"identity": IDENTITY, "rules": DEMO_RULES}, "dropped": [], "problems": []}
    saved = await client.get(f"{source_url}/access-rules", headers=auth_header(creator_token))
    assert saved.json()["rules"] is None, "drafting saved the rules — a draft must never be applied by itself"


async def test_the_model_is_shown_the_real_positions_the_identity_and_the_view_columns(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    model = _model_says({"rules": DEMO_RULES})

    await _draft(client, creator_token, source_url)

    [prompt] = model.prompts
    for position in ("Nhân viên KD", "Trưởng nhóm KD", "Kế toán trưởng"):
        assert f"- {position}" in prompt, position
    assert "employee_code → cột emp_id" in prompt
    assert "- v_ar_open:" in prompt and "sales_rep_id" in prompt
    # The employee view is for resolving askers, not a view a rule grants.
    assert "- v_test_staff:" not in prompt
    assert DEMO in prompt


async def test_whatever_the_model_names_that_is_not_there_is_cut_out_and_reported(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    _model_says(
        {
            "rules": [
                {"position": "Giám đốc", "views": {"v_ar_open": "all"}},
                {
                    "position": "nhân viên kd",  # a position written in the model's own case still meets the real one
                    "views": {
                        "v_ar_open": {"where": {"sales_rep_id": "employee_code", "rep_code": "employee_code"}},
                        "fin_invoice": "all",
                    },
                },
                # Every condition invalid: the grant goes — it is never widened to "all".
                {"position": "Trưởng nhóm KD", "views": {"v_ar_open": {"where": {"department": "region"}}}},
                {"position": "Kế toán trưởng", "views": {"v_ar_open": "all"}},
            ]
        }
    )

    body = (await _draft(client, creator_token, source_url)).json()

    assert body["rules"]["rules"] == [
        {"position": "Nhân viên KD", "views": {"v_ar_open": {"where": {"sales_rep_id": "employee_code"}}}},
        {"position": "Kế toán trưởng", "views": {"v_ar_open": "all"}},
    ]
    assert body["problems"] == []
    dropped = "\n".join(body["dropped"])
    assert "'Giám đốc' không có trong view nhân viên" in dropped
    assert "không có cột 'rep_code'" in dropped
    assert "'fin_invoice' không được phép đọc" in dropped
    assert "không có thuộc tính 'region'" in dropped
    assert "'Trưởng nhóm KD' không còn quyền xem view nào hợp lệ" in dropped


async def test_an_identity_that_does_not_fit_is_refused_before_the_model_is_asked(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    model = _model_says({"rules": DEMO_RULES})

    response = await _draft(client, creator_token, source_url, identity={**IDENTITY, "view": "fin_invoice"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["problems"]
    assert model.prompts == []


async def test_a_reply_that_is_never_json_is_a_502_and_saves_nothing(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    _model_says("không phải JSON", "vẫn không phải", "lần ba cũng không")

    response = await _draft(client, creator_token, source_url)

    assert response.status_code == 502, response.text
    saved = await client.get(f"{source_url}/access-rules", headers=auth_header(creator_token))
    assert saved.json()["rules"] is None


async def test_the_creator_edits_the_draft_and_saves_it_through_the_ordinary_door(
    client: AsyncClient, creator_token: str, source_url: str
) -> None:
    _model_says({"rules": DEMO_RULES})
    drafted = (await _draft(client, creator_token, source_url)).json()["rules"]

    edited = {**drafted, "rules": [*drafted["rules"], {"position": "Kế toán trưởng", "views": {"v_ar_open": "all"}}]}
    saved = await client.put(f"{source_url}/access-rules", json=edited, headers=auth_header(creator_token))

    assert saved.status_code == 200, saved.text
    read = await client.get(f"{source_url}/access-rules", headers=auth_header(creator_token))
    assert read.json()["rules"] == edited


async def test_someone_elses_source_cannot_be_drafted_for(
    client: AsyncClient, second_creator_token: str, source_url: str
) -> None:
    model = _model_says({"rules": DEMO_RULES})

    response = await _draft(client, second_creator_token, source_url)

    assert response.status_code == 404, response.text
    assert model.prompts == []
