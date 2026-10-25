"""How many Documents one user may upload per minute — #101.

Reuses `SlidingWindowLimiter` (`src.deployments.rate_limit`, #51 / ADR-0037)
rather than a second sliding-window implementation: the counting problem is
identical — "how many of X may caller C do inside this window" — and differs
only in what X is and what the ceiling is read from. There, X is a run and the
ceiling is a Deployment's own column; here, X is an upload and the ceiling is a
platform-wide setting, because an upload does not belong to any one Deployment
for a per-resource ceiling to live on.
"""

from src.config import get_settings
from src.deployments.rate_limit import RateLimitExceededError, SlidingWindowLimiter

__all__ = ["RateLimitExceededError", "charge_one_upload", "get_upload_rate_limiter"]

#: The "deployment_id" half of `SlidingWindowLimiter`'s `(deployment_id, caller)`
#: key. Uploads have no Deployment to key by, so every caller shares this one
#: fixed bucket — `caller` (the uploader's user id, as a string) is still what
#: actually separates one person's window from the next person's.
_UPLOAD_BUCKET = 0

#: Process-wide, like `src.deployments.rate_limit`'s own counters (ADR-0037
#: §4) — reachable through `get_upload_rate_limiter` so a test replaces it the
#: same way `test_deployment_rate_limit.py` replaces that one.
_limiter = SlidingWindowLimiter()


def get_upload_rate_limiter() -> SlidingWindowLimiter:
    """FastAPI dependency: the counters this process is enforcing uploads against."""
    return _limiter


def charge_one_upload(limiter: SlidingWindowLimiter, owner_id: int) -> None:
    """Count one upload against `owner_id`'s allowance, or raise `RateLimitExceededError`.

    Charged before the file is stored — the point of the ceiling is to bound
    how often an expensive pipeline (RAG-08) is set in motion, and a caller
    told off only after their upload was written would have been told off too
    late to matter.
    """
    limiter.charge(
        deployment_id=_UPLOAD_BUCKET,
        caller=str(owner_id),
        limit=get_settings().document_upload_rate_limit_per_user,
    )
