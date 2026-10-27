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
from pydantic import BaseModel, ConfigDict, Field

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
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(..., min_length=1)
    api_key_secret: str | None = None
    model: str = Field(..., min_length=1)


class AuthConfig(BaseModel):
    """How an asker is identified. `trusted_header` here; `oidc`/`jwt`/`custom` are DS-14's."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["trusted_header", "oidc", "jwt", "custom"] = "trusted_header"
    header: str = "X-Asker"


class RuntimeConfig(BaseModel):
    """`runtime.yaml`, as written by the company. Relative paths are relative to the file."""

    model_config = ConfigDict(extra="forbid")

    bundle: str = "bundle.json"
    database: DatabaseConfig
    data_sources: list[DataSourceConfig] = Field(default_factory=list)
    llm: LlmConfig
    embedding: EmbeddingConfig | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
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
    return config.model_dump(mode="json", exclude_none=True)
