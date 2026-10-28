"""UC1-08 (#122) — `/connections`, spec.md §19.

Deliberately its own router, reachable from nothing the meta-agent's prompt
touches (`src.metaagent.draft_spec` never imports this module and this module
never imports it back) — FR-21's "credential đi thẳng từ frontend vào kho secret
qua endpoint riêng, không đi qua context window của LLM" is a fact about the
call graph, not a runtime filter stripping a field. Every route requires
`AgentUser` (§3's actor table: nhân viên CNTT / `agent_admin` wires up
connections, not `sales_rep`/`sales_manager`).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.auth.dependencies import AdminUser
from src.db.base import get_db
from src.db.models import UC1Connection
from src.models.schemas import UC1ConnectionCreateRequest, UC1ConnectionResponse
from src.services.secret_store import SecretStoreDep
from src.workflow.uc1_connections import (
    ConnectionAlreadyExistsError,
    UnsupportedProbeProviderError,
    create_connection,
    list_connections,
    run_probe,
)

router = APIRouter(prefix="/connections", tags=["connections"])


def get_owned_connection(connection_id: int, db: Annotated[Session, Depends(get_db)]) -> UC1Connection:
    """The connection at `connection_id`, or 404 — connections are `agent_admin`-shared, not per-owner."""
    connection = db.get(UC1Connection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy kết nối")
    return connection


OwnedConnection = Annotated[UC1Connection, Depends(get_owned_connection)]


@router.get("", response_model=list[UC1ConnectionResponse])
def list_my_connections(user: AdminUser, db: Annotated[Session, Depends(get_db)]) -> list[UC1ConnectionResponse]:
    """FR-22: every connection, never a credential."""
    return [UC1ConnectionResponse.model_validate(row) for row in list_connections(db)]


@router.post("", response_model=UC1ConnectionResponse, status_code=status.HTTP_201_CREATED)
def create_my_connection(
    request: UC1ConnectionCreateRequest, user: AdminUser, db: Annotated[Session, Depends(get_db)], secrets: SecretStoreDep
) -> UC1ConnectionResponse:
    """FR-20/FR-21: write the credential to the secret store, return only its pointer."""
    try:
        connection = create_connection(
            db,
            secrets,
            name=request.name,
            type_=request.type,
            credential=request.credential,
            allowed_objects=request.allowed_objects,
            allowed_hosts=request.allowed_hosts,
            read_only=request.read_only,
            created_by_id=user.id,
        )
    except ConnectionAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return UC1ConnectionResponse.model_validate(connection)


@router.post("/{connection_id}/probe", response_model=UC1ConnectionResponse)
def probe_my_connection(
    connection: OwnedConnection, user: AdminUser, db: Annotated[Session, Depends(get_db)], secrets: SecretStoreDep
) -> UC1ConnectionResponse:
    """FR-23: dry-run the connection for real. Always 200 — `connected`/`failed` is the answer, never a crash."""
    try:
        updated = run_probe(db, secrets, connection)
    except UnsupportedProbeProviderError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return UC1ConnectionResponse.model_validate(updated)
