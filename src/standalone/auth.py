"""Who is asking, in the exported package — DS-14 / ADR-0059 §10.

Four modes, one contract: an `Authenticator` takes the request and returns
**one value or nothing**. The value is the asker — what `access.yaml`'s
`identity.match_by` is matched against (DS-03) — and nothing is a 401. There
is no third outcome: no default asker, no "anonymous", no first employee in
the view. A valid token that names somebody the employee view does not hold
is therefore a 200 whose every read is denied ("không có quyền"), which is
the one place that decision belongs.

- `trusted_header` — a gateway in front of the package signed the person in
  and passes who they are in one header.
- `jwt` — a bearer token the company's identity system signs; the verifying
  key (shared secret for HS*, PEM public key for RS*/ES*) is a package secret.
  Signature, expiry and — when configured — issuer and audience are checked.
- `oidc` — a bearer token from an OpenID Connect issuer: its signing keys are
  found through discovery (or `jwks_url`) at start-up and cached; a token
  signed with a key not yet seen refetches them once, which is how a key
  rotation is followed without a restart.
- `custom` — a file the company writes with exactly one function,
  `resolve_principal(request) -> str | None` (sync or async). Whatever it
  raises or returns that is not a non-blank string is a 401.
"""

import importlib.util
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi import Request

from src.standalone.config import AuthConfig, RuntimeConfigError

_log = logging.getLogger(__name__)

#: The one shape every mode is reduced to.
Authenticator = Callable[[Request], Awaitable[str | None]]

#: The function a `custom` adapter must define, and nothing else is looked for.
CUSTOM_FUNCTION = "resolve_principal"


def build_authenticator(
    config: AuthConfig,
    *,
    secrets: Mapping[str, str],
    base: Path,
    transport: httpx.BaseTransport | None = None,
) -> Authenticator:
    """The `Authenticator` `config` describes — `RuntimeConfigError` naming what is missing otherwise.

    `transport` is the OIDC test seam: an issuer served by `httpx.MockTransport`.
    """
    if config.mode == "trusted_header":
        return _trusted_header(config.header)
    if config.mode == "jwt":
        if config.jwt is None:
            raise RuntimeConfigError(
                "auth.mode là 'jwt' nhưng thiếu auth.jwt (key_secret, algorithms, issuer, audience)"
            )
        settings = config.jwt
        key = secrets[settings.key_secret]

        def verify(token: str) -> dict[str, Any]:
            return jwt.decode(
                token,
                key,
                algorithms=settings.algorithms,
                issuer=settings.issuer,
                audience=settings.audience,
                leeway=settings.leeway_seconds,
                options={"require": ["exp"], "verify_aud": settings.audience is not None},
            )

        return _bearer(verify, config.claim)
    if config.mode == "oidc":
        if config.oidc is None:
            raise RuntimeConfigError("auth.mode là 'oidc' nhưng thiếu auth.oidc (issuer, audience)")
        return _bearer(_OidcVerifier(config.oidc.model_dump(), transport=transport).verify, config.claim)
    if config.custom is None:
        raise RuntimeConfigError("auth.mode là 'custom' nhưng thiếu auth.custom (module)")
    return _custom(_resolve_principal_of(_path(config.custom.module, base)))


def _trusted_header(header: str) -> Authenticator:
    async def authenticate(request: Request) -> str | None:
        return _asker_of(request.headers.get(header))

    return authenticate


