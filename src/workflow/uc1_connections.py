"""UC1-08 (#122) — the connections registry: credential → `SecretStore`, dry-run probe (FR-20..24).

`src.api.connection_routes` is the only door onto this module, and it is a
door the meta-agent's prompt never touches — FR-21's "không đi qua context
window của LLM" is a fact about which routers exist, not a filter this module
runs. `create_connection` never hands the credential back out once it has
written it: the row it returns carries only `credential_ref`
(`vault://conn/<name>`), the same pointer contract
`src.agentspec.spec.Connection.credential_ref` requires (§7).

The credential lives in the platform's one `SecretStore` (DS-20), under the
key `conn/<name>` (`UC1Connection.vault_path`). The `vault://` prefix of the
pointer is the spec's format, kept as written; no Vault is dialled.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agentspec.spec import AgentSpec, ConnectionType
from src.db.models import UC1Connection, UC1ConnectionProbeStatus
from src.services.secret_store import SecretNotFoundError, SecretStore, SecretUnreadableError
from src.services.uc1_connection_probe import SUPPORTED_PROBE_TYPES, ProbeResult, ProbeStatus, probe_postgres_connection

_VAULT_PATH_PREFIX = "conn"


class UnsupportedProbeProviderError(ValueError):
    """`connection.type` has no dry-run prober — only `postgres` does in this build (§16.2)."""


class ConnectionAlreadyExistsError(ValueError):
    """`name` already names a connection — `POST /connections` refuses rather than silently overwriting its secret."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Đã có kết nối tên {name!r}")


def _vault_path(name: str) -> str:
    return f"{_VAULT_PATH_PREFIX}/{name}"


def credential_ref_of(name: str) -> str:
    """`vault://conn/<name>` — the exact pointer a spec's `connections[].credential_ref` must equal."""
    return f"vault://{_vault_path(name)}"


def create_connection(
    db: Session,
    secrets: SecretStore,
    *,
    name: str,
    type_: ConnectionType,
    credential: str,
    allowed_objects: Sequence[str],
    allowed_hosts: Sequence[str],
    read_only: bool,
    created_by_id: int,
) -> UC1Connection:
    """Write `credential` to the secret store, then a row naming only the pointer — never the value.

    Refuses a duplicate `name` before writing any secret: two connections
    must never share one key, since a second `put` there
    would silently replace the first connection's credential with the
    second's (FR-20).
    """
    if db.execute(select(UC1Connection).where(UC1Connection.name == name)).scalar_one_or_none() is not None:
        raise ConnectionAlreadyExistsError(name)

    secrets.put(_vault_path(name), credential)
    row = UC1Connection(
        name=name,
        type=type_,
        allowed_objects=list(allowed_objects),
        allowed_hosts=list(allowed_hosts),
        read_only=read_only,
        vault_path=_vault_path(name),
        credential_ref=credential_ref_of(name),
        created_by_id=created_by_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_connections(db: Session) -> Sequence[UC1Connection]:
    """Every connection, newest first — `GET /connections` never returns a credential (FR-22)."""
    return db.execute(select(UC1Connection).order_by(UC1Connection.id.desc())).scalars().all()


def run_probe(db: Session, secrets: SecretStore, connection: UC1Connection) -> UC1Connection:
    """`POST /connections/{id}/probe` for real (FR-23): resolve the credential, dial out, persist the verdict.

    Raises `UnsupportedProbeProviderError` for a connection type this build
    cannot dry-run yet — a 4xx at the route, not a crash. Every failure past
    that point (credential unreadable or never written, the target
    unreachable, a base table where a view should be) is written onto the
    row as a `failed` verdict instead — FR-23's "lỗi rõ ràng, không treo".
    """
    if connection.type not in SUPPORTED_PROBE_TYPES:
        raise UnsupportedProbeProviderError(
            f"Loại kết nối {connection.type.value!r} chưa có driver kiểm tra tự động — bản này chỉ hỗ trợ 'postgres'"
        )

    try:
        dsn = secrets.get(connection.vault_path)
    except (SecretNotFoundError, SecretUnreadableError) as exc:
        return _apply(db, connection, ProbeResult(status=ProbeStatus.FAILED, detail=f"Không đọc được credential: {exc}"))

    result = probe_postgres_connection(dsn=dsn, allowed_objects=connection.allowed_objects)
    return _apply(db, connection, result)


def verified_objects_for_spec(db: Session, spec: AgentSpec) -> dict[str, list[str]]:
    """FR-24 at compile time — `src.compiler.graph.compile_spec`'s
    `CompileDependencies.verified_connection_objects`: for each of `spec.connections`,
    the view names its most recent *successful* probe actually confirmed.

    Matched by the name embedded in `credential_ref` (`vault://conn/<name>`,
    `credential_ref_of`'s own format), not `UC1Connection.id` — a spec's
    `connections[].id` (e.g. `conn_erp`, `agentspec.yaml`'s own convention) has
    no reason to equal this table's autoincrement PK. A connection nobody has
    probed successfully yet (no row, or `last_probe_status` is not
    `CONNECTED`) is simply absent from the result — `compile_spec` then treats
    its `allowed_objects` as unverified, the same as a bare base table.
    """
    result: dict[str, list[str]] = {}
    for connection in spec.connections:
        name = _connection_name_in(connection.credential_ref)
        if name is None:
            continue
        row = db.execute(select(UC1Connection).where(UC1Connection.name == name)).scalar_one_or_none()
        if row is not None and row.last_probe_status is UC1ConnectionProbeStatus.CONNECTED:
            result[connection.id] = [entry["object"] for entry in row.last_probe_objects]
    return result


def _connection_name_in(credential_ref: str) -> str | None:
    prefix = f"vault://{_VAULT_PATH_PREFIX}/"
    return credential_ref.removeprefix(prefix) if credential_ref.startswith(prefix) else None


def _apply(db: Session, connection: UC1Connection, result: ProbeResult) -> UC1Connection:
    connection.last_probe_status = (
        UC1ConnectionProbeStatus.CONNECTED if result.status is ProbeStatus.CONNECTED else UC1ConnectionProbeStatus.FAILED
    )
    connection.last_probe_detail = result.detail
    connection.last_probe_objects = [{"object": count.object, "row_count": count.row_count} for count in result.objects]
    connection.last_probed_at = datetime.now(UTC).replace(tzinfo=None)
    db.commit()
    db.refresh(connection)
    return connection
