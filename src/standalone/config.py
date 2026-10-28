"""`runtime.yaml` and the secrets it names — DS-13.

The one file a company's IT edits (with `access.yaml`). Read before anything
touches a database, because it is what says which database: the package's own
Postgres URL is itself a secret.

**Every secret it names is required, and all of the missing ones are named at
once.** A package that starts and then fails on its first chat because the
LLM key was never set is a package an operator debugs one restart at a time;
refusing at start-up with every missing name — and where each was looked for —
is one fix.

Three backends, one interface (`get`): `env` reads `<prefix><NAME>`, `file`
reads `<directory>/<name>` (Docker/Kubernetes secrets), `vault` reads KV v2
through the platform's own `VaultKvStore`.
"""

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.services.vault_secret_store import VaultKvStore, VaultSecretNotFoundError, VaultUnavailableError


class SecretBackend(StrEnum):
    ENV = "env"
    FILE = "file"
    VAULT = "vault"


class VaultSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    mount: str = "secret"
    #: The Vault token is the one secret that cannot live in Vault — read from this env variable.
    token_env: str = "VAULT_TOKEN"


class SecretsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: SecretBackend = SecretBackend.ENV
    env_prefix: str = "AGENT_SECRET_"
    directory: str = "/run/secrets"
    vault: VaultSettings | None = None


class DatabaseConfig(BaseModel):
    """The package's own Postgres — checkpoints, approvals, runs, audit."""

    model_config = ConfigDict(extra="forbid")

    url_secret: str = Field(..., min_length=1)


class DataSourceConfig(BaseModel):
    """One Hệ CSDL the bundle's `data_query` nodes read, as this company reaches it."""

    model_config = ConfigDict(extra="forbid")

    #: The `source_id` the bundle's `data_query` nodes name — the platform's id when it was exported.
    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    #: `src.db.models.DatabaseDialect`'s values — spelled here so this module never imports the ORM (and its engine).
    dialect: Literal["postgresql", "sqlserver", "mysql"]
    dsn_secret: str = Field(..., min_length=1)
    access: str = Field(default="access.yaml", description="Đường dẫn access.yaml của Hệ CSDL này")


class LlmConfig(BaseModel):
    """An OpenAI-compatible endpoint. Data stays in the company only when this points inside it."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(..., min_length=1)
    api_key_secret: str | None = None
    default_model: str = Field(..., min_length=1)
    #: The model the bundle names for a node (the one it was evaluated with) → the model served here.
    models: dict[str, str] = Field(default_factory=dict)


class EmbeddingConfig(BaseModel):
    """An OpenAI-compatible `/embeddings` endpoint — what the package's documents are indexed with (DS-18).

    Like `llm`, data stays in the company only when this points inside it: every passage is sent here.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(..., min_length=1)
    api_key_secret: str | None = None
    model: str = Field(..., min_length=1)
    #: Passages per request.
    batch_size: int = Field(default=32, ge=1, le=512)
    timeout_seconds: float = Field(default=60, gt=0, le=600)


