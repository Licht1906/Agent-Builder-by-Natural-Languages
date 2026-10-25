"""UC1-24 (#140) — `GET /agents/{id}/spec-versions` + `PATCH
/agents/{id}/versions/{v}`, spec.md §13/§19, FR-13/FR-50. `POST
/agents/{id}/spec-versions` (UC1-04's E2 `confirm`) joins them: the three
together are the whole life of a spec — created once, read back, edited into
successors.

`spec-versions` rather than spec.md §19's literal `versions`: V2's own
`src.api.agent_routes` already owns `GET /agents/{agent_id}/versions` (an
`int`-keyed `AgentVersion`, a different table entirely) under the same
`/agents` prefix, registered first in `src.main`. Starlette dispatches on path
*shape*, not on a path param's declared Python type — a request to
`/agents/agt_x/versions` would reach V2's handler first regardless, and only
fail once FastAPI tries to coerce `"agt_x"` into an `int` (a 422 that looks
like this endpoint doesn't exist, not like it was shadowed). `uc1_approval_routes.
queue_router`'s own docstring already made this exact call for the same reason
(`/approvals` instead of colliding with V2's `/admin/approvals`); this module
follows it. `PATCH /agents/{id}/versions/{v}` has no V2 counterpart at that
shape (V2 has no `PATCH .../versions/{v}` at all) and keeps spec.md's literal
path.

Editing is never an in-place update: `patch_spec_version` merges the caller's
field overrides into the *current* version's own spec, runs the merged result
through `validate_spec` (the strict door — FR-13's guardrail-baseline gate:
additions welcome, removals refused, same as any human edit), then calls
`src.agentspec.lifecycle.new_draft_version` to allocate `v+1` as a fresh
draft. The version named in the URL is never written to: `save_spec_version`
only ever inserts a new row, and nothing in this module calls
`update_spec_version_status` on the version being edited — which is what
makes "sửa spec 3 lần → 3 version độc lập, version cũ không đổi nội dung"
true by construction rather than by a caller remembering not to.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agentspec.lifecycle import new_draft_version
from src.agentspec.spec import SpecStatus
from src.agentspec.store import SpecVersionNotFoundError, get_spec_version, save_spec_version
from src.agentspec.validation import SpecValidationError, validate_spec
from src.auth.dependencies import AdminUser
from src.db.base import get_db
from src.db.models import UC1AgentSpecVersion

router = APIRouter(prefix="/agents", tags=["uc1-agent"])

#: Its own prefix rather than a `GET /agents/...` sibling, for the reason this
#: module's docstring already gives about `spec-versions`: `/agents` is V2's
#: `int`-keyed surface, registered first, and Starlette dispatches on path
#: shape. A literal `/agents/catalog` would be reachable, but only by being
#: careful about registration order forever — `queue_router` in
#: `uc1_approval_routes` made the same call for the same reason.
catalog_router = APIRouter(prefix="/uc1-agents", tags=["uc1-agent"])

#: Top-level fields a `PATCH` body never controls — the document envelope.
#: `metadata` itself is *not* here: FR-13 is "mọi trường trừ guardrail
#: baseline", and `metadata.name`/`metadata.description` are ordinary,
#: human-editable fields — only `_PLATFORM_CONTROLLED_METADATA_FIELDS` below
#: is off limits within it.
_PLATFORM_CONTROLLED_TOP_LEVEL_FIELDS = ("apiVersion", "kind")

#: Identity/lifecycle sub-fields of `metadata` a `PATCH` body never controls,
#: pinned back to the *current* version's own values before validation runs
#: regardless of what the caller sent — the same "hard rule"
#: `validate_drafted_spec` already applies to the meta-agent's own output
#: (forcing `metadata.status` itself), applied here to a human's edit instead
#: of a model's.
_PLATFORM_CONTROLLED_METADATA_FIELDS = ("id", "version", "status", "approved_by", "parent_version")

#: What `create_first_spec_version` always writes. Named rather than inlined
#: because `new_draft_version`'s `v+1` is the *only* other way a version number
#: is ever chosen on this platform, and both should stay easy to find together.
_FIRST_VERSION = 1


def _merged_edit(current_raw: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """`current_raw` with `payload`'s edits folded in — every top-level field
    payload names except the envelope, and within `metadata` specifically,
    every sub-field except the identity/lifecycle ones, which always keep
    `current_raw`'s own value no matter what `payload` sent for them.
    """
    edits = {key: value for key, value in payload.items() if key not in _PLATFORM_CONTROLLED_TOP_LEVEL_FIELDS}
    merged = {**current_raw, **edits}
    current_metadata = current_raw["metadata"]
    payload_metadata = edits.get("metadata")
    edited_metadata = payload_metadata if isinstance(payload_metadata, dict) else current_metadata
    pinned = {field: current_metadata[field] for field in _PLATFORM_CONTROLLED_METADATA_FIELDS if field in current_metadata}
    merged["metadata"] = {**current_metadata, **edited_metadata, **pinned}
    return merged


class ProblemResponse(BaseModel):
    code: str
    path: str
    message: str


class VersionSummaryResponse(BaseModel):
    version: int
    status: str
    name: str
    parent_version: int | None
    created_at: str


class SpecVersionResponse(BaseModel):
    agent_id: str
    version: int
    status: str
    parent_version: int | None
    spec: dict[str, Any]


def _summary(row: UC1AgentSpecVersion) -> VersionSummaryResponse:
    metadata = row.spec["metadata"]
    return VersionSummaryResponse(
        version=row.version,
        status=metadata["status"],
        name=metadata["name"],
        parent_version=metadata.get("parent_version"),
        created_at=row.created_at.isoformat(),
    )


def _spec_response(row: UC1AgentSpecVersion) -> SpecVersionResponse:
    metadata = row.spec["metadata"]
    return SpecVersionResponse(
        agent_id=row.agent_id,
        version=row.version,
        status=metadata["status"],
        parent_version=metadata.get("parent_version"),
        spec=row.spec,
    )


@router.get("/{agent_id}/spec-versions", response_model=list[VersionSummaryResponse])
def list_versions(agent_id: str, user: AdminUser, db: Annotated[Session, Depends(get_db)]) -> list[VersionSummaryResponse]:
    """FR-50: every version this agent has ever had, oldest first — the reading
    surface AC-24 needs ("3 version độc lập truy vấn lại được").
    """
    rows = (
        db.execute(select(UC1AgentSpecVersion).where(UC1AgentSpecVersion.agent_id == agent_id).order_by(UC1AgentSpecVersion.version))
        .scalars()
        .all()
    )
    return [_summary(row) for row in rows]


class AgentCatalogEntryResponse(BaseModel):
    """One UC-1 agent, as the catalog lists it: its newest version and where that version stands."""

    agent_id: str
    name: str
    latest_version: int
    status: str
    version_count: int


@catalog_router.get("", response_model=list[AgentCatalogEntryResponse])
def list_uc1_agents(user: AdminUser, db: Annotated[Session, Depends(get_db)]) -> list[AgentCatalogEntryResponse]:
    """Every UC-1 agent that has at least one spec version, newest first.

    The way in. Every other endpoint in this module and in
    `uc1_approval_routes`/`sandbox_routes` is addressed by `(agent_id,
    version)`, and until this existed nothing handed those out: an agent
    confirmed five minutes ago could only be reached by whoever still had the
    id in front of them. A queue (`GET /approvals/queue`) is not a substitute
    — it shows `pending_approval` only, which is precisely the state an agent
    reaches *after* the sandbox and golden-set work this catalog exists to
    lead to.

    One row per agent, keyed on the highest version rather than one row per
    version: `GET /agents/{id}/spec-versions` is the per-agent history and
    repeating it here would make the catalog grow with every edit.
    """
    rows = (
        db.execute(select(UC1AgentSpecVersion).order_by(UC1AgentSpecVersion.agent_id, UC1AgentSpecVersion.version))
        .scalars()
        .all()
    )
    newest: dict[str, UC1AgentSpecVersion] = {}
    counts: dict[str, int] = {}
    for row in rows:
        newest[row.agent_id] = row  # ordered by version, so the last write wins
        counts[row.agent_id] = counts.get(row.agent_id, 0) + 1
    return sorted(
        (
            AgentCatalogEntryResponse(
                agent_id=agent_id,
                name=row.spec["metadata"]["name"],
                latest_version=row.version,
                status=row.spec["metadata"]["status"],
                version_count=counts[agent_id],
            )
            for agent_id, row in newest.items()
        ),
        key=lambda entry: entry.agent_id,
    )


@router.post("/{agent_id}/spec-versions", response_model=SpecVersionResponse, status_code=status.HTTP_201_CREATED)
def create_first_spec_version(
    agent_id: str, payload: dict[str, Any], user: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> SpecVersionResponse:
    """UC1-04 (E2), spec.md §19: confirm a drafted `AgentSpec` as this agent's version 1.

    The link that used to be missing. `POST /metaagent/draft-spec` produced a
    spec and handed it back as a *value* — nothing on the platform could store
    one, so a draft lived only in the browser tab that asked for it and
    `PATCH .../versions/{v}` below had no version 1 to edit. Every UC1 test in
    this codebase worked around that by inserting the row directly
    (`tests/test_api/test_uc1_demo_rehearsal.py`'s "persistence bridge"); this
    is that bridge as a real endpoint.

    Only ever version 1. Editing is `PATCH .../versions/{v}`'s job and always
    allocates `v+1` — a second call here is a 409, not a silent second draft,
    because "confirm this draft" and "revise what is already confirmed" are
    different intentions and the second one has a different endpoint. That also
    keeps the caller off the `(agent_id, version)` unique index, which would
    otherwise answer a double-click with a 500.

    `metadata`'s identity/lifecycle fields are pinned exactly the way
    `_merged_edit` pins them for a human's edit: the URL names the agent, the
    platform names the version and the status. A model that drafted itself
    `version: 7` or `status: approved` is not granting itself either.
    """
    already = db.execute(
        select(UC1AgentSpecVersion.version).where(UC1AgentSpecVersion.agent_id == agent_id).limit(1)
    ).scalar_one_or_none()
    if already is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Agent {agent_id!r} đã có version rồi — muốn sửa thì dùng "
                f"PATCH /agents/{agent_id}/versions/{{version}}, mỗi lần sửa sinh một version mới"
            ),
        )

    metadata = payload.get("metadata")
    pinned = {
        **(metadata if isinstance(metadata, dict) else {}),
        "id": agent_id,
        "version": _FIRST_VERSION,
        "status": SpecStatus.DRAFT.value,
        "parent_version": None,
        "approved_by": [],
    }
    try:
        confirmed = validate_spec({**payload, "metadata": pinned})
    except SpecValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[ProblemResponse(code=p.code, path=p.path, message=p.message).model_dump() for p in exc.problems],
        ) from None

    return _spec_response(save_spec_version(db, confirmed, created_by_id=user.id))


@router.patch("/{agent_id}/versions/{version}", response_model=SpecVersionResponse)
def patch_spec_version(
    agent_id: str, version: int, payload: dict[str, Any], user: AdminUser, db: Annotated[Session, Depends(get_db)]
) -> SpecVersionResponse:
    """FR-13/FR-50: never mutates `version` — always inserts `version + 1` as a
    fresh draft. Valid regardless of `version`'s own current status
    (§13: "bất kỳ → draft (v+1)"), a `deployed` version included.
    """
    try:
        current = get_spec_version(db, agent_id, version)
    except SpecVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None

    merged_raw = _merged_edit(current.model_dump(mode="json", by_alias=True), payload)
    try:
        edited = validate_spec(merged_raw)
    except SpecValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[ProblemResponse(code=p.code, path=p.path, message=p.message).model_dump() for p in exc.problems],
        ) from None

    row = save_spec_version(db, new_draft_version(edited), created_by_id=user.id)
    return _spec_response(row)
