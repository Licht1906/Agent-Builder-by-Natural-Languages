"""Where the exported package's approved mail goes — DS-16 / ADR-0059 §10.

The package `test_standalone_runtime` builds, its workflow ending in a
`human_approval` in front of `gmail.send_email`. The ticket's two criteria:

- through MailHog (the compose file's `mailhog` service): the mail arrives
  after the asker approves it, and does not when they reject it;
- `mail.mode` switched with the same bundle file: the mail leaves through the
  other channel, the bundle untouched.

Beside them, each mode's own promises — STARTTLS before a password, a
refresh token exchanged once and cached, a company adapter that breaks being
a mail not sent — and the start-up refusals.
"""

import base64
import email
import json
import smtplib
from collections.abc import Callable, Iterator
from email.policy import default as default_policy
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from src.standalone.app import create_app
from src.standalone.config import MailConfig, RuntimeConfig, RuntimeConfigError, required_secrets
from src.standalone.mail import MailChannelError, build_mail_sender, mail_invoker
from src.standalone.package import Package, prepare_database
from src.workflow.tools import GMAIL
from tests.conftest import ScriptedRun, parse_sse
from tests.test_standalone.test_standalone_runtime import (
    OVERDUE,
    SOURCE_ID,
    _loaded,
    _platform_factory,
    _runtime,
    _views,
    _writes,
    package_dir,
    secrets_env,
)

pytestmark = pytest.mark.asyncio

# Scenery borrowed from `test_standalone_runtime`, named so pytest sees it.
__all__ = ["_views", "package_dir", "secrets_env"]

MAILHOG_API = "http://localhost:8025/api"
REMINDER = {
    "to": "khach@example.com",
    "subject": "Nhắc công nợ quá hạn",
    "body": "Kính gửi quý khách, hoá đơn C1 đã quá hạn 45 ngày.",
}
SMTP_TO_MAILHOG = {
    "mode": "smtp",
    "smtp": {"host": "localhost", "port": 1025, "from": "agent@cty.vn", "starttls": False},
}


def _workflow() -> dict[str, Any]:
    """`data_query → draft → human_approval → gmail.send_email → report → end`, rejected going straight to report."""

    def edge(index: int, source: str, port: str, target: str) -> dict[str, Any]:
        return {"id": f"e{index}", "source_node_id": source, "source_port": port, "target_node_id": target}

    return {
        "start_node_id": "query",
        "nodes": [
            {"id": "query", "type": "data_query", "config": {"source_id": SOURCE_ID, "views": ["v_sa_ar"]}},
            {"id": "draft", "type": "agent_llm", "config": {"prompt": "Soạn mail nhắc nợ.", "model": "gpt-4o-mini"}},
            {
                "id": "approval",
                "type": "human_approval",
                "config": {"approver_role": "manager", "message_template": "Gửi mail nhắc nợ này?"},
            },
            {"id": "send", "type": "mcp", "config": {"tool_id": GMAIL, "action": "send_email"}},
            {"id": "report", "type": "agent_llm", "config": {"prompt": "Báo lại.", "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            edge(1, "query", "done", "draft"),
            edge(2, "draft", "done", "approval"),
            edge(3, "approval", "approved", "send"),
            edge(4, "approval", "rejected", "report"),
            edge(5, "send", "done", "report"),
            edge(6, "report", "done", "finish"),
        ],
        "metadata": {},
    }


def _fills(**args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "send_email", "args": args, "id": "fill", "type": "tool_call"}])


@pytest.fixture
def mailhog() -> Iterator[Callable[[], list[email.message.EmailMessage]]]:
    """MailHog's inbox, emptied first — skipped when the compose service is not up (`docker compose up -d mailhog`)."""
    try:
        httpx.delete(f"{MAILHOG_API}/v1/messages", timeout=2).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("MailHog không chạy ở localhost:8025 — docker compose up -d mailhog")

    def inbox() -> list[email.message.EmailMessage]:
        items = httpx.get(f"{MAILHOG_API}/v2/messages", timeout=2).json()["items"]
        return [
            email.message_from_string(item["Raw"]["Data"], policy=default_policy)  # type: ignore[misc]
            for item in items
        ]

    yield inbox
    httpx.delete(f"{MAILHOG_API}/v1/messages", timeout=2)


