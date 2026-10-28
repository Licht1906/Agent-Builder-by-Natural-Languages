"""The exported package's chat page — DS-17 / ADR-0059 §10.

The page itself (sign-in, turns, the approval card) is tested in vitest
(`frontend/src/package/*.test.*`). Here: what the runtime serves it, what it
tells it, and the ticket's path end to end at the API the page drives —

  a demo employee signs in at a mock OIDC issuer → asks → the answer carries
  its sources → they approve the mail → the mail arrives in MailHog.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from src.standalone.app import create_app, ui_config
from src.standalone.package import Package, prepare_database
from tests.conftest import ScriptedRun, parse_sse
from tests.test_standalone.test_package_auth import AUDIENCE, ISSUER, MockIssuer
from tests.test_standalone.test_package_mail import REMINDER, SMTP_TO_MAILHOG, _fills, _workflow, mailhog
from tests.test_standalone.test_standalone_runtime import (
    OVERDUE,
    _loaded,
    _platform_factory,
    _views,
    _writes,
    package_dir,
    secrets_env,
)

pytestmark = pytest.mark.asyncio

# Scenery borrowed from the other package tests, named so pytest sees it.
__all__ = ["_views", "mailhog", "package_dir", "secrets_env"]

CLIENT_ID = AUDIENCE  # the page sends the ID token, whose audience is the client
OIDC = {"mode": "oidc", "claim": "email", "oidc": {"issuer": ISSUER, "audience": AUDIENCE, "client_id": CLIENT_ID}}


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    """A built chat page, as `npm run build:package` leaves it."""
    built = tmp_path / "dist-package"
    (built / "assets").mkdir(parents=True)
    (built / "index.html").write_text('<div id="root"></div><script src="/assets/app.js"></script>', encoding="utf-8")
    (built / "assets" / "app.js").write_text("console.log('chat')", encoding="utf-8")
    return built


def _client(package: Package, *, ui_dir: Path | None = None, issuer: MockIssuer | None = None) -> AsyncClient:
    app = create_app(
        package,
        model_factory=_platform_factory(),
        auth_transport=issuer.transport() if issuer else None,
        ui_dir=ui_dir,
    )
    app.state.ids = prepare_database(package)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://package")


# --------------------------------------------------------------------------- #
# What the runtime serves the page
# --------------------------------------------------------------------------- #


async def test_the_page_and_its_assets_are_served_at_the_root(
    package_dir: Callable[..., Path], secrets_env: None, ui_dir: Path
) -> None:
    async with _client(_loaded(package_dir()), ui_dir=ui_dir) as client:
        page = await client.get("/?code=abc&state=xyz")  # the issuer's redirect lands here, query and all
        script = await client.get("/assets/app.js")

    assert page.status_code == 200 and 'id="root"' in page.text
    assert script.status_code == 200 and "chat" in script.text
    assert page.headers["referrer-policy"] == "no-referrer", "the sign-in code must not leak in a Referer"
    assert page.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in page.headers["content-security-policy"]


async def test_without_a_build_the_page_says_so_and_the_api_still_answers(
    package_dir: Callable[..., Path], secrets_env: None, tmp_path: Path
) -> None:
    async with _client(_loaded(package_dir()), ui_dir=tmp_path / "missing") as client:
        page = await client.get("/")
        health = await client.get("/health")

    assert page.status_code == 404 and "/chat" in page.json()["detail"]
    assert health.status_code == 200


async def test_the_page_may_talk_to_its_own_origin_and_the_issuer_and_nowhere_else(
    package_dir: Callable[..., Path], secrets_env: None, ui_dir: Path
) -> None:
    async with _client(_loaded(package_dir(auth=OIDC)), ui_dir=ui_dir, issuer=MockIssuer()) as client:
        policy = (await client.get("/")).headers["content-security-policy"]

    assert "connect-src 'self' https://idp.cty.test;" in policy
    assert "fonts.googleapis.com" not in policy


def test_ui_config_says_how_to_sign_in_and_nothing_secret(package_dir: Callable[..., Path], secrets_env: None) -> None:
    oidc = ui_config(_loaded(package_dir(auth=OIDC)))
    jwt = ui_config(
        _loaded(package_dir(auth={"mode": "jwt", "jwt": {"key_secret": "llm_key", "algorithms": ["HS256"]}}))
    )
    header = ui_config(_loaded(package_dir()))

    assert oidc == {
        "agent": "Trợ lý công nợ",
        "auth": {"mode": "oidc", "oidc": {"issuer": ISSUER, "client_id": CLIENT_ID, "scope": "openid email profile"}},
    }
    assert jwt == {"agent": "Trợ lý công nợ", "auth": {"mode": "jwt"}}, "the jwt key is a secret and never described"
    assert header["auth"] == {"mode": "trusted_header"}, "the header a gateway sets is not the page's to know"


def test_an_oidc_package_without_a_client_id_warns_that_its_page_cannot_sign_in(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    package = _loaded(package_dir(auth={**OIDC, "oidc": {"issuer": ISSUER, "audience": AUDIENCE}}))

    assert any("client_id" in warning for warning in package.warnings)


# --------------------------------------------------------------------------- #
# The ticket's path, at the API the page drives
# --------------------------------------------------------------------------- #


async def test_a_signed_in_employee_asks_sees_sources_approves_and_the_mail_arrives(
    package_dir: Callable[..., Path],
    secrets_env: None,
    ui_dir: Path,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    mailhog: Callable[[], list[Any]],
) -> None:
    issuer = MockIssuer()
    package = _loaded(package_dir(workflow=_workflow(), auth=OIDC, mail=SMTP_TO_MAILHOG))
    signed_in = {"Authorization": f"Bearer {issuer.token({'email': 'an@cty.vn'})}"}

    async with _client(package, ui_dir=ui_dir, issuer=issuer) as client:
        config = (await client.get("/ui-config")).json()
        assert config["auth"]["oidc"]["client_id"] == CLIENT_ID
        refused = await client.post("/chat", json={"message": "xin chào"})
        assert refused.status_code == 401, "no sign-in, no chat"

        scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="Bản nháp mail.")], [_fills(**REMINDER)]])
        asked = await client.post("/chat", json={"message": "nhắc khach@example.com về hoá đơn C1"}, headers=signed_in)
        [read] = [line for line in parse_sse(asked.text, event="tool_result") if line["tool"] == "data_query"]
        [paused] = parse_sse(asked.text, event="awaiting_approval")
        assert read["ok"] is True and read["citations"], "the answer must carry its sources"
        assert "v_sa_ar" in str(read["citations"])
        assert paused["actions"][0]["args"] == REMINDER, "the card shows the mail verbatim"
        assert mailhog() == []

        scripted_chat_model_factory([[AIMessage(content="Đã gửi mail nhắc.")]])
        approved = await client.post(f"/approvals/{paused['approval_id']}", json={"approved": True}, headers=signed_in)

    assert approved.status_code == 200, approved.text
    [mail] = mailhog()
    assert (mail["To"], mail["Subject"]) == (REMINDER["to"], REMINDER["subject"])
