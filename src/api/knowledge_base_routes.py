"""Knowledge Base endpoints: a creator's shelves, and which agents may search them — #90.

Every shelf is scoped to its owner, the same `OwnedAgent`-style 404 pattern
`src.api.agent_routes.get_owned_agent` established: a Knowledge Base that
belongs to somebody else is 404, not 403, so a caller learns nothing about
who owns what they cannot reach.

Assigning shelves to an agent is a separate pair of routes, under
`/agents/{agent_id}/knowledge-bases`, rather than a field on the agent
response — the assignment is a many-to-many the caller edits as its own list
(`set_agent_knowledge_bases`), not a property that rides along with every read
of the agent.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from qdrant_client import QdrantClient
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.api.agent_routes import OwnedAgent

#: Called directly, not through `Depends`: the connection id arrives in the
#: create *body* rather than in the path, so there is no path parameter for
#: FastAPI to resolve it from — but the ownership rule a BYODB shelf has to
#: obey is exactly the one `/external-connections/{id}` already enforces, and
#: a second copy of it here is a second chance to get it wrong.
from src.api.external_connection_routes import get_owned_external_connection
from src.auth.dependencies import CurrentUser
from src.db.base import get_db
from src.db.models import KnowledgeBase
from src.models.schemas import (
    AgentKnowledgeBasesRequest,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdateRequest,
)
from src.services.qdrant import QdrantWriteError, get_qdrant_client
from src.workflow.knowledge_bases import (
    KnowledgeBaseDeleteRefusedError,
    KnowledgeBaseInUseError,
    create_knowledge_base,
    delete_knowledge_base,
    list_knowledge_bases,
    set_agent_knowledge_bases,
    update_knowledge_base,
)

#: Resolved via `Depends`, not called bare, so a test's
#: `app.dependency_overrides[get_qdrant_client]` reaches it — the same reason
#: `document_routes.py` defines its own copy of this alias rather than every
#: route importing `get_qdrant_client` and calling it directly.
QdrantClientDep = Annotated[QdrantClient, Depends(get_qdrant_client)]

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])

#: The assignment endpoints live under `/agents/{agent_id}/...` rather than
#: under this module's own prefix, so they get their own router mounted
#: separately in `src.main` — an agent's shelf list is a fact about the
#: agent, not about any one Knowledge Base.
agent_links_router = APIRouter(prefix="/agents", tags=["knowledge-bases"])


def get_owned_knowledge_base(
    kb_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> KnowledgeBase:
    """The Knowledge Base at `kb_id` if the caller owns it, otherwise 404.

    Same reasoning as `get_owned_agent`: a 403 would confirm that *someone's*
    shelf exists at this id, which is not this caller's business to know.

    A `select` with `selectinload`, not `db.get`, for the same reason
    `list_knowledge_bases` carries the same options (RAG-13 / #99):
    `KnowledgeBaseResponse.document_count`/`agent_names` read `documents`/
    `agents`, and this row backs the single-shelf GET and the rename PATCH —
    both serialize those same two fields.
    """
    knowledge_base = db.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.id == kb_id)
        .options(selectinload(KnowledgeBase.documents), selectinload(KnowledgeBase.agents))
    ).scalar_one_or_none()
    if knowledge_base is None or knowledge_base.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy Knowledge Base")
    return knowledge_base


OwnedKnowledgeBase = Annotated[KnowledgeBase, Depends(get_owned_knowledge_base)]


@router.get("", response_model=list[KnowledgeBaseResponse])
def list_my_knowledge_bases(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> list[KnowledgeBase]:
    """Every shelf the caller owns, newest first."""
    return list(list_knowledge_bases(db, user.id))


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
def create_my_knowledge_base(
    request: KnowledgeBaseCreateRequest, user: CurrentUser, db: Annotated[Session, Depends(get_db)]
) -> KnowledgeBase:
    """Start a new shelf, in either of RAG-22's two modes.

    `managed` is the default and the ordinary case: an empty shelf that
    `POST /knowledge-bases/{id}/documents` then puts Documents on.

    `byodb` (#108) instead names one of the caller's own
    `ExternalConnection`s and never accepts an upload at all
    (`src.workflow.documents.store_document` refuses one) — the platform
    queries that database, and neither indexes nor changes what is in it.
    A connection the caller does not own is 404 rather than 403, the same
    reason `get_owned_knowledge_base` gives.
    """
    connection = None
    if request.external_connection_id is not None:
        connection = get_owned_external_connection(request.external_connection_id, user, db)
    return create_knowledge_base(db, user.id, request.name, request.description, external_connection=connection)


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
def get_my_knowledge_base(knowledge_base: OwnedKnowledgeBase) -> KnowledgeBase:
    """One of the caller's own shelves, in full."""
    return knowledge_base


@router.patch("/{kb_id}", response_model=KnowledgeBaseResponse)
def update_my_knowledge_base(
    knowledge_base: OwnedKnowledgeBase, request: KnowledgeBaseUpdateRequest, db: Annotated[Session, Depends(get_db)]
) -> KnowledgeBase:
    """Rename the shelf or change its description. Either field left out keeps its current value."""
    return update_knowledge_base(db, knowledge_base, name=request.name, description=request.description)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_knowledge_base(
    knowledge_base: OwnedKnowledgeBase,
    db: Annotated[Session, Depends(get_db)],
    qdrant_client: QdrantClientDep,
    confirm: bool = False,
) -> None:
    """Remove a shelf — its Documents, its Qdrant points, and its agent assignments go with it (RAG-09).

    Two refusals, both 409, told apart by whether `confirm=true` gets past
    them — see `KnowledgeBaseInUseError`/`KnowledgeBaseLinkedError` in
    `src.workflow.knowledge_bases` for the reasoning:

    - A confirmed graph naming this shelf blocks the delete outright.
      `confirm` cannot override it; the graph is immutable, so there is
      nothing to re-confirm out of the way.
    - An Agent currently linked (`agent_kb_links`) is a warning naming who —
      call again with `?confirm=true` to delete anyway.

    Either refusal's 409 body carries `affected_agents` as a plain list
    alongside the human-readable `message` (RAG-13) — the confirmation screen
    lists them by name before a caller chooses to pass `confirm=true`, and a
    list is what it needs, not a sentence to parse back apart. `blocking`
    says which of the two refusals this was: `true` means no `confirm` will
    ever get past it.

    A Qdrant failure is a 503: nothing wrong with the request, retry once
    the dependency is reachable.
    """
    try:
        delete_knowledge_base(db, knowledge_base, qdrant_client=qdrant_client, confirm=confirm)
    except KnowledgeBaseDeleteRefusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "affected_agents": exc.agents,
                "blocking": isinstance(exc, KnowledgeBaseInUseError),
            },
        ) from None
    except QdrantWriteError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None


