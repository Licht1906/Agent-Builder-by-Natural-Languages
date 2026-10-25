"""UC1-26 (#142) — `POST /control-plane/templates/{template_id}/pull`, the one
HTTP door onto `src.services.control_plane.pull_template` (see that module's
own docstring for why nothing else may reach the control plane at all).
Admin-gated the same way every other UC-1 admin action is (deploy, suspend,
sandbox chat): pulling a new template is an explicit, deliberate act, never
something a request triggers on a caller's behalf.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from src.auth.dependencies import AdminUser
from src.services.control_plane import ControlPlaneError, pull_template

router = APIRouter(prefix="/control-plane", tags=["control-plane"])


class PullTemplateResponse(BaseModel):
    template_id: str
    build_id: str
    content: dict[str, Any]


@router.post("/templates/{template_id}/pull", response_model=PullTemplateResponse)
def pull_template_route(template_id: str, user: AdminUser) -> PullTemplateResponse:
    try:
        pulled = pull_template(template_id)
    except ControlPlaneError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    return PullTemplateResponse(template_id=pulled.template_id, build_id=pulled.build_id, content=pulled.content)
