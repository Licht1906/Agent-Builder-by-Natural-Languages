"""BYODB: a creator's own pointer at a vector database this platform does not run — RAG-20 / #106.

Same split `src.workflow.knowledge_bases` draws: this module owns the row —
creating one, listing an owner's own, deleting one, running `/test` against
it — and nothing about what a `KnowledgeSearch` node does with it at run
time, which is a later ticket's job. Like that module, it has no HTTP surface
of its own; `src.api.external_connection_routes` is what turns these rows
into responses or 404s.

`run_connection_test` is the one function here that leaves the process,
and even it does not — it delegates every byte of the actual dial-out to
`src.services.connection_probe.probe_connection`, keeping the SSRF-guarded
transport in one module and the bookkeeping (resolving `secret_ref`,
writing the verdict back onto the row) in this one.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import (
    ExternalConnection,
    ExternalConnectionProvider,
    ExternalConnectionStatus,
    KnowledgeBase,
    VectorDistanceMetric,
)
from src.services.connection_probe import (
    HttpGet,
    ProbeFailureReason,
    ProbeOutcome,
    http_get_via_httpx,
    probe_connection,
)
from src.services.secret_store import SecretNotFoundError, SecretStore, SecretUnreadableError


def create_external_connection(
    db: Session,
    owner_id: int,
    *,
    name: str,
    provider: ExternalConnectionProvider,
    connection_url: str,
    secret_ref: str,
    collection_name: str,
    embedding_dimension: int,
    distance_metric: VectorDistanceMetric,
    field_mapping: dict[str, str],
) -> ExternalConnection:
    """A fresh, `untested` BYODB pointer belonging to `owner_id`.

    Never dials out — `connection_url` is stored as given, whatever it names.
    Only `POST .../test` (`run_connection_test`) ever reaches for it,
    and always through `assert_safe_url` first — a row existing is not a
    network call having happened.
    """
    connection = ExternalConnection(
        owner_id=owner_id,
        name=name,
        provider=provider,
        connection_url=connection_url,
        secret_ref=secret_ref,
        collection_name=collection_name,
        embedding_dimension=embedding_dimension,
        distance_metric=distance_metric,
        field_mapping=dict(field_mapping),
    )
    db.add(connection)
    return _persist(db, connection)


def list_external_connections(db: Session, owner_id: int) -> Sequence[ExternalConnection]:
    """Every BYODB connection `owner_id` owns, newest first."""
    return (
        db.execute(
            select(ExternalConnection)
            .where(ExternalConnection.owner_id == owner_id)
            .order_by(ExternalConnection.id.desc())
        )
        .scalars()
        .all()
    )


class ExternalConnectionInUseError(ValueError):
    """≥1 Knowledge Base still names this connection — the ticket's own "chặn nếu đang được ≥1 KB dùng".

    Unlike `KnowledgeBaseLinkedError`, there is no `confirm=True` past this:
    the ticket's checklist says outright *chặn*, not *warn*. Carries the
    Knowledge Bases by name for the same reason
    `KnowledgeBaseDeleteRefusedError` does — a caller building a confirmation
    screen needs who, not a sentence to parse it back out of.
    """

    def __init__(self, message: str, *, knowledge_bases: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.knowledge_bases = list(knowledge_bases)


def delete_external_connection(db: Session, connection: ExternalConnection) -> None:
    """Remove `connection` — refused outright while any Knowledge Base still points at it."""
    using = (
        db.execute(select(KnowledgeBase.name).where(KnowledgeBase.external_connection_id == connection.id))
        .scalars()
        .all()
    )
    if using:
        raise ExternalConnectionInUseError(
            f"Không thể xoá — đang được dùng bởi Knowledge Base: {', '.join(using)}",
            knowledge_bases=using,
        )
    db.delete(connection)
    db.commit()


def run_connection_test(
    db: Session,
    connection: ExternalConnection,
    *,
    secret_store: SecretStore,
    http_get: HttpGet = http_get_via_httpx,
) -> ExternalConnection:
    """Run `POST .../test` for real: resolve the credential, probe, and persist the verdict.

    `secret_ref` empty means "no credential" and `probe_connection` gets
    `None` — a self-hosted instance with no API key configured is a real,
    supported shape, not a misconfiguration. A `secret_ref` that is set but
    unresolvable (nobody `put` it yet, or it no longer decrypts) is reported
    the same way a bad connection string is — `failed`, with the reason
    named, never a crash — because from a creator's chair both are "this
    connection cannot run yet" and neither should ever reach past this
    function as an exception.
    """
    if connection.secret_ref:
        try:
            secret = secret_store.get(connection.secret_ref)
        except (SecretNotFoundError, SecretUnreadableError) as exc:
            outcome = ProbeOutcome(
                status=ExternalConnectionStatus.FAILED,
                detail=_secret_error_detail(connection, exc),
                reason=(
                    ProbeFailureReason.CREDENTIAL_MISSING
                    if isinstance(exc, SecretNotFoundError)
                    else ProbeFailureReason.CREDENTIAL_UNREADABLE
                ),
            )
            return _apply(db, connection, outcome)
    else:
        secret = None

    outcome = probe_connection(connection, secret=secret, http_get=http_get)
    return _apply(db, connection, outcome)


def _secret_error_detail(connection: ExternalConnection, exc: SecretNotFoundError | SecretUnreadableError) -> str:
    """One sentence naming which of the two `secret_store.get` failures this was — ADR-0045's own distinction.

    Not found means nobody `put` this key yet; unreadable means the row
    exists but no longer decrypts under today's `SECRET_STORE_KEY`. Different
    causes, so different sentences — collapsing them would tell a creator to
    fix a reference that was never wrong (RAG-19's own reasoning).
    """
    if isinstance(exc, SecretNotFoundError):
        return f"secret_ref {connection.secret_ref!r} không có trong secret store — cần admin PUT giá trị trước"
    return f"secret_ref {connection.secret_ref!r} không giải mã được — SECRET_STORE_KEY có thể đã đổi"


def _apply(db: Session, connection: ExternalConnection, outcome: ProbeOutcome) -> ExternalConnection:
    connection.status = outcome.status
    connection.status_detail = outcome.detail
    connection.status_reason = outcome.reason
    connection.last_checked_at = datetime.now(UTC).replace(tzinfo=None)
    return _persist(db, connection)


def _persist(db: Session, connection: ExternalConnection) -> ExternalConnection:
    db.commit()
    db.refresh(connection)
    return connection
