"""UC1-06 (#120), FR-14 — `clarifications` reach the client separate from the spec.

`draft_spec` (#119) already splits them into `DraftedSpec.spec` /
`DraftedSpec.clarifications`; this suite is the HTTP contract that carries that
split across the wire, which is what the form (FR-12) renders each clarification
beside its field from. The seam is HTTP, same as every other suite here.
"""

import json

import pytest
from httpx import AsyncClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from src.main import app
from src.services.llm import get_chat_model_factory
from tests.conftest import auth_header
from tests.test_agentspec.conftest import spec_payload

pytestmark = pytest.mark.asyncio

_CLARIFICATIONS = [
    {
        "field": "authority.approval_matrix",
        "question": "Ai là người duyệt trước khi email nhắc nợ được gửi đi?",
        "why": "Email rời khỏi tổ chức và tới khách hàng.",
    },
    {
        "field": "audience.data_scope",
        "question": "Mỗi nhân viên chỉ xem công nợ khách mình phụ trách, hay cả phòng?",
        "why": "Đang mặc định phạm vi hẹp nhất.",
    },
    {
        "field": "connections",
        "question": "Dữ liệu công nợ nằm ở hệ thống nào?",
        "why": "Cần đúng nguồn để cấp quyền chỉ trên view cần thiết.",
    },
]


def _valid_draft() -> dict:
    document = spec_payload()
    document["clarifications"] = _CLARIFICATIONS
    return document


def _install_reply(document: dict) -> None:
    model = FakeMessagesListChatModel(responses=[AIMessage(content=json.dumps(document, ensure_ascii=False))])
    app.dependency_overrides[get_chat_model_factory] = lambda: (lambda model_id: model)


@pytest.fixture(autouse=True)
def _reset_metaagent_model_factory():
    yield
    app.dependency_overrides.pop(get_chat_model_factory, None)


async def test_clarifications_come_back_separate_from_the_spec(client: AsyncClient, creator_token: str) -> None:
    _install_reply(_valid_draft())

    response = await client.post(
        "/api/v1/metaagent/draft-spec",
        json={"description": "Tôi cần trợ lý theo dõi công nợ quá hạn và nhắc nhân viên."},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "clarifications" not in body["spec"]
    assert 3 <= len(body["clarifications"]) <= 6
    assert [c["field"] for c in body["clarifications"]] == [c["field"] for c in _CLARIFICATIONS]
    assert body["spec"]["metadata"]["status"] == "draft"


async def test_metadata_status_is_forced_to_draft_whatever_the_model_claims(
    client: AsyncClient, creator_token: str
) -> None:
    """Hard rule #6 of the prompt, enforced regardless (`validate_drafted_spec`, #111)."""
    draft = _valid_draft()
    draft["metadata"] = {**draft["metadata"], "status": "approved"}
    _install_reply(draft)

    response = await client.post(
        "/api/v1/metaagent/draft-spec",
        json={"description": "Mô tả bất kỳ."},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["spec"]["metadata"]["status"] == "draft"


async def test_an_invalid_first_reply_is_retried_with_the_validation_error_and_then_succeeds(
    client: AsyncClient, creator_token: str
) -> None:
    invented_tool = spec_payload(capabilities=[{"tool": "erp.query_all", "scope": "read"}])
    invented_tool["clarifications"] = _CLARIFICATIONS
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(content=json.dumps(invented_tool, ensure_ascii=False)),
            AIMessage(content=json.dumps(_valid_draft(), ensure_ascii=False)),
        ]
    )
    app.dependency_overrides[get_chat_model_factory] = lambda: (lambda model_id: model)

    response = await client.post(
        "/api/v1/metaagent/draft-spec",
        json={"description": "Mô tả bất kỳ."},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["spec"]["metadata"]["id"] == "agt_ar_assistant"


async def test_requires_authentication(client: AsyncClient) -> None:
    response = await client.post("/api/v1/metaagent/draft-spec", json={"description": "Bất kỳ."})

    assert response.status_code == 401


async def test_an_empty_description_is_rejected_with_a_422_not_a_call_to_the_model(
    client: AsyncClient, creator_token: str
) -> None:
    response = await client.post(
        "/api/v1/metaagent/draft-spec",
        json={"description": ""},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422


async def test_exhausting_every_attempt_is_a_structured_error_not_a_crash(
    client: AsyncClient, creator_token: str
) -> None:
    invented_tool = spec_payload(capabilities=[{"tool": "erp.query_all", "scope": "read"}])
    invented_tool["clarifications"] = _CLARIFICATIONS
    model = FakeMessagesListChatModel(
        responses=[AIMessage(content=json.dumps(invented_tool, ensure_ascii=False))] * 3
    )
    app.dependency_overrides[get_chat_model_factory] = lambda: (lambda model_id: model)

    response = await client.post(
        "/api/v1/metaagent/draft-spec",
        json={"description": "Mô tả bất kỳ."},
        headers=auth_header(creator_token),
    )

    assert response.status_code == 422
    assert "thử lại" in response.json()["detail"]
