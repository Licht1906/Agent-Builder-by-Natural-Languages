"""UC1-05/UC1-06 (#119, #120) — the `metaagent` service's one door (spec.md §6.2,
§19): turn a natural-language description into a draft `AgentSpec` plus
`clarifications` (FR-10, FR-11, FR-14).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from src.agentspec.validation import Clarification
from src.auth.dependencies import CurrentUser
from src.metaagent.draft_spec import DraftSpecError, draft_spec
from src.services.llm import ChatModelFactory, get_chat_model_factory

router = APIRouter(prefix="/metaagent", tags=["metaagent"])


class DraftSpecRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=4000)


class DraftSpecResponse(BaseModel):
    """`spec` and `clarifications` as two separate top-level keys (UC1-06, FR-14).

    Never nested inside each other: `clarifications` is stripped out of the spec
    on the way in (`validate_drafted_spec`) precisely so a form can render a
    clarification beside the field it asks about without first having to dig it
    back out of the document it lives beside.
    """

    spec: dict[str, Any]
    clarifications: list[Clarification]


@router.post("/draft-spec", response_model=DraftSpecResponse)
async def post_draft_spec(
    payload: DraftSpecRequest,
    user: CurrentUser,
    model_factory: Annotated[ChatModelFactory, Depends(get_chat_model_factory)],
) -> DraftSpecResponse:
    """FR-10/FR-11: a natural-language description becomes a pre-filled config
    form. FR-14: `clarifications` — the points the meta-agent had to guess about
    — ride beside the spec instead of being buried in a log.
    """
    principal = {
        "sub": str(user.id),
        "employee_id": user.employee_id,
        "department": user.department,
        "roles": [user.role.value],
    }
    try:
        drafted = await draft_spec(payload.description, model_factory=model_factory, principal=principal)
    except DraftSpecError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return DraftSpecResponse(
        spec=drafted.spec.model_dump(mode="json", by_alias=True),
        clarifications=drafted.clarifications,
    )
