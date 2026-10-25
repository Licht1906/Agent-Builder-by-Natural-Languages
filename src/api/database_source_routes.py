"""Hệ CSDL endpoints — DS-02 / ADR-0059 §2.

`src.api.external_connection_routes`' shape, for a business database instead
of a vector one: every row is its owner's, somebody else's is 404, and
`POST .../test` — the one route that dials out — sits behind ownership, the
per-owner `/test` rate limit and the dialect's own address check at once.

No response carries the DSN. `_response_of` is the only way a row reaches the
wire, and it has nothing to read the DSN from: the row holds a secret-store
key, and `has_dsn` is all that is said about it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.auth.dependencies import CurrentUser
from src.config import get_settings
from src.datasources.dialects import ProbeFailure
from src.datasources.sources import (
    AllowedViewsError,
    DatabaseSourceInUseError,
    create_database_source,
    delete_database_source,
    list_database_sources,
    rename_database_source,
    replace_dsn,
    run_database_test,
    set_allowed_views,
    snapshot_of,
)
from src.db.base import get_db
from src.db.models import DatabaseSource
from src.deployments.rate_limit import RateLimitExceededError, SlidingWindowLimiter
from src.models.schemas import (
    DatabaseSourceCreateRequest,
    DatabaseSourceResponse,
    DatabaseSourceUpdateRequest,
    DatabaseViewResponse,
)
from src.services.connection_test_rate_limit import charge_one_connection_test, get_connection_test_rate_limiter
from src.services.secret_store import SecretStore, get_secret_store

router = APIRouter(prefix="/database-sources", tags=["database-sources"])


def get_owned_database_source(
    source_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> DatabaseSource:
    """The Hệ CSDL at `source_id` if the caller owns it, otherwise 404 — never 403."""
    source = db.get(DatabaseSource, source_id)
    if source is None or source.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy Hệ CSDL")
    return source


OwnedDatabaseSource = Annotated[DatabaseSource, Depends(get_owned_database_source)]
SecretStoreDep = Annotated[SecretStore, Depends(get_secret_store)]
ConnectionTestRateLimiterDep = Annotated[SlidingWindowLimiter, Depends(get_connection_test_rate_limiter)]


def _response_of(source: DatabaseSource) -> DatabaseSourceResponse:
    """`source` for the wire: the last probe's snapshot, each view marked ticked or not, `has_dsn` for the DSN."""
    allowed = set(source.allowed_views)
    snapshot = snapshot_of(source)
    return DatabaseSourceResponse(
        id=source.id,
        name=source.name,
        dialect=source.dialect,
        has_dsn=bool(source.dsn_secret_ref),
        status=source.last_probe_status,
        status_reason=_reason_of(source),
        status_detail=source.last_probe_detail,
        last_probed_at=source.last_probed_at,
        allowed_views=source.allowed_views,
        views=[
            DatabaseViewResponse(
                name=view.name, columns=list(view.columns), row_count=view.row_count, allowed=view.name in allowed
            )
            for view in snapshot.views
        ],
        base_tables=list(snapshot.base_tables),
        created_at=source.created_at,
    )


def _reason_of(source: DatabaseSource) -> ProbeFailure | None:
    """`last_probe_reason` as the enum, or `None` for one this build does not know — `_reason_of`'s BYODB reason."""
    try:
        return ProbeFailure(source.last_probe_reason) if source.last_probe_reason else None
    except ValueError:
        return None


@router.get("", response_model=list[DatabaseSourceResponse])
def list_my_database_sources(
    user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> list[DatabaseSourceResponse]:
    """Every Hệ CSDL the caller owns, newest first."""
    return [_response_of(row) for row in list_database_sources(db, user.id)]


@router.post("", response_model=DatabaseSourceResponse, status_code=status.HTTP_201_CREATED)
def create_my_database_source(
    request: DatabaseSourceCreateRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    secret_store: SecretStoreDep,
) -> DatabaseSourceResponse:
    """Register a Hệ CSDL. Never dials out — `POST .../test` is what does."""
    source = create_database_source(
        db, user.id, name=request.name, dialect=request.dialect, dsn=request.dsn, secret_store=secret_store
    )
    return _response_of(source)


@router.get("/{source_id}", response_model=DatabaseSourceResponse)
def get_my_database_source(source: OwnedDatabaseSource) -> DatabaseSourceResponse:
    """One of the caller's own Hệ CSDL, with its last probe — the DSN still only `has_dsn`."""
    return _response_of(source)


@router.patch("/{source_id}", response_model=DatabaseSourceResponse)
def update_my_database_source(
    request: DatabaseSourceUpdateRequest,
    source: OwnedDatabaseSource,
    db: Annotated[Session, Depends(get_db)],
    secret_store: SecretStoreDep,
) -> DatabaseSourceResponse:
    """Rename, replace the DSN, or tick views — 422 naming every view that is not a view the last probe saw."""
    if request.name is not None:
        source = rename_database_source(db, source, request.name)
    if request.dsn is not None:
        source = replace_dsn(db, source, request.dsn, secret_store=secret_store)
    if request.allowed_views is not None:
        try:
            source = set_allowed_views(db, source, request.allowed_views)
        except AllowedViewsError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    return _response_of(source)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_database_source(
    source: OwnedDatabaseSource, db: Annotated[Session, Depends(get_db)], secret_store: SecretStoreDep
) -> None:
    """Remove a Hệ CSDL and its DSN — refused outright while any agent's spec reads from it."""
    try:
        delete_database_source(db, source, secret_store=secret_store)
    except DatabaseSourceInUseError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail={"message": str(exc), "agents": exc.agents}
        ) from None


@router.post("/{source_id}/test", response_model=DatabaseSourceResponse)
def test_my_database_source(
    source: OwnedDatabaseSource,
    db: Annotated[Session, Depends(get_db)],
    secret_store: SecretStoreDep,
    rate_limiter: ConnectionTestRateLimiterDep,
) -> DatabaseSourceResponse:
    """Probe for real. Always 200 — `connected`/`failed`/`needs_dsn` with a reason is the answer.

    Shares BYODB's `/test` allowance (`charge_one_connection_test`): both are a
    person dialling out by hand, and one ceiling across both is what keeps
    two endpoints from being two scanners.
    """
    try:
        charge_one_connection_test(rate_limiter, source.owner_id)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.message,
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from None
    updated = run_database_test(
        db, source, secret_store=secret_store, trusted_hosts=get_settings().database_source_trusted_host_set
    )
    return _response_of(updated)
