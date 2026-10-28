"""UC1-26 (#142) — the one, explicit, pull-only door to the vendor's control
plane (spec.md §4.7/§12.4, FR-52).

`pull_template` is the *only* function in this codebase allowed to reach the
control plane, and the only thing this module exports that ever opens an
outbound connection. Nothing on the request path an ordinary user's action
goes through — creating an agent, editing one, a chat turn — calls this module
at all: `src.api.control_plane_routes` is its one importer
(`tests/test_services/test_control_plane.py` holds that). §12.4's own table names exactly two events that may: "tạo
version mới cho agent, nếu cần template mới" and "cập nhật phiên bản phần
mềm" — both explicit, admin-initiated actions, never something a request
triggers as a side effect. `src.api.control_plane_routes` is the one HTTP
door onto this function, gated the same `AdminUser` way every other UC-1
admin action is.

Signature verification, not transport encryption, is what this module trusts:
a pulled artifact is refused unless it verifies against
`settings.control_plane_public_key` (Ed25519), matching §12.3's own air-gap
story — an artifact imported offline from a signed `.tar` has to clear the
same check a live pull would, so "verify before applying" is one code path,
not two.
"""

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from src.config import get_settings

logger = logging.getLogger("control_plane")


class ControlPlaneError(RuntimeError):
    """The pull failed outright — unreachable, malformed response, or (the one
    that matters) a signature that does not verify. Never a partial success:
    an artifact this module cannot vouch for is not returned at all.
    """


@dataclass(frozen=True, slots=True)
class PulledTemplate:
    template_id: str
    build_id: str
    content: dict[str, Any]


def _canonical_bytes(content: dict[str, Any]) -> bytes:
    """The exact bytes the vendor's own signer must have signed — sorted keys,
    no incidental whitespace, so this module and the signer can never disagree
    about what "the content" was just because a JSON encoder reordered it.
    """
    return json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_signature(content: dict[str, Any], signature_b64: str, public_key_b64: str) -> None:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        public_key.verify(base64.b64decode(signature_b64), _canonical_bytes(content))
    except (InvalidSignature, ValueError) as exc:
        raise ControlPlaneError(f"chữ ký template không hợp lệ, từ chối áp dụng: {exc}") from exc


def pull_template(template_id: str) -> PulledTemplate:
    """FR-52: pull `template_id`, refuse it unless its signature verifies, and
    log exactly one line *every* time this is called — "log mỗi lần gọi", a
    denied pull included, not only an accepted one — carrying only the
    template/build identifier, never the pulled content and never anything
    about the caller's own agents or data ("không gửi dữ liệu nghiệp vụ" cuts
    both directions: nothing sensitive goes out in the request, and nothing
    about what came back is logged beyond what identifies it). The `finally`
    below is what makes the log line unconditional: every `raise` in this
    function still passes through it before the caller ever sees the error.
    """
    settings = get_settings()
    build_id = ""
    outcome = "denied"
    try:
        if not settings.control_plane_url:
            raise ControlPlaneError("CONTROL_PLANE_URL chưa được cấu hình")
        if not settings.control_plane_public_key:
            raise ControlPlaneError("CONTROL_PLANE_PUBLIC_KEY chưa được cấu hình")

        try:
            response = httpx.get(
                f"{settings.control_plane_url}/templates/{template_id}", timeout=settings.control_plane_http_timeout_seconds
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise ControlPlaneError(f"không gọi được control plane: {exc}") from exc

        try:
            content = body["content"]
            signature = body["signature"]
        except (KeyError, TypeError) as exc:
            raise ControlPlaneError(f"phản hồi control plane thiếu trường bắt buộc: {exc}") from exc
        _verify_signature(content, signature, settings.control_plane_public_key)

        build_id = str(body.get("build_id", ""))
        outcome = "accepted"
        return PulledTemplate(template_id=template_id, build_id=build_id, content=content)
    finally:
        logger.info("control_plane_pull template_id=%s build_id=%s outcome=%s", template_id, build_id, outcome)