async def _paused(package: Package, scripted: Callable[[list[list[AIMessage]]], ScriptedRun]) -> tuple[Any, int]:
    """A client on `package`, and the approval its first turn stopped at — the mail drafted, not yet sent."""
    app = create_app(package, model_factory=_platform_factory())
    app.state.ids = prepare_database(package)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://package")
    scripted([[_writes(OVERDUE)], [AIMessage(content="Bản nháp mail.")], [_fills(**REMINDER)]])
    paused = await client.post(
        "/chat", json={"message": "nhắc khach@example.com về hoá đơn C1"}, headers={"X-Asker": "an@cty.vn"}
    )
    [event] = parse_sse(paused.text, event="awaiting_approval")
    # Earlier tests' pauses are still pending on the same agent; this run's is the one its stream named.
    pending = (await client.get("/approvals", headers={"X-Asker": "an@cty.vn"})).json()
    [waiting] = [row for row in pending if row["approval_id"] == event["approval_id"]]
    assert waiting["actions"][0]["args"] == REMINDER
    return client, waiting["approval_id"]


async def _decide(client: AsyncClient, approval_id: int, *, approved: bool) -> Any:
    decision: dict[str, Any] = {"approved": approved} if approved else {"approved": False, "reason": "Sai người nhận"}
    return await client.post(f"/approvals/{approval_id}", json=decision, headers={"X-Asker": "an@cty.vn"})


def _send_results(response: Any) -> list[dict[str, Any]]:
    return [line for line in parse_sse(response.text, event="tool_result") if line["tool"] == f"{GMAIL}.send_email"]


# --------------------------------------------------------------------------- #
# The acceptance criteria
# --------------------------------------------------------------------------- #


async def test_an_approved_mail_arrives_in_mailhog_exactly_as_shown(
    package_dir: Callable[..., Path],
    secrets_env: None,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    mailhog: Callable[[], list[email.message.EmailMessage]],
) -> None:
    package = _loaded(package_dir(workflow=_workflow(), mail=SMTP_TO_MAILHOG))
    client, approval_id = await _paused(package, scripted_chat_model_factory)
    assert mailhog() == [], "nothing may leave before the decision"

    scripted_chat_model_factory([[AIMessage(content="Đã gửi mail nhắc.")]])
    resumed = await _decide(client, approval_id, approved=True)
    await client.aclose()

    assert resumed.status_code == 200, resumed.text
    assert [line["ok"] for line in _send_results(resumed)] == [True]
    [mail] = mailhog()
    assert (mail["To"], mail["From"], mail["Subject"]) == ("khach@example.com", "agent@cty.vn", REMINDER["subject"])
    assert mail.get_content().strip() == REMINDER["body"]


async def test_a_rejected_mail_never_reaches_mailhog(
    package_dir: Callable[..., Path],
    secrets_env: None,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    mailhog: Callable[[], list[email.message.EmailMessage]],
) -> None:
    package = _loaded(package_dir(workflow=_workflow(), mail=SMTP_TO_MAILHOG))
    client, approval_id = await _paused(package, scripted_chat_model_factory)

    scripted_chat_model_factory([[AIMessage(content="Không gửi.")]])
    resumed = await _decide(client, approval_id, approved=False)
    await client.aclose()

    assert resumed.status_code == 200, resumed.text
    assert _send_results(resumed) == []
    assert mailhog() == []


async def test_switching_mail_mode_needs_no_change_to_the_bundle(
    package_dir: Callable[..., Path],
    secrets_env: None,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
    tmp_path: Path,
) -> None:
    """The same `bundle.json`, byte for byte, sending through a company adapter instead of SMTP."""
    directory = package_dir(workflow=_workflow(), mail=SMTP_TO_MAILHOG)
    bundle = (directory / "bundle.json").read_bytes()
    outbox = tmp_path / "outbox.jsonl"
    (directory / "mail_adapter.py").write_text(
        "import json\n"
        "def send_email(to, subject, body):\n"
        f"    with open({str(outbox)!r}, 'a', encoding='utf-8') as out:\n"
        "        out.write(json.dumps([to, subject, body], ensure_ascii=False) + '\\n')\n"
        "    return 'noi-bo-001'\n",
        encoding="utf-8",
    )
    package_dir(workflow=_workflow(), mail={"mode": "custom", "custom": {"module": "mail_adapter.py"}})
    assert (directory / "bundle.json").read_bytes() == bundle

    client, approval_id = await _paused(_loaded(directory), scripted_chat_model_factory)
    scripted_chat_model_factory([[AIMessage(content="Đã gửi.")]])
    resumed = await _decide(client, approval_id, approved=True)
    await client.aclose()

    [sent] = _send_results(resumed)
    assert sent["ok"] is True and "noi-bo-001" in sent["result"]
    [line] = outbox.read_text(encoding="utf-8").splitlines()
    to, subject, body = json.loads(line)
    assert (to, subject, body.strip()) == (REMINDER["to"], REMINDER["subject"], REMINDER["body"])


