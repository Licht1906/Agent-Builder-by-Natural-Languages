"""External Connection endpoints: a creator's own vector databases — RAG-20 / #106.

Same ownership pattern `src.api.knowledge_base_routes` established: every
connection is scoped to its owner, and one that belongs to somebody else is
404, never 403 — a caller learns nothing about who owns what they cannot
reach.

`POST .../test` is the one route that actually leaves this process. It sits
behind three things at once: `OwnedExternalConnection` (may this caller even
see this row), a per-owner rate limit (RAG-20's own "Rate limit /test" —
`src.services.connection_test_rate_limit`), and `src.services.ssrf_guard`
inside the probe itself. None of the three substitutes for the others.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.dependencies import CurrentUser
from src.db.base import get_db
from src.db.models import ExternalConnection
from src.deployments.rate_limit import RateLimitExceededError, SlidingWindowLimiter
from src.models.schemas import ExternalConnectionCreateRequest, ExternalConnectionResponse
from src.services.connection_probe import HttpGet, ProbeFailureReason, get_connection_http_get
from src.services.connection_test_rate_limit import charge_one_connection_test, get_connection_test_rate_limiter
from src.services.secret_store import SecretStore, get_secret_store
from src.workflow.external_connections import (
    ExternalConnectionInUseError,
    create_external_connection,
    delete_external_connection,
    list_external_connections,
    run_connection_test,
)

router = APIRouter(prefix="/external-connections", tags=["external-connections"])


def get_owned_external_connection(
    connection_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> ExternalConnection:
    """The connection at `connection_id` if the caller owns it, otherwise 404 — `get_owned_knowledge_base`'s reason."""
    connection = db.execute(
        select(ExternalConnection).where(ExternalConnection.id == connection_id)
    ).scalar_one_or_none()
    if connection is None or connection.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy kết nối")
    return connection


OwnedExternalConnection = Annotated[ExternalConnection, Depends(get_owned_external_connection)]
ConnectionTestRateLimiterDep = Annotated[SlidingWindowLimiter, Depends(get_connection_test_rate_limiter)]
SecretStoreDep = Annotated[SecretStore, Depends(get_secret_store)]
ConnectionHttpGetDep = Annotated[HttpGet, Depends(get_connection_http_get)]


def _response_of(connection: ExternalConnection) -> ExternalConnectionResponse:
    """`connection` masked for the wire — `has_secret` in place of `secret_ref` itself."""
    return ExternalConnectionResponse(
        id=connection.id,
        name=connection.name,
        provider=connection.provider,
        connection_url=_masked_url(connection.connection_url),
        has_secret=bool(connection.secret_ref),
        collection_name=connection.collection_name,
        embedding_dimension=connection.embedding_dimension,
        distance_metric=connection.distance_metric,
        field_mapping=connection.field_mapping,
        status=connection.status,
        status_detail=connection.status_detail,
        status_reason=_reason_of(connection),
        last_checked_at=connection.last_checked_at,
        created_at=connection.created_at,
    )


def _reason_of(connection: ExternalConnection) -> ProbeFailureReason | None:
    """`status_reason` as the enum it holds, or `None` if it holds nothing this build recognises.

    The column is a plain string (`ExternalConnection.status_reason` says
    why), so a row written by a build that knew a reason this one does not is
    possible — it reads as "no reason given", which is exactly what the
    screen does with an untested connection, rather than a 500 on a `GET`
    that only wanted to list connections.
    """
    if not connection.status_reason:
        return None
    try:
        return ProbeFailureReason(connection.status_reason)
    except ValueError:
        return None


def _masked_url(url: str) -> str:
    """`url` with any embedded userinfo (`user:pass@host`) stripped — the "mask mọi thứ nhạy cảm" line, belt and suspenders.

    `secret_ref`/the secret store is the sanctioned way a connection carries a
    credential; a connection string that also embeds one is not this
    platform's own doing, and this is what keeps such a string from being
    echoed back verbatim on every `GET`.
    """
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    prefix, rest = url[: scheme_sep + 3], url[scheme_sep + 3 :]
    userinfo, _, host_and_path = rest.partition("@")
    if not host_and_path:
        return url
    return f"{prefix}***@{host_and_path}"


@router.get("", response_model=list[ExternalConnectionResponse])
def list_my_external_connections(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> list[ExternalConnectionResponse]:
    """Every BYODB connection the caller owns, newest first."""
    return [_response_of(row) for row in list_external_connections(db, user.id)]


@router.post("", response_model=ExternalConnectionResponse, status_code=status.HTTP_201_CREATED)
def create_my_external_connection(
    request: ExternalConnectionCreateRequest, user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> ExternalConnectionResponse:
    """Register a pointer at an external vector database. Never dials out — `POST .../test` is what does."""
    connection = create_external_connection(
        db,
        user.id,
        name=request.name,
        provider=request.provider,
        connection_url=request.connection_url,
        secret_ref=request.secret_ref,
        collection_name=request.collection_name,
        embedding_dimension=request.embedding_dimension,
        distance_metric=request.distance_metric,
        field_mapping=request.field_mapping,
    )
    return _response_of(connection)


@router.get("/{connection_id}", response_model=ExternalConnectionResponse)
def get_my_external_connection(connection: OwnedExternalConnection) -> ExternalConnectionResponse:
    """One of the caller's own connections, in full — secrets still masked."""
    return _response_of(connection)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_external_connection(connection: OwnedExternalConnection, db: Annotated[Session, Depends(get_db)]) -> None:
    """Remove a connection — refused outright while any Knowledge Base still points at it."""
    try:
        delete_external_connection(db, connection)
    except ExternalConnectionInUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "knowledge_bases": exc.knowledge_bases},
        ) from None


@router.post("/{connection_id}/test", response_model=ExternalConnectionResponse)
def test_my_external_connection(
    connection: OwnedExternalConnection,
    db: Annotated[Session, Depends(get_db)],
    secret_store: SecretStoreDep,
    http_get: ConnectionHttpGetDep,
    rate_limiter: ConnectionTestRateLimiterDep,
) -> ExternalConnectionResponse:
    """Ping the connection for real. Always 200 — `connected`/`failed` with a reason is the answer, never a crash.

    Rate-limited before the probe runs (`charge_one_connection_test`'s own
    reason): the point of the ceiling is to bound how often an outbound dial
    is set in motion, and a caller told off only after the probe already ran
    would have been told off too late to matter.
    """
    try:
        charge_one_connection_test(rate_limiter, connection.owner_id)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.message,
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from None
    updated = run_connection_test(db, connection, secret_store=secret_store, http_get=http_get)
    return _response_of(updated)