class QdrantConfig(BaseModel):
    """The package's own Qdrant, which holds nothing but this package's documents."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1)
    collection: str = Field(default="agent_documents", min_length=1)


class RerankerConfig(BaseModel):
    """The rerank service (the platform's `reranker` image). Off unless switched on: search then keeps the fused order."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str = "http://reranker:8001"
    timeout_seconds: float = Field(default=30, gt=0, le=300)


class JwtSettings(BaseModel):
    """A bearer token the company's own identity system signs (DS-14)."""

    model_config = ConfigDict(extra="forbid")

    #: The secret holding the verifying key: the shared key for HS*, the PEM public key for RS*/ES*.
    key_secret: str = Field(..., min_length=1)
    algorithms: list[str] = Field(default_factory=lambda: ["HS256"], min_length=1)
    issuer: str | None = None
    audience: str | None = None
    leeway_seconds: int = Field(default=30, ge=0, le=600)


class OidcSettings(BaseModel):
    """An OpenID Connect issuer: its signing keys found through discovery (or `jwks_url`), cached."""

    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(..., min_length=1)
    audience: str = Field(..., min_length=1)
    #: The public client the package's chat page signs people in as (Authorization Code + PKCE, DS-17).
    #: The page sends the ID token, so `audience` is this id. Not a secret — the page has no secret.
    client_id: str | None = None
    scope: str = "openid email profile"
    jwks_url: str | None = None
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"], min_length=1)
    leeway_seconds: int = Field(default=30, ge=0, le=600)


class CustomAuthSettings(BaseModel):
    """A file the company writes, with exactly one function: `resolve_principal(request) -> str | None`."""

    model_config = ConfigDict(extra="forbid")

    module: str = "auth_adapter.py"


class AuthConfig(BaseModel):
    """How an asker is identified (DS-14). Whatever the mode, it yields one value — the `claim` — or a 401.

    That value is what `access.yaml`'s `identity.match_by` is matched against
    (DS-03), so a token that is valid but names somebody the employee view
    does not hold is answered "không có quyền", never a 500 and never a
    default asker.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["trusted_header", "oidc", "jwt", "custom"] = "trusted_header"
    #: `trusted_header`: the header a gateway that already signed the person in passes their identity in.
    header: str = "X-Asker"
    #: `jwt`/`oidc`: which claim of the token is the asker.
    claim: str = "email"
    jwt: JwtSettings | None = None
    oidc: OidcSettings | None = None
    custom: CustomAuthSettings | None = None


class SmtpSettings(BaseModel):
    """The company's SMTP relay. STARTTLS unless switched off — and a password never goes over a plain line."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    host: str = Field(..., min_length=1)
    port: int = Field(default=587, ge=1, le=65535)
    #: The address every mail is sent from — the package's, not the model's to choose.
    sender: str = Field(..., alias="from", min_length=3)
    user: str | None = None
    password_secret: str | None = None
    starttls: bool = True
    timeout_seconds: int = Field(default=10, ge=1, le=120)

    @model_validator(mode="after")
    def _no_password_in_clear(self) -> "SmtpSettings":
        if self.password_secret and not self.starttls:
            raise ValueError("mail.smtp: có password_secret thì phải bật starttls — mật khẩu không đi dạng rõ")
        return self


class GmailOauthSettings(BaseModel):
    """A Gmail account the company owns, reached with a refresh token — the platform creator's account never is."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(..., min_length=1)
    client_secret_secret: str = Field(..., min_length=1)
    refresh_token_secret: str = Field(..., min_length=1)
    token_url: str = "https://oauth2.googleapis.com/token"


class CustomMailSettings(BaseModel):
    """A file the company writes, with exactly one function: `send_email(to, subject, body) -> str`."""

    model_config = ConfigDict(extra="forbid")

    module: str = "mail_adapter.py"


class MailConfig(BaseModel):
    """Where the bundle's `gmail.send_email` goes in this company (DS-16). Changing it never touches the bundle."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["smtp", "gmail_oauth", "custom"]
    smtp: SmtpSettings | None = None
    gmail_oauth: GmailOauthSettings | None = None
    custom: CustomMailSettings | None = None


class RuntimeConfig(BaseModel):
    """`runtime.yaml`, as written by the company. Relative paths are relative to the file."""

    model_config = ConfigDict(extra="forbid")

    bundle: str = "bundle.json"
    database: DatabaseConfig
    data_sources: list[DataSourceConfig] = Field(default_factory=list)
    llm: LlmConfig
    embedding: EmbeddingConfig | None = None
    #: Where the bundle's documents are: `<documents>/<knowledge base id>/<file>` (DS-18).
    documents: str = "documents"
    qdrant: QdrantConfig | None = None
    reranker: RerankerConfig = Field(default_factory=RerankerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    #: Required once the bundle can send mail; absent, a send is refused by name.
    mail: MailConfig | None = None
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    #: Database hosts on a private network the package may dial (`database_source_trusted_hosts`).
    trusted_hosts: list[str] = Field(default_factory=list)


class RuntimeConfigError(ValueError):
    """The package cannot start. The message names what to fix."""


class MissingSecretError(RuntimeConfigError):
    """Secrets `runtime.yaml` requires that the backend does not have — every one of them, named."""

    def __init__(self, missing: Mapping[str, str]) -> None:
        self.missing = dict(missing)
        lines = "; ".join(f"{name} ({where})" for name, where in missing.items())
        super().__init__(f"Thiếu secret bắt buộc nên gói không khởi động: {lines}")


class SecretSource(Protocol):
    def get(self, name: str) -> str | None:
        """The secret called `name`, or `None` when the backend has none by that name."""
        ...

    def where(self, name: str) -> str:
        """Where `name` was looked for, said for an operator."""
        ...


class EnvSecrets:
    def __init__(self, prefix: str, environ: Mapping[str, str] | None = None) -> None:
        self.prefix = prefix
        self.environ = os.environ if environ is None else environ

    def _variable(self, name: str) -> str:
        return f"{self.prefix}{name.upper().replace('-', '_').replace('.', '_')}"

    def get(self, name: str) -> str | None:
        value = self.environ.get(self._variable(name))
        return value if value else None

    def where(self, name: str) -> str:
        return f"biến môi trường {self._variable(name)}"


class FileSecrets:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def get(self, name: str) -> str | None:
        path = self.directory / name
        if not path.is_file():
            return None
        value = path.read_text(encoding="utf-8").strip()
        return value or None

    def where(self, name: str) -> str:
        return f"file {self.directory / name}"


class VaultSecrets:
    def __init__(self, store: VaultKvStore) -> None:
        self.store = store

    def get(self, name: str) -> str | None:
        try:
            return self.store.get(name)
        except VaultSecretNotFoundError:
            return None

    def where(self, name: str) -> str:
        return f"Vault {self.store.mount}/{name}"


def load_runtime_config(path: Path) -> RuntimeConfig:
    """`runtime.yaml` read and checked — `RuntimeConfigError` naming the problem otherwise."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeConfigError(f"Không đọc được {path}: {exc}") from None
    try:
        return RuntimeConfig.model_validate(document)
    except ValueError as exc:
        raise RuntimeConfigError(f"{path} sai định dạng: {exc}") from None


def secret_source(config: SecretsConfig, *, base: Path, environ: Mapping[str, str] | None = None) -> SecretSource:
    """The backend `config` names."""
    if config.backend is SecretBackend.ENV:
        return EnvSecrets(config.env_prefix, environ)
    if config.backend is SecretBackend.FILE:
        directory = Path(config.directory)
        return FileSecrets(directory if directory.is_absolute() else base / directory)
    if config.vault is None:
        raise RuntimeConfigError("secrets.backend là 'vault' nhưng thiếu secrets.vault (url, mount, token_env)")
    env = os.environ if environ is None else environ
    token = env.get(config.vault.token_env, "")
    if not token:
        raise MissingSecretError({config.vault.token_env: f"biến môi trường {config.vault.token_env} (token Vault)"})
    return VaultSecrets(VaultKvStore(base_url=config.vault.url.rstrip("/"), token=token, mount=config.vault.mount))


def required_secrets(config: RuntimeConfig) -> list[str]:
    """Every secret `config` names, in the order an operator reads the file."""
    names = [config.database.url_secret, *(source.dsn_secret for source in config.data_sources)]
    if config.llm.api_key_secret:
        names.append(config.llm.api_key_secret)
    if config.embedding is not None and config.embedding.api_key_secret:
        names.append(config.embedding.api_key_secret)
    if config.auth.mode == "jwt" and config.auth.jwt is not None:
        names.append(config.auth.jwt.key_secret)
    mail = config.mail
    if mail is not None and mail.mode == "smtp" and mail.smtp is not None and mail.smtp.password_secret:
        names.append(mail.smtp.password_secret)
    if mail is not None and mail.mode == "gmail_oauth" and mail.gmail_oauth is not None:
        names.extend([mail.gmail_oauth.client_secret_secret, mail.gmail_oauth.refresh_token_secret])
    return list(dict.fromkeys(names))


def resolve_secrets(config: RuntimeConfig, source: SecretSource) -> dict[str, str]:
    """Every required secret's value, or `MissingSecretError` naming all that are missing."""
    found: dict[str, str] = {}
    missing: dict[str, str] = {}
    for name in required_secrets(config):
        try:
            value = source.get(name)
        except VaultUnavailableError as exc:
            raise RuntimeConfigError(f"Không đọc được Vault khi lấy secret {name}: {exc}") from None
        if value is None:
            missing[name] = source.where(name)
        else:
            found[name] = value
    if missing:
        raise MissingSecretError(missing)
    return found


def as_document(config: RuntimeConfig) -> dict[str, Any]:
    """`config` as YAML would hold it — for the template an export writes."""
    return config.model_dump(mode="json", exclude_none=True, by_alias=True)