# --------------------------------------------------------------------------- #
# Start-up
# --------------------------------------------------------------------------- #


def test_a_bundle_that_sends_mail_does_not_start_without_a_mail_channel(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    with pytest.raises(RuntimeConfigError) as caught:
        _loaded(package_dir(workflow=_workflow()))

    assert "gmail.send_email" in str(caught.value) and "mail.mode" in str(caught.value)


def test_a_bundle_that_sends_nothing_starts_without_one(package_dir: Callable[..., Path], secrets_env: None) -> None:
    package = _loaded(package_dir())

    assert package.config.mail is None


def test_a_mailbox_action_the_package_cannot_perform_is_warned_about(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    workflow = _workflow()
    workflow["nodes"].insert(2, {"id": "inbox", "type": "mcp", "config": {"tool_id": GMAIL, "action": "search_emails"}})
    workflow["edges"][1] = {"id": "e2", "source_node_id": "draft", "source_port": "done", "target_node_id": "inbox"}
    workflow["edges"].append(
        {"id": "e7", "source_node_id": "inbox", "source_port": "done", "target_node_id": "approval"}
    )

    package = _loaded(package_dir(workflow=workflow, mail=SMTP_TO_MAILHOG))

    assert any("gmail.search_emails" in warning for warning in package.warnings)


def test_an_smtp_password_never_travels_without_starttls() -> None:
    with pytest.raises(ValueError, match="starttls"):
        MailConfig.model_validate(
            {"mode": "smtp", "smtp": {"host": "smtp", "from": "a@cty.vn", "password_secret": "p", "starttls": False}}
        )


def test_the_mail_channels_secrets_are_required_secrets() -> None:
    smtp = RuntimeConfig.model_validate(
        _runtime(mail={"mode": "smtp", "smtp": {"host": "smtp", "from": "a@cty.vn", "password_secret": "smtp_pass"}})
    )
    gmail = RuntimeConfig.model_validate(
        _runtime(
            mail={
                "mode": "gmail_oauth",
                "gmail_oauth": {
                    "client_id": "c",
                    "client_secret_secret": "g_secret",
                    "refresh_token_secret": "g_refresh",
                },
            }
        )
    )

    assert "smtp_pass" in required_secrets(smtp)
    assert {"g_secret", "g_refresh"} <= set(required_secrets(gmail))


def test_a_mode_named_without_its_settings_or_a_custom_file_without_its_function_refuses_to_start(
    tmp_path: Path,
) -> None:
    (tmp_path / "mail_adapter.py").write_text("def gui_mail(to, subject, body):\n    return 'x'\n", encoding="utf-8")

    with pytest.raises(RuntimeConfigError, match="mail.smtp"):
        build_mail_sender(MailConfig(mode="smtp"), secrets={}, base=tmp_path)
    with pytest.raises(RuntimeConfigError, match="send_email"):
        build_mail_sender(MailConfig.model_validate({"mode": "custom", "custom": {}}), secrets={}, base=tmp_path)


# --------------------------------------------------------------------------- #
# Each channel
# --------------------------------------------------------------------------- #


def test_smtp_starts_tls_before_it_logs_in(monkeypatch: pytest.MonkeyPatch) -> None:
    said: list[str] = []

    class _Relay:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            said.append(f"connect {host}:{port}")

        def __enter__(self) -> "_Relay":
            return self

        def __exit__(self, *exc: object) -> None:
            said.append("quit")

        def starttls(self, context: Any) -> None:
            said.append("starttls")

        def login(self, user: str, password: str) -> None:
            said.append(f"login {user}:{password}")

        def send_message(self, message: Any) -> dict[str, Any]:
            said.append(f"send {message['From']} -> {message['To']}")
            return {}

    monkeypatch.setattr(smtplib, "SMTP", _Relay)
    config = MailConfig.model_validate(
        {"mode": "smtp", "smtp": {"host": "smtp.cty", "from": "agent@cty.vn", "user": "u", "password_secret": "p"}}
    )

    result = build_mail_sender(config, secrets={"p": "mat-khau"}, base=Path("."))(*REMINDER.values())

    assert said == [
        "connect smtp.cty:587",
        "starttls",
        "login u:mat-khau",
        "send agent@cty.vn -> khach@example.com",
        "quit",
    ]
    assert json.loads(result)["sent"] is True


def test_gmail_oauth_exchanges_the_refresh_token_once_and_sends_as_the_companys_account() -> None:
    calls: list[tuple[str, str]] = []

    def google(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get("Authorization", "")))
        if request.url.path == "/token":
            assert b"refresh_token=rt-cong-ty" in request.content
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        sent = email.message_from_bytes(
            base64.urlsafe_b64decode(json.loads(request.content)["raw"]), policy=default_policy
        )
        assert sent["To"] == "khach@example.com"
        return httpx.Response(200, json={"id": f"gm-{len(calls)}"})

    config = MailConfig.model_validate(
        {
            "mode": "gmail_oauth",
            "gmail_oauth": {
                "client_id": "cid",
                "client_secret_secret": "cs",
                "refresh_token_secret": "rt",
                "token_url": "https://oauth2.googleapis.com/token",
            },
        }
    )
    send = build_mail_sender(
        config, secrets={"cs": "bi-mat", "rt": "rt-cong-ty"}, base=Path("."), transport=httpx.MockTransport(google)
    )

    first, second = send(*REMINDER.values()), send(*REMINDER.values())

    assert [path for path, _ in calls] == [
        "/token",
        "/gmail/v1/users/me/messages/send",
        "/gmail/v1/users/me/messages/send",
    ]
    assert all(header == "Bearer at-1" for path, header in calls if path != "/token")
    assert json.loads(first)["message_id"] == "gm-2" and json.loads(second)["message_id"] == "gm-3"


def test_gmail_oauth_says_so_when_the_refresh_token_is_refused() -> None:
    config = MailConfig.model_validate(
        {
            "mode": "gmail_oauth",
            "gmail_oauth": {"client_id": "c", "client_secret_secret": "cs", "refresh_token_secret": "rt"},
        }
    )
    send = build_mail_sender(
        config,
        secrets={"cs": "x", "rt": "het-han"},
        base=Path("."),
        transport=httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "invalid_grant"})),
    )

    with pytest.raises(MailChannelError, match="refresh token"):
        send(*REMINDER.values())


