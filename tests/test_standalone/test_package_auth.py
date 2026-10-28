"""Who is asking, in the exported package — DS-14 / ADR-0059 §10.

One test group per `auth.mode`, against the package `test_standalone_runtime`
builds (real bundle, real Hệ CSDL, scripted model):

- `trusted_header` with and without the header;
- `jwt` signed right, signed wrong, expired, wrong issuer/audience, no claim;
- `oidc` against a mock issuer (discovery, JWKS, a key rotation);
- `custom` adapter — sync, async, raising, missing its function;

and the case every mode must end the same way: a valid identity the employee
view does not hold is answered "không có quyền", never a 500, never a default.
"""

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from src.standalone.app import create_app
from src.standalone.config import RuntimeConfigError
from src.standalone.package import Package, prepare_database
from tests.conftest import ScriptedRun, parse_sse
from tests.test_standalone.test_standalone_runtime import (
    OVERDUE,
    PREFIX,
    _loaded,
    _platform_factory,
    _rows_read,
    _views,
    _writes,
    package_dir,
    secrets_env,
)

pytestmark = pytest.mark.asyncio

# Scenery borrowed from `test_standalone_runtime`, named so pytest sees it.
__all__ = ["_views", "package_dir", "secrets_env"]

JWT_KEY = "khoa-bi-mat-cua-cong-ty-dai-it-nhat-32-byte!!"
ISSUER = "https://idp.cty.test"
AUDIENCE = "tro-ly-cong-no"


async def _client(package: Package, *, transport: httpx.BaseTransport | None = None) -> AsyncClient:
    app = create_app(package, model_factory=_platform_factory(), auth_transport=transport)
    app.state.ids = prepare_database(package)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://package")


async def _status(package: Package, headers: dict[str, str], *, transport: httpx.BaseTransport | None = None) -> int:
    """What `/approvals` answers — a route that needs nothing but an asker."""
    async with await _client(package, transport=transport) as client:
        return (await client.get("/approvals", headers=headers)).status_code


