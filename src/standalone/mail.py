"""Where the exported package's mail goes — DS-16 / ADR-0059 §10.

On the platform `gmail.send_email` sends as the agent's creator, with their
own OAuth token. Neither exists inside a company's network, so the package
puts a **mail channel** in place of that one implementation — chosen by
`runtime.yaml`'s `mail.mode`, never by the bundle:

- `smtp` — the company's relay, STARTTLS unless switched off, `from` fixed
  by the configuration (a model never chooses who a mail is from);
- `gmail_oauth` — a Gmail account the company owns, reached with a refresh
  token kept as a package secret;
- `custom` — a file the company writes with exactly one function,
  `send_email(to, subject, body) -> str`.

**Only how the mail leaves changes.** The channel is handed to the runtime as
an invoker for the `gmail` connection (`WorkflowRunContext.invokers`), so the
action, its risk, the `human_approval` in front of it and DS-08's payload
binding are the registry's and the runtime's, exactly as on the platform: an
unapproved or rewritten send is refused before a channel is ever called.

Every other `gmail` action reads somebody's mailbox, and the package has no
mailbox to read — each is refused by name rather than attempted.
"""

import base64
import importlib.util
import json
import smtplib
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from email.utils import make_msgid
from pathlib import Path
from typing import Any

import httpx

from src.standalone.config import GmailOauthSettings, MailConfig, RuntimeConfigError, SmtpSettings
from src.workflow.google_tools import GMAIL_API, email_message
from src.workflow.tools import ToolInvoker

#: `(to, subject, body) -> what the run is told` — one sent mail, or an exception saying why not.
MailSender = Callable[[str, str, str], str]

#: The one `gmail` action the package can perform.
SEND_ACTION = "send_email"

#: The function a `custom` adapter must define, and nothing else is looked for.
CUSTOM_FUNCTION = "send_email"


class MailChannelError(RuntimeError):
    """A mail that did not leave, said so the run — and the person reading it — can act on it."""


def build_mail_sender(
    config: MailConfig | None,
    *,
    secrets: Mapping[str, str],
    base: Path,
    transport: httpx.BaseTransport | None = None,
) -> MailSender:
    """The channel `config` describes — `RuntimeConfigError` naming what is missing otherwise.

    No `mail` at all is a channel that refuses every send, by name: the
    bundle may never send (start-up refuses one that can — `load_package`).
    `transport` is the `gmail_oauth` test seam.
    """
    if config is None:
        return _unconfigured
    if config.mode == "smtp":
        if config.smtp is None:
            raise RuntimeConfigError("mail.mode là 'smtp' nhưng thiếu mail.smtp (host, port, from)")
        password = secrets.get(config.smtp.password_secret) if config.smtp.password_secret else None
        return _smtp(config.smtp, password)
    if config.mode == "gmail_oauth":
        if config.gmail_oauth is None:
            raise RuntimeConfigError(
                "mail.mode là 'gmail_oauth' nhưng thiếu mail.gmail_oauth (client_id, client_secret_secret, "
                "refresh_token_secret)"
            )
        return _GmailOauth(config.gmail_oauth, secrets, transport=transport).send
    if config.custom is None:
        raise RuntimeConfigError("mail.mode là 'custom' nhưng thiếu mail.custom (module)")
    return _custom(_send_email_of(_path(config.custom.module, base)))


def mail_invoker(send: MailSender) -> ToolInvoker:
    """`send` as the `gmail` connection's implementation — what `WorkflowRunContext.invokers` maps `gmail` to."""

    def invoke(action: str, args: Mapping[str, Any]) -> str:
        if action != SEND_ACTION:
            raise MailChannelError(
                f"Gói chỉ có kênh gửi mail — action gmail.{action} cần hộp thư của ai đó nên không chạy trong gói"
            )
        return send(str(args.get("to", "")), str(args.get("subject", "")), str(args.get("body", "")))

    return invoke


def _unconfigured(to: str, subject: str, body: str) -> str:
    raise MailChannelError("Gói chưa cấu hình kênh mail (mail.mode trong runtime.yaml) nên không gửi được")