def test_a_company_adapter_that_breaks_or_returns_nothing_is_a_mail_not_sent(tmp_path: Path) -> None:
    (tmp_path / "hong.py").write_text(
        "def send_email(to, subject, body):\n    raise OSError('relay down')\n", encoding="utf-8"
    )
    (tmp_path / "rong.py").write_text("def send_email(to, subject, body):\n    return None\n", encoding="utf-8")

    def sender(module: str) -> Callable[..., str]:
        return build_mail_sender(
            MailConfig.model_validate({"mode": "custom", "custom": {"module": module}}), secrets={}, base=tmp_path
        )

    with pytest.raises(MailChannelError, match="relay down"):
        sender("hong.py")(*REMINDER.values())
    with pytest.raises(MailChannelError, match="phải trả về"):
        sender("rong.py")(*REMINDER.values())


def test_the_channel_sends_and_does_nothing_else_with_the_gmail_connection() -> None:
    sent: list[tuple[str, str, str]] = []
    invoke = mail_invoker(lambda to, subject, body: sent.append((to, subject, body)) or "{}")

    with pytest.raises(MailChannelError, match="gmail.search_emails"):
        invoke("search_emails", {"query": "from:sep"})
    invoke("send_email", REMINDER)

    assert sent == [tuple(REMINDER.values())]


def test_no_mail_channel_refuses_a_send_by_name() -> None:
    with pytest.raises(MailChannelError, match="mail.mode"):
        build_mail_sender(None, secrets={}, base=Path("."))(*REMINDER.values())