def _hs256(claims: dict[str, Any], *, key: str = JWT_KEY) -> str:
    return jwt.encode({"exp": int(time.time()) + 300, **claims}, key, algorithm="HS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# trusted_header
# --------------------------------------------------------------------------- #


async def test_a_trusted_header_names_the_asker_and_its_absence_is_a_401(
    package_dir: Callable[..., Path], secrets_env: None
) -> None:
    package = _loaded(package_dir(auth={"mode": "trusted_header", "header": "X-Nguoi-Hoi"}))

    assert await _status(package, {"X-Nguoi-Hoi": "an@cty.vn"}) == 200
    assert await _status(package, {}) == 401
    assert await _status(package, {"X-Nguoi-Hoi": "   "}) == 401
    assert await _status(package, {"X-Asker": "an@cty.vn"}) == 401, "only the configured header counts"


# --------------------------------------------------------------------------- #
# jwt
# --------------------------------------------------------------------------- #


@pytest.fixture
def jwt_package(package_dir: Callable[..., Path], secrets_env: None, monkeypatch: pytest.MonkeyPatch) -> Package:
    monkeypatch.setenv(f"{PREFIX}JWT_KEY", JWT_KEY)
    return _loaded(
        package_dir(
            auth={
                "mode": "jwt",
                "claim": "email",
                "jwt": {"key_secret": "jwt_key", "algorithms": ["HS256"], "issuer": ISSUER, "audience": AUDIENCE},
            }
        )
    )


async def test_a_token_signed_with_the_companys_key_names_the_asker(jwt_package: Package) -> None:
    token = _hs256({"email": "an@cty.vn", "iss": ISSUER, "aud": AUDIENCE})

    assert await _status(jwt_package, _bearer(token)) == 200


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(
            _hs256(
                {"email": "an@cty.vn", "iss": ISSUER, "aud": AUDIENCE}, key="khoa-cua-ke-khac-cung-dai-32-byte-nhe!!"
            ),
            id="wrong-key",
        ),
        pytest.param(
            jwt.encode(
                {"email": "an@cty.vn", "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) - 3600},
                JWT_KEY,
                algorithm="HS256",
            ),
            id="expired",
        ),
        pytest.param(
            jwt.encode({"email": "an@cty.vn", "iss": ISSUER, "aud": AUDIENCE}, JWT_KEY, algorithm="HS256"),
            id="no-expiry",
        ),
        pytest.param(
            _hs256({"email": "an@cty.vn", "iss": "https://ke-gia-mao.test", "aud": AUDIENCE}), id="wrong-issuer"
        ),
        pytest.param(_hs256({"email": "an@cty.vn", "iss": ISSUER, "aud": "agent-khac"}), id="wrong-audience"),
        pytest.param(_hs256({"sub": "123", "iss": ISSUER, "aud": AUDIENCE}), id="no-email-claim"),
        pytest.param(
            jwt.encode(
                {"email": "an@cty.vn", "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300},
                None,
                algorithm="none",
            ),
            id="alg-none",
        ),
        pytest.param("khong-phai-token", id="garbage"),
    ],
)
async def test_a_token_that_does_not_verify_is_a_401(jwt_package: Package, token: str) -> None:
    assert await _status(jwt_package, _bearer(token)) == 401


async def test_no_bearer_token_is_a_401(jwt_package: Package) -> None:
    assert await _status(jwt_package, {}) == 401
    assert await _status(jwt_package, {"Authorization": "Basic YW46eA=="}) == 401


def test_the_jwt_key_is_a_required_secret(package_dir: Callable[..., Path], secrets_env: None) -> None:
    from src.standalone.config import MissingSecretError

    with pytest.raises(MissingSecretError) as caught:
        _loaded(package_dir(auth={"mode": "jwt", "jwt": {"key_secret": "jwt_key"}}))

    assert "jwt_key" in caught.value.missing


# --------------------------------------------------------------------------- #
# oidc — a mock issuer
# --------------------------------------------------------------------------- #


class MockIssuer:
    """An OpenID Connect issuer served through `httpx.MockTransport`, whose keys can be rotated."""

    def __init__(self) -> None:
        self.private: dict[str, Any] = {}
        self.published: list[dict[str, Any]] = []
        self.add_key("k1")

    def add_key(self, kid: str) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private[kid] = key
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        self.published.append({**public, "kid": kid, "use": "sig", "alg": "RS256"})

    def token(self, claims: dict[str, Any], *, kid: str = "k1", issuer: str = ISSUER, audience: str = AUDIENCE) -> str:
        body = {"iss": issuer, "aud": audience, "exp": int(time.time()) + 300, **claims}
        return jwt.encode(body, self.private[kid], algorithm="RS256", headers={"kid": kid})

    def transport(self, *, says_issuer: str = ISSUER) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/.well-known/openid-configuration":
                return httpx.Response(200, json={"issuer": says_issuer, "jwks_uri": f"{ISSUER}/jwks"})
            if request.url.path == "/jwks":
                return httpx.Response(200, json={"keys": self.published})
            return httpx.Response(404)

        return httpx.MockTransport(handle)


@pytest.fixture
def issuer() -> MockIssuer:
    return MockIssuer()


@pytest.fixture
def oidc_package(package_dir: Callable[..., Path], secrets_env: None) -> Package:
    return _loaded(
        package_dir(auth={"mode": "oidc", "claim": "email", "oidc": {"issuer": ISSUER, "audience": AUDIENCE}})
    )


async def test_an_oidc_token_from_the_issuer_names_the_asker(oidc_package: Package, issuer: MockIssuer) -> None:
    assert (
        await _status(oidc_package, _bearer(issuer.token({"email": "an@cty.vn"})), transport=issuer.transport()) == 200
    )


async def test_an_oidc_token_signed_by_anyone_else_is_a_401(oidc_package: Package, issuer: MockIssuer) -> None:
    impostor = MockIssuer()  # its own key, published nowhere the package looks — and the same kid

    assert (
        await _status(oidc_package, _bearer(impostor.token({"email": "an@cty.vn"})), transport=issuer.transport())
        == 401
    )


@pytest.mark.parametrize(("issuer_claim", "audience"), [("https://idp-khac.test", AUDIENCE), (ISSUER, "agent-khac")])
async def test_an_oidc_token_for_another_issuer_or_audience_is_a_401(
    oidc_package: Package, issuer: MockIssuer, issuer_claim: str, audience: str
) -> None:
    token = issuer.token({"email": "an@cty.vn"}, issuer=issuer_claim, audience=audience)

    assert await _status(oidc_package, _bearer(token), transport=issuer.transport()) == 401


async def test_a_key_the_issuer_rotated_in_after_start_up_is_followed(
    oidc_package: Package, issuer: MockIssuer
) -> None:
    transport = issuer.transport()
    async with await _client(oidc_package, transport=transport) as client:
        issuer.add_key("k2")  # published after the package fetched its keys
        response = await client.get("/approvals", headers=_bearer(issuer.token({"email": "an@cty.vn"}, kid="k2")))

    assert response.status_code == 200


def test_an_issuer_whose_discovery_names_another_issuer_refuses_to_start(
    oidc_package: Package, issuer: MockIssuer
) -> None:
    with pytest.raises(RuntimeConfigError) as caught:
        create_app(oidc_package, auth_transport=issuer.transport(says_issuer="https://ke-gia-mao.test"))

    assert "không khớp" in str(caught.value)


# --------------------------------------------------------------------------- #
# custom — the company's own adapter
# --------------------------------------------------------------------------- #


@pytest.fixture
def adapter(package_dir: Callable[..., Path], secrets_env: None) -> Iterator[Callable[[str], Package]]:
    def _with(source: str) -> Package:
        directory = package_dir(auth={"mode": "custom", "custom": {"module": "auth_adapter.py"}})
        (directory / "auth_adapter.py").write_text(source, encoding="utf-8")
        return _loaded(directory)

    yield _with


async def test_a_custom_adapter_names_the_asker_and_none_is_a_401(adapter: Callable[[str], Package]) -> None:
    package = adapter("def resolve_principal(request):\n    return request.headers.get('X-Cty-User')\n")

    assert await _status(package, {"X-Cty-User": "an@cty.vn"}) == 200
    assert await _status(package, {}) == 401


async def test_an_async_custom_adapter_is_awaited(adapter: Callable[[str], Package]) -> None:
    package = adapter("async def resolve_principal(request):\n    return request.headers.get('X-Cty-User')\n")

    assert await _status(package, {"X-Cty-User": "an@cty.vn"}) == 200


async def test_a_custom_adapter_that_raises_or_answers_nonsense_lets_nobody_in(
    adapter: Callable[[str], Package],
) -> None:
    raising = adapter("def resolve_principal(request):\n    raise RuntimeError('LDAP down')\n")
    nonsense = adapter("def resolve_principal(request):\n    return {'email': 'an@cty.vn'}\n")

    assert await _status(raising, {"X-Cty-User": "an@cty.vn"}) == 401
    assert await _status(nonsense, {"X-Cty-User": "an@cty.vn"}) == 401


def test_a_custom_adapter_without_its_function_refuses_to_start(adapter: Callable[[str], Package]) -> None:
    package = adapter("def who(request):\n    return 'an@cty.vn'\n")

    with pytest.raises(RuntimeConfigError) as caught:
        create_app(package)

    assert "resolve_principal" in str(caught.value)


# --------------------------------------------------------------------------- #
# A valid identity the employee view does not hold
# --------------------------------------------------------------------------- #


async def test_a_valid_token_for_someone_not_in_the_employee_view_is_told_no_permission(
    jwt_package: Package,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    token = _hs256({"email": "nguoi-ngoai@cty.vn", "iss": ISSUER, "aud": AUDIENCE})
    async with await _client(jwt_package) as client:
        scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="Bạn không có quyền xem dữ liệu này.")]])
        response = await client.post("/chat", json={"message": "cho xem hết công nợ"}, headers=_bearer(token))

    assert response.status_code == 200, response.text
    assert "không có quyền" in _rows_read(response).lower()
    assert not parse_sse(response.text, event="error")


async def test_a_valid_token_reads_exactly_what_its_owners_position_allows(
    jwt_package: Package,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    token = _hs256({"email": "an@cty.vn", "iss": ISSUER, "aud": AUDIENCE})
    async with await _client(jwt_package) as client:
        scripted_chat_model_factory([[_writes(OVERDUE)], [AIMessage(content="C1")]])
        response = await client.post("/chat", json={"message": "khách nào quá hạn?"}, headers=_bearer(token))

    assert "C1" in _rows_read(response) and "C3" not in _rows_read(response)