def _sent(message_id: str | None, **extra: Any) -> str:
    return json.dumps({"message_id": message_id, "sent": True, **extra}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# smtp
# --------------------------------------------------------------------------- #


def _smtp(settings: SmtpSettings, password: str | None) -> MailSender:
    def send(to: str, subject: str, body: str) -> str:
        message = email_message({"to": to, "subject": subject, "body": body})
        # 7-bit safe on the wire: a relay without 8BITMIME would mangle a Vietnamese body sent as raw 8bit.
        message.set_content(message.get_content(), cte="quoted-printable")
        message["From"] = settings.sender
        message["Message-ID"] = make_msgid(domain=settings.sender.rpartition("@")[2] or None)
        try:
            with smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout_seconds) as client:
                if settings.starttls:
                    client.starttls(context=ssl.create_default_context())
                if settings.user:
                    client.login(settings.user, password or "")
                refused = client.send_message(message)
        except smtplib.SMTPAuthenticationError:
            raise MailChannelError("Máy chủ SMTP từ chối tài khoản trong mail.smtp") from None
        except smtplib.SMTPRecipientsRefused:
            raise MailChannelError(f"Máy chủ SMTP từ chối người nhận {to}") from None
        except (smtplib.SMTPException, OSError) as exc:
            raise MailChannelError(f"Không gửi được qua SMTP {settings.host}:{settings.port}: {exc}") from None
        if refused:
            raise MailChannelError(f"Máy chủ SMTP từ chối người nhận {', '.join(refused)}")
        return _sent(str(message["Message-ID"]))

    return send


# --------------------------------------------------------------------------- #
# gmail_oauth
# --------------------------------------------------------------------------- #


class _GmailOauth:
    """Gmail's send endpoint, with an access token refreshed from the package's refresh token and cached."""

    #: Refreshed this long before Google says it expires, so a send never carries a token that dies in flight.
    _MARGIN_SECONDS = 60

    def __init__(
        self, settings: GmailOauthSettings, secrets: Mapping[str, str], *, transport: httpx.BaseTransport | None
    ) -> None:
        self._settings = settings
        self._client_secret = secrets[settings.client_secret_secret]
        self._refresh_token = secrets[settings.refresh_token_secret]
        self._transport = transport
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def send(self, to: str, subject: str, body: str) -> str:
        message = email_message({"to": to, "subject": subject, "body": body})
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        reply = self._post(
            f"{GMAIL_API}/messages/send", json={"raw": raw}, headers={"Authorization": f"Bearer {self._access()}"}
        )
        if reply.status_code == 401:
            self._token = None  # revoked or expired early: the next send refreshes
            raise MailChannelError("Gmail từ chối access token của gói — kiểm tra refresh token trong secret")
        if reply.status_code >= 400:
            raise MailChannelError(f"Gmail không gửi được (HTTP {reply.status_code})")
        return _sent(reply.json().get("id"))

    def _access(self) -> str:
        with self._lock:
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token
            reply = self._post(
                self._settings.token_url,
                data={
                    "client_id": self._settings.client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if reply.status_code >= 400:
                raise MailChannelError(
                    f"Không đổi được refresh token lấy access token Gmail (HTTP {reply.status_code}) — "
                    "kiểm tra client_id, client secret và refresh token"
                )
            body = reply.json()
            self._token = str(body["access_token"])
            self._expires_at = time.monotonic() + max(int(body.get("expires_in", 3600)) - self._MARGIN_SECONDS, 0)
            return self._token

    def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            with httpx.Client(timeout=10, transport=self._transport) as client:
                return client.post(url, **kwargs)
        except httpx.HTTPError as exc:
            raise MailChannelError(f"Không gọi được Google: {exc}") from None


# --------------------------------------------------------------------------- #
# custom
# --------------------------------------------------------------------------- #


def _custom(send_email: Callable[[str, str, str], Any]) -> MailSender:
    def send(to: str, subject: str, body: str) -> str:
        # The platform's own rules for the three fields, so the adapter is handed what SMTP would have sent.
        message = email_message({"to": to, "subject": subject, "body": body})
        try:
            result = send_email(str(message["To"]), str(message["Subject"]), message.get_content())
        except Exception as exc:  # noqa: BLE001 — the company's adapter failing is a mail not sent, not a crash
            raise MailChannelError(f"mail_adapter.{CUSTOM_FUNCTION} báo lỗi: {type(exc).__name__}: {exc}") from None
        if not isinstance(result, str) or not result.strip():
            raise MailChannelError(f"mail_adapter.{CUSTOM_FUNCTION} phải trả về một chuỗi (mã thư đã gửi)")
        return _sent(result.strip())

    return send


def _send_email_of(path: Path) -> Callable[[str, str, str], Any]:
    if not path.is_file():
        raise RuntimeConfigError(f"mail.mode là 'custom' nhưng không có file {path}")
    spec = importlib.util.spec_from_file_location("package_mail_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeConfigError(f"Không nạp được {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — any failure of the company's file is a reason not to start
        raise RuntimeConfigError(f"{path} lỗi khi nạp: {type(exc).__name__}: {exc}") from None
    send_email = getattr(module, CUSTOM_FUNCTION, None)
    if not callable(send_email):
        raise RuntimeConfigError(f"{path} phải có hàm {CUSTOM_FUNCTION}(to, subject, body) -> str")
    return send_email


def _path(value: str, base: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path
