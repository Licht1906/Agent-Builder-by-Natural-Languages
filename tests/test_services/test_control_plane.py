"""UC1-26 (#142) — `pull_template`'s own rules, at the `httpx.get` seam
(`monkeypatch.setattr(control_plane.httpx, "get", ...)`, the same shape
`tests/test_policy/test_pep.py` uses for `httpx.post`), no live control plane.
"""

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import src.services.control_plane as control_plane
from src.config import get_settings
from src.services.control_plane import ControlPlaneError, pull_template


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch) -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(private_key.public_key().public_bytes_raw()).decode()
    monkeypatch.setattr(get_settings(), "control_plane_url", "https://control-plane.vendor.test")
    monkeypatch.setattr(get_settings(), "control_plane_public_key", public_b64)
    return private_key, public_b64


def _sign(private_key: Ed25519PrivateKey, content: dict) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(private_key.sign(canonical)).decode()


def _respond_with(monkeypatch: pytest.MonkeyPatch, body: dict) -> None:
    def _get(url: str, *, timeout: float) -> httpx.Response:
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(control_plane.httpx, "get", _get)


def test_a_correctly_signed_template_is_accepted(monkeypatch: pytest.MonkeyPatch, _configured) -> None:
    private_key, _ = _configured
    content = {"tools": ["erp.query_receivables"], "guardrails": {"input": []}}
    _respond_with(monkeypatch, {"content": content, "signature": _sign(private_key, content), "build_id": "2026.08.15"})

    pulled = pull_template("uc1-ar-assistant")

    assert pulled.template_id == "uc1-ar-assistant"
    assert pulled.build_id == "2026.08.15"
    assert pulled.content == content


def test_a_tampered_template_is_refused_not_applied(monkeypatch: pytest.MonkeyPatch, _configured) -> None:
    private_key, _ = _configured
    signed_content = {"tools": ["erp.query_receivables"]}
    tampered_content = {"tools": ["erp.query_receivables", "mail.send_reminder"]}  # signed one thing, served another
    _respond_with(
        monkeypatch, {"content": tampered_content, "signature": _sign(private_key, signed_content), "build_id": "x"}
    )

    with pytest.raises(ControlPlaneError, match="chữ ký"):
        pull_template("uc1-ar-assistant")


def test_a_signature_from_the_wrong_key_is_refused(monkeypatch: pytest.MonkeyPatch, _configured) -> None:
    wrong_key = Ed25519PrivateKey.generate()
    content = {"tools": []}
    _respond_with(monkeypatch, {"content": content, "signature": _sign(wrong_key, content), "build_id": "x"})

    with pytest.raises(ControlPlaneError, match="chữ ký"):
        pull_template("uc1-ar-assistant")


def test_pulling_with_no_control_plane_url_configured_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "control_plane_url", "")

    with pytest.raises(ControlPlaneError, match="CONTROL_PLANE_URL"):
        pull_template("uc1-ar-assistant")


def test_the_pull_logs_only_the_identifiers_never_the_content(monkeypatch: pytest.MonkeyPatch, _configured) -> None:
    """Spies on `control_plane.logger.info` directly rather than through
    `caplog` — this suite runs inside a much larger session where some other
    test's logging configuration (handler levels, propagation) is not this
    module's to depend on; asserting on exactly what `pull_template` *tried*
    to log is robust to all of that.
    """
    private_key, _ = _configured
    content = {"secret_looking_field": "this must never appear in a log line"}
    _respond_with(monkeypatch, {"content": content, "signature": _sign(private_key, content), "build_id": "b7"})

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(control_plane.logger, "info", lambda *args: calls.append(args))

    pull_template("uc1-ar-assistant")

    logged = " ".join(str(arg) for call in calls for arg in call)
    assert "uc1-ar-assistant" in logged
    assert "b7" in logged
    assert "secret_looking_field" not in logged
    assert "this must never appear" not in logged


def test_a_denied_pull_is_logged_too(monkeypatch: pytest.MonkeyPatch, _configured) -> None:
    """"Log mỗi lần gọi" — every call, not only the ones that end up accepted.
    A tampered/mismatched signature must still leave a trace of the attempt.
    """
    wrong_key = Ed25519PrivateKey.generate()
    content = {"tools": []}
    _respond_with(monkeypatch, {"content": content, "signature": _sign(wrong_key, content), "build_id": "b9"})

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(control_plane.logger, "info", lambda *args: calls.append(args))

    with pytest.raises(ControlPlaneError):
        pull_template("uc1-ar-assistant")

    assert calls, "a denied pull must still be logged"
    logged = " ".join(str(arg) for call in calls for arg in call)
    assert "uc1-ar-assistant" in logged
    assert "denied" in logged
