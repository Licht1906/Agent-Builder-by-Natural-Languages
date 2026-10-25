"""UC1-26 (#142), FR-52, AC: "bắt traffic ra ngoài lúc tạo agent/agent trả lời
câu hỏi → 0 request tới control plane".

`control_plane_that_must_never_be_reached` makes the *real* `httpx.get` this
module's `pull_template` would call fully live, and fatal if anything reaches
it — the same shape `tests.conftest.smtp_that_must_never_be_reached` already
uses to prove `mail.send_reminder` never fires before approval. Without a
configured `control_plane_url`, `pull_template` refuses early on its own
(§12.4's honest empty default); this fixture is what proves the *absence* of
a call, not merely the absence of a working one.
"""

import json

import pytest
from httpx import AsyncClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import src.services.control_plane as control_plane
from src.config import get_settings
from src.db.models import UC1AgentSpecVersion
from src.main import app
from src.policy.pep import AuthzRequest, Decision, Effect, get_pep_client
from src.services.llm import get_chat_model_factory
from src.services.vault_secret_store import get_vault_store
from tests.conftest import ScriptedToolModel, auth_header, user_id_of
from tests.test_agentspec.conftest import spec_payload
from tests.test_api.test_connection_routes import _FakeVault

pytestmark = pytest.mark.asyncio


class _ControlPlaneReached(BaseException):
    """A `BaseException`, not `Exception` — see `RealToolReached` (`tests.conftest`)
    for why: this must escape ordinary exception handling on the request path
    rather than be swallowed as "the pull just failed".
    """


@pytest.fixture(autouse=True)
def control_plane_that_must_never_be_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "control_plane_url", "https://control-plane.vendor.test")
    monkeypatch.setattr(get_settings(), "control_plane_public_key", "irrelevant-for-this-test")

    def _explode(*args: object, **kwargs: object) -> None:
        raise _ControlPlaneReached("control_plane.pull_template chạy trong luồng tạo agent/trả lời câu hỏi — đây là sự cố")

    monkeypatch.setattr(control_plane.httpx, "get", _explode)


async def test_creating_an_agent_via_the_meta_agent_makes_zero_control_plane_calls(
    client: AsyncClient, creator_token: str
) -> None:
    document = spec_payload()
    model = FakeMessagesListChatModel(responses=[AIMessage(content=json.dumps(document, ensure_ascii=False))])
    app.dependency_overrides[get_chat_model_factory] = lambda: (lambda model_id: model)
    try:
        response = await client.post(
            "/api/v1/metaagent/draft-spec",
            json={"description": "Tôi cần trợ lý theo dõi công nợ quá hạn."},
            headers=auth_header(creator_token),
        )
    finally:
        app.dependency_overrides.pop(get_chat_model_factory, None)

    assert response.status_code == 200, response.text


async def test_a_sandbox_chat_turn_makes_zero_control_plane_calls(
    client: AsyncClient, agent_admin_token: str, db_session
) -> None:
    def _always_allow(request: AuthzRequest) -> Decision:
        return Decision(effect=Effect.ALLOW, reasons=[], row_filter={}, approver_roles=[], redact_fields=[], max_rows=100)

    app.dependency_overrides[get_vault_store] = lambda: _FakeVault()
    app.dependency_overrides[get_pep_client] = lambda: _always_allow
    model = ScriptedToolModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "erp.query_receivables", "args": {}, "id": "c1"}]),
            AIMessage(content="7 khách quá hạn."),
        ]
    )
    app.dependency_overrides[get_chat_model_factory] = lambda: (lambda model_id: model)

    payload = spec_payload(metadata={**spec_payload()["metadata"], "id": "agt_cp_never_reached", "version": 1})
    db_session.add(
        UC1AgentSpecVersion(agent_id="agt_cp_never_reached", version=1, spec=payload, created_by_id=user_id_of(agent_admin_token))
    )
    db_session.commit()

    try:
        response = await client.post(
            "/api/v1/agents/agt_cp_never_reached/versions/1/sandbox/chat",
            json={"message": "Khách nào quá hạn trên 30 ngày?"},
            headers=auth_header(agent_admin_token),
        )
    finally:
        for dep in (get_vault_store, get_pep_client, get_chat_model_factory):
            app.dependency_overrides.pop(dep, None)

    assert response.status_code == 200, response.text