def _bearer(verify: Callable[[str], dict[str, Any]], claim: str) -> Authenticator:
    """A bearer token, verified by `verify`, reduced to its `claim` — `None` for every way that fails."""

    async def authenticate(request: Request) -> str | None:
        scheme, _, token = (request.headers.get("Authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        try:
            payload = verify(token.strip())
        except jwt.PyJWTError as exc:
            _log.info("Token bị từ chối: %s", type(exc).__name__)
            return None
        return _asker_of(payload.get(claim))

    return authenticate


class _OidcVerifier:
    """An OpenID Connect issuer's keys, discovered once and refetched once on a key id not yet seen."""

    def __init__(self, settings: Mapping[str, Any], *, transport: httpx.BaseTransport | None) -> None:
        self.issuer = str(settings["issuer"]).rstrip("/")
        self.audience = str(settings["audience"])
        self.algorithms = list(settings["algorithms"])
        self.leeway = int(settings["leeway_seconds"])
        self.client = httpx.Client(transport=transport, timeout=5.0)
        self.jwks_url = settings.get("jwks_url") or self._discovered_jwks_url()
        self.keys = self._fetch_keys()

    def _discovered_jwks_url(self) -> str:
        url = f"{self.issuer}/.well-known/openid-configuration"
        try:
            document = self.client.get(url).raise_for_status().json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeConfigError(f"Không đọc được cấu hình OIDC của issuer {self.issuer}: {exc}") from None
        if str(document.get("issuer", "")).rstrip("/") != self.issuer:
            raise RuntimeConfigError(
                f"Issuer OIDC tự khai {document.get('issuer')!r}, không khớp auth.oidc.issuer {self.issuer!r}"
            )
        if not document.get("jwks_uri"):
            raise RuntimeConfigError(f"Cấu hình OIDC của {self.issuer} không có jwks_uri")
        return str(document["jwks_uri"])

    def _fetch_keys(self) -> dict[str, Any]:
        try:
            document = self.client.get(self.jwks_url).raise_for_status().json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeConfigError(f"Không đọc được khoá ký (JWKS) tại {self.jwks_url}: {exc}") from None
        keys: dict[str, Any] = {}
        for entry in document.get("keys") or []:
            try:
                keys[str(entry.get("kid", ""))] = jwt.PyJWK(entry).key
            except jwt.PyJWTError:
                continue  # a key of a type this package does not verify with is simply not usable
        return keys

    def verify(self, token: str) -> dict[str, Any]:
        kid = str(jwt.get_unverified_header(token).get("kid", ""))
        if kid not in self.keys:
            try:
                self.keys = self._fetch_keys()  # a rotation: the issuer has a key we have not seen yet
            except RuntimeConfigError:
                raise jwt.InvalidKeyError("không lấy được khoá ký mới") from None
        key = self.keys.get(kid)
        if key is None:
            raise jwt.InvalidKeyError(f"không có khoá ký {kid!r}")
        return jwt.decode(
            token,
            key,
            algorithms=self.algorithms,
            issuer=self.issuer,
            audience=self.audience,
            leeway=self.leeway,
            options={"require": ["exp", "iss", "aud"]},
        )


def _custom(resolve: Callable[[Request], Any]) -> Authenticator:
    async def authenticate(request: Request) -> str | None:
        try:
            value = resolve(request)
            if inspect.isawaitable(value):
                value = await value
        except Exception:  # noqa: BLE001 — an adapter that breaks lets nobody in, it does not become a 500
            _log.exception("auth_adapter.%s gặp lỗi — request bị từ chối", CUSTOM_FUNCTION)
            return None
        return _asker_of(value) if isinstance(value, str) else None

    return authenticate


def _resolve_principal_of(path: Path) -> Callable[[Request], Any]:
    if not path.is_file():
        raise RuntimeConfigError(f"auth.mode là 'custom' nhưng không có file {path}")
    spec = importlib.util.spec_from_file_location("package_auth_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeConfigError(f"Không nạp được {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — any failure of the company's file is a reason not to start
        raise RuntimeConfigError(f"{path} lỗi khi nạp: {type(exc).__name__}: {exc}") from None
    resolve = getattr(module, CUSTOM_FUNCTION, None)
    if not callable(resolve):
        raise RuntimeConfigError(f"{path} phải có hàm {CUSTOM_FUNCTION}(request) -> str | None")
    return resolve


def _asker_of(value: object) -> str | None:
    """A claim or header as an asker: a non-blank string, or a number written as one — anything else is nobody."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | str):
        text = str(value).strip()
        return text or None
    return None


def _path(value: str, base: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path
