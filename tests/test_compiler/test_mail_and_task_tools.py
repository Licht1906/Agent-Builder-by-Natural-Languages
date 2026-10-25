"""UC1-21 (#137) — `default_tool_implementations`'s `mail.send_reminder`/
`task.create`, unit-level (no PEP, no graph): these two are only ever reached
post-approval (`src.compiler.tools`'s own module docstring), so what they do
*once reached* is this file's concern, not whether they were gated — that is
`tests/test_compiler/test_pending_actions.py`'s and
`tests/test_api/test_uc1_action_approval_routes.py`'s job.
"""

import smtplib
from typing import Any

import pytest

from src.compiler.tools import RowScope, default_tool_implementations
from src.config import get_settings

_SCOPE = RowScope(principal_id="E102", department="SALES", scope="own_records")


def test_send_reminder_sends_a_real_email_when_smtp_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "smtp_host", "mailhog.test")
    sent: dict[str, Any] = {}

    class _FakeSMTP:
        def __init__(self, host: str, port: int) -> None:
            sent["host"] = host

        def __enter__(self) -> "_FakeSMTP":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def send_message(self, message: object) -> None:
            sent["message"] = message

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    impl = default_tool_implementations(resolve_credential=lambda ref: "")["mail.send_reminder"]

    result = impl({"customer_id": "cust-a", "amount_due": 500}, _SCOPE)

    assert result == {"sent": True, "to": "cust-a@customer.internal"}
    assert sent["host"] == "mailhog.test"


def test_send_reminder_degrades_honestly_when_smtp_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "smtp_host", "")
    impl = default_tool_implementations(resolve_credential=lambda ref: "")["mail.send_reminder"]

    result = impl({"customer_id": "cust-a"}, _SCOPE)

    assert result["sent"] is False


def test_create_task_defaults_the_assignee_to_the_caller_when_none_is_given() -> None:
    impl = default_tool_implementations(resolve_credential=lambda ref: "")["task.create"]

    result = impl({}, _SCOPE)

    assert result["created"] is True
    assert result["assignee_id"] == "E102"


def test_create_task_honours_an_explicit_assignee() -> None:
    impl = default_tool_implementations(resolve_credential=lambda ref: "")["task.create"]

    result = impl({"assignee_id": "E999"}, _SCOPE)

    assert result["assignee_id"] == "E999"
