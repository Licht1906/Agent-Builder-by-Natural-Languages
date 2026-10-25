"""What a screen is told when the model provider refuses — `src.main`, `src.services.llm`.

Measured on a real creator: a spec confirmed, *Tạo workflow* pressed, and an
OpenAI project sitting at its enforced spend limit. The provider's `429` escaped
`write_document` (it is not one of `RETRYABLE`), escaped `compose_workflow`,
escaped the fallback in `src.workflow.generator` (which catches
`GraphCompositionError` and nothing else), escaped the route (which catches the
two stage errors), and arrived as `500 Internal Server Error` with no `detail`
at all — which the frontend can only render as *"Yêu cầu thất bại (HTTP 500)"*.
The creator was left on an empty canvas being told, in effect, that the platform
is broken and that it might be something they wrote.

So the assertions here are about the *answer*, not about the exception: which
status a dependency being down deserves, and whether the sentence names the real
cause. The composer is the route under test only because it is where this was
found; the handler is one for the whole app, and the last test is what says so.
"""

from typing import Any

import httpx
import openai
import pytest
from httpx import AsyncClient

from src.db.models import AgentVersion, User, VersionStatus
from tests.conftest import auth_header, install_chat_model_factory

pytestmark = pytest.mark.asyncio

SPEC: dict[str, Any] = {
    "goal": "Kiểm chéo file trong Drive và tổng hợp thành báo cáo",
    "users": ["Nhân viên văn phòng"],
    "data": [{"name": "Thư mục Drive dự án", "kind": "connected_account", "ref": "conn://drive-1"}],
    "actions": ["Liệt kê file trong thư mục", "Tổng hợp thành báo cáo"],
    "expected_outcome": ["Một báo cáo nêu những điểm lệch giữa các file"],
    "success_criteria": [{"text": "Nêu đúng tên file bị lệch", "category": "correctness"}],
    "prohibitions": ["Không sửa file gốc"],
}


def _refusing(error: Exception) -> None:
    """Point the model seam at a provider that will not answer.

    Raised from the *factory* rather than from a fake model's `ainvoke`, which is
    both simpler and truer to the failure: an authentication or quota refusal is
    not specific to one call, and every path that reaches a model reaches this
    function first.
    """

    def _build(_model_id: str) -> Any:
        raise error

    install_chat_model_factory(_build)


def _openai_error(cls: type[Exception], code: str | None = None) -> Exception:
    """One `openai` error of `cls`, carrying `code` the way the live SDK does."""
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))
    return cls("refused", response=response, body={"code": code} if code else None)


@pytest.fixture
def confirmed_version(solution_spec_version: Any, seeded_creator: User) -> AgentVersion:
    """A version one press of *Tạo workflow* away from having a graph."""
    return solution_spec_version(seeded_creator, SPEC, status=VersionStatus.SPEC_CONFIRMED, name="Kiểm chéo Drive")


async def _draft(client: AsyncClient, token: str, version: AgentVersion) -> httpx.Response:
    return await client.post(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/workflow",
        headers=auth_header(token),
        json={"template_id": None},
    )


async def test_a_capped_account_answers_503_and_says_so(
    client: AsyncClient, creator_token: str, confirmed_version: AgentVersion
) -> None:
    """The measured failure, and the whole reason this module exists.

    503 rather than 500 is the load-bearing half: 500 says *this platform has a
    bug* about a request that is worth repeating verbatim once somebody raises
    the limit. `Retry-After` says the same thing to anything that is not a
    person.
    """
    _refusing(_openai_error(openai.RateLimitError, "project_spend_limit_exceeded"))

    response = await _draft(client, creator_token, confirmed_version)

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "60"
    detail = response.json()["detail"]
    assert "trần chi tiêu" in detail
    # The two things the creator on that screen actually needs to know, and the
    # two the old 500 could not say.
    assert "không phải của bạn" in detail
    assert "vẫn còn nguyên" in detail


async def test_the_creator_is_never_shown_the_providers_own_words(
    client: AsyncClient, creator_token: str, confirmed_version: AgentVersion
) -> None:
    """English prose naming an OpenAI settings URL is for the log, not the screen.

    The raw message is *"Your project has reached its configured enforced spend
    limit. Update your limit at https://platform.openai.com/settings/proj_…"* —
    an instruction addressed to somebody who is not the person reading it, at a
    page they cannot open, in a language the rest of the screen is not in.
    """
    _refusing(_openai_error(openai.RateLimitError, "project_spend_limit_exceeded"))

    detail = (await _draft(client, creator_token, confirmed_version)).json()["detail"]

    assert "platform.openai.com" not in detail
    assert "spend limit" not in detail


async def test_being_throttled_is_told_apart_from_being_out_of_money(
    client: AsyncClient, creator_token: str, confirmed_version: AgentVersion
) -> None:
    """Both arrive as `RateLimitError`; only `code` separates them, and the advice differs.

    One of them ends by itself in a minute and the other never does until an
    admin acts. Telling a creator to wait out a spend limit is telling them to
    wait forever.
    """
    _refusing(_openai_error(openai.RateLimitError))

    detail = (await _draft(client, creator_token, confirmed_version)).json()["detail"]

    assert "quá tải" in detail
    assert "trần chi tiêu" not in detail


async def test_a_rejected_key_is_named_as_configuration(
    client: AsyncClient, creator_token: str, confirmed_version: AgentVersion
) -> None:
    """An unusable key is an unfinished deployment, and the sentence has to say which."""
    _refusing(_openai_error(openai.AuthenticationError))

    detail = (await _draft(client, creator_token, confirmed_version)).json()["detail"]

    assert "Khoá API" in detail
    assert "cấu hình của nền tảng" in detail


async def test_an_unreachable_provider_is_not_reported_as_a_platform_bug(
    client: AsyncClient, creator_token: str, confirmed_version: AgentVersion
) -> None:
    """A socket that never connected is the clearest 503 there is."""
    _refusing(openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/x")))

    response = await _draft(client, creator_token, confirmed_version)

    assert response.status_code == 503
    assert "Không kết nối được" in response.json()["detail"]


async def test_the_handler_covers_every_route_that_reaches_a_model(
    client: AsyncClient, creator_token: str, seeded_creator: User
) -> None:
    """Not the composer's own rescue — one handler for the whole app.

    The interview is the other end of the same journey and a different router
    entirely, so it is the honest way to ask whether the translation belongs to
    the application rather than to one endpoint. A `try` per route would pass
    every test above and fail this one.
    """
    _refusing(_openai_error(openai.RateLimitError, "insufficient_quota"))

    response = await client.post(
        "/api/v1/solution-interviews",
        headers=auth_header(creator_token),
        json={"description": "Tôi cần một agent kiểm chéo file trong Drive rồi tổng hợp báo cáo"},
    )

    assert response.status_code == 503
    assert "trần chi tiêu" in response.json()["detail"]


async def test_a_platform_bug_is_still_a_platform_bug(
    client: AsyncClient, creator_token: str, confirmed_version: AgentVersion
) -> None:
    """The translation must not become a blanket that hides real 500s.

    A `RuntimeError` from our own code is exactly what `500` is for, and turning
    every failure into "the provider is unavailable" would buy a readable screen
    by making every bug in the platform unfindable.
    """
    _refusing(RuntimeError("một lỗi thật của nền tảng"))

    with pytest.raises(RuntimeError):
        await _draft(client, creator_token, confirmed_version)
