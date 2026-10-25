"""UC1-12 — one test per rule the PEP client enforces.

Seam: `_evaluate` (returned by `get_pep_client`), the only function allowed to
reach OPA. `httpx.post` is monkeypatched on the `pep` module object —
`monkeypatch.setattr(pep.httpx, "post", ...)`, the same shape
`tests/test_api/test_workflow_sandbox.py`'s `a_configured_search_api` fixture
uses for `builtin.httpx` — rather than a live sidecar, matching this repo's
model-factory seam principle (§20): a fake with a scripted response, no live
service, no flaky test.

This module's own real-sidecar verification was manual, not automated: `opa
check`/`opa eval` against `authz.rego`, then `opa run --server` hit over HTTP
with the sample input from the file's header, confirming both an `allow` and a
`deny` decision round-trip correctly. Not captured here as a test because it
needs a live OPA binary this suite cannot assume — UC1-13 (#114) owns the
durable, CI-covered version of that coverage (`authz_test.rego` + `opa test`).
"""

from typing import Any

import httpx
import pytest

import src.policy.pep as pep
from src.policy.pep import AuthzRequest, Effect, PEPUnavailableError, get_pep_client


def _stub_response(status_code: int, body: Any) -> Any:
    def _respond(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(status_code, json=body, request=httpx.Request("POST", url))

    return _respond


def _decision_body(effect: str) -> dict[str, Any]:
    return {
        "result": {
            "effect": effect,
            "reasons": ["some reason"] if effect != "allow" else [],
            "row_filter": {"scope": "own_records", "principal_id": "E102"},
            "approver_roles": ["agent_admin"],
            "redact_fields": [],
            "max_rows": 100,
        }
    }


def test_the_request_sent_matches_authz_rego_documented_input_shape(
    monkeypatch: pytest.MonkeyPatch, authz_request: AuthzRequest
) -> None:
    """The header of `authz.rego` names exactly these six top-level keys — no more, no fewer."""
    sent: dict[str, Any] = {}

    def _capture(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        sent.update(json)
        return httpx.Response(200, json=_decision_body("allow"), request=httpx.Request("POST", url))

    monkeypatch.setattr(pep.httpx, "post", _capture)

    get_pep_client()(authz_request)

    assert set(sent["input"].keys()) == {"spec", "principal", "action", "provenance", "session", "approvals"}
    assert sent["input"]["principal"] == {
        "sub": "u1",
        "employee_id": "E102",
        "roles": ["sales_rep"],
        "department": "sales",
    }
    assert sent["input"]["spec"]["metadata"]["status"] == "deployed"


def test_a_decision_parses_for_every_effect_value(
    monkeypatch: pytest.MonkeyPatch, authz_request: AuthzRequest
) -> None:
    for effect in Effect:
        monkeypatch.setattr(pep.httpx, "post", _stub_response(200, _decision_body(effect.value)))
        decision = get_pep_client()(authz_request)
        assert decision.effect is effect
        assert decision.allowed is (effect is Effect.ALLOW)
        assert decision.row_filter == {"scope": "own_records", "principal_id": "E102"}
        assert decision.max_rows == 100


def test_transport_or_http_failure_raises_rather_than_returning_a_decision(
    monkeypatch: pytest.MonkeyPatch, authz_request: AuthzRequest
) -> None:
    """A dead sidecar and a 5xx sidecar are the same failure: never mistaken for a `Decision`."""

    def _connection_refused(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(pep.httpx, "post", _connection_refused)
    with pytest.raises(PEPUnavailableError):
        get_pep_client()(authz_request)

    monkeypatch.setattr(pep.httpx, "post", _stub_response(500, {"error": "internal"}))
    with pytest.raises(PEPUnavailableError):
        get_pep_client()(authz_request)


def test_a_response_this_module_cannot_trust_raises_rather_than_being_treated_as_a_decision(
    monkeypatch: pytest.MonkeyPatch, authz_request: AuthzRequest
) -> None:
    """A 200 with an unusable body is exactly as dangerous as no response — never silently `allow`."""
    missing_result = _stub_response(200, {"not_result": {}})
    monkeypatch.setattr(pep.httpx, "post", missing_result)
    with pytest.raises(PEPUnavailableError):
        get_pep_client()(authz_request)

    unknown_effect = _stub_response(200, _decision_body("maybe"))
    monkeypatch.setattr(pep.httpx, "post", unknown_effect)
    with pytest.raises(PEPUnavailableError):
        get_pep_client()(authz_request)
