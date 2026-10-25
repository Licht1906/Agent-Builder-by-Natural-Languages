"""How many times one creator may call `POST /external-connections/{id}/test` per minute — RAG-20 / #106.

`upload_rate_limit`'s reason, restated for a different door: `SlidingWindowLimiter`
already answers "how many of X may caller C do inside this window", and X here
is a probe that actually dials a creator-supplied address — the ticket's own
"Rate limit /test", there so `/test` cannot be turned into a throttle-free
network scanner even after `src.services.ssrf_guard` closes the internal-address
hole.
"""

from src.config import get_settings
from src.deployments.rate_limit import RateLimitExceededError, SlidingWindowLimiter

__all__ = ["RateLimitExceededError", "charge_one_connection_test", "get_connection_test_rate_limiter"]

#: The "deployment_id" half of `SlidingWindowLimiter`'s `(deployment_id, caller)`
#: key. A connection test has no Deployment to key by, so every caller shares
#: this one fixed bucket — `caller` (the owner's user id, as a string) is what
#: actually separates one person's window from the next's.
_CONNECTION_TEST_BUCKET = 1

#: Process-wide, like `src.services.upload_rate_limit`'s own counter — reachable
#: through `get_connection_test_rate_limiter` so a test replaces it the same way.
_limiter = SlidingWindowLimiter()


def get_connection_test_rate_limiter() -> SlidingWindowLimiter:
    """FastAPI dependency: the counters this process is enforcing connection tests against."""
    return _limiter


def charge_one_connection_test(limiter: SlidingWindowLimiter, owner_id: int) -> None:
    """Count one `/test` call against `owner_id`'s allowance, or raise `RateLimitExceededError`.

    Charged before the probe runs, `charge_one_upload`'s reason: the point of
    the ceiling is to bound how often an outbound dial is set in motion, and a
    caller told off only after the probe already ran would have been told off
    too late to matter.
    """
    limiter.charge(
        deployment_id=_CONNECTION_TEST_BUCKET,
        caller=str(owner_id),
        limit=get_settings().external_connection_test_rate_limit_per_user,
    )