@agent_links_router.get("/{agent_id}/knowledge-bases", response_model=list[KnowledgeBaseResponse])
def list_agent_knowledge_bases(agent: OwnedAgent) -> list[KnowledgeBase]:
    """Every shelf `agent` may currently search."""
    return list(agent.knowledge_bases)


@agent_links_router.put("/{agent_id}/knowledge-bases", response_model=list[KnowledgeBaseResponse])
def set_my_agent_knowledge_bases(
    agent: OwnedAgent,
    request: AgentKnowledgeBasesRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> list[KnowledgeBase]:
    """Replace the whole set of shelves `agent` may search.

    Every id must be one of the caller's own Knowledge Bases — assigning
    somebody else's shelf to your agent is refused the same way addressing
    somebody else's agent is, by 404 rather than by a silent no-op.
    """
    owned_ids = {knowledge_base.id for knowledge_base in list_knowledge_bases(db, user.id)}
    unknown = set(request.knowledge_base_ids) - owned_ids
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không tìm thấy Knowledge Base: {sorted(unknown)}",
        )
    knowledge_bases = [db.get(KnowledgeBase, kb_id) for kb_id in request.knowledge_base_ids]
    updated = set_agent_knowledge_bases(db, agent, [kb for kb in knowledge_bases if kb is not None])
    return list(updated.knowledge_bases)
