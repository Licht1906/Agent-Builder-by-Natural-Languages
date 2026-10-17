"""How often one caller may set a Deployment running — #51 / ADR-0037.

`Deployment.rate_limit_per_user` has been a column since #50 and a promise since
ADR-0029 §6: a ceiling per `(deployment, caller)`, never one shared number that a
single heavy caller could spend on everybody else's behalf. This module is the
counting, and it is deliberately the *only* thing in it — who the caller is was
settled by `src.api.deployment_access.Caller`, and what a run costs once it is
running belongs to `src.agents.budget`.

Two different mechanisms on purpose. The budget ledger bounds the spend *inside*
one run; this bounds how often a caller may start one. A single mechanism could
not do both: the ledger's numbers are only known once a run is over, and by then
the expensive thing has already happened.

The counters live in this process (ADR-0037 §4). That is a real limitation and it
is written down rather than implied: run two workers and the effective ceiling is
two ceilings, restart and everybody is forgiven. It is the right trade for a
cost-control ceiling on a single-worker deployment, and it is one class behind one
method so moving the counts to shared storage is a substitution rather than a
rewrite.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable

#: The length of the window `rate_limit_per_user` is measured over. Sixty because
#: the column has said "mỗi phút" since #50 — the number on the owner's screen and
#: the number counted here have to mean the same thing.
WINDOW_SECONDS = 60

#: Below this many tracked callers, sweeping costs more than the memory it saves:
#: a window that empties is dropped on the caller's next request anyway, and what
#: the sweep is really for is the burst of one-off callers that never come back.
_SWEEP_ABOVE = 32


class RateLimitExceededError(Exception):
    """A caller who has already used their allowance for the current window.

    Carries the numbers rather than only a sentence, for `BudgetExceededError`'s
    reason: the ceiling that bit and the wait it implies are what let an owner
    tell "my limit is too tight" from "somebody is hammering my agent", and a bare
    "too many requests" tells them apart for nobody.
    """

    def __init__(self, *, limit: int, retry_after_seconds: int) -> None:
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Vượt giới hạn {limit} request mỗi phút cho mỗi người gọi tới deployment này "
            f"— thử lại sau {retry_after_seconds} giây."
        )

    @property
    def message(self) -> str:
        """The human-readable reason, for the client that is about to be refused."""
        return str(self)


class SlidingWindowLimiter:
    """Every caller's recent requests, and the refusal when there are too many.

    One deque of timestamps per `(deployment_id, caller)`, trimmed to the last
    `WINDOW_SECONDS` on every touch — so the count *is* the length of the deque
    and there is no separate number to keep in step with it.

    Sliding rather than fixed (ADR-0037 §3): a fixed window lets a caller send
    their whole allowance either side of one boundary and be twice over the
    ceiling within a second, which makes the limit something that has to be
    explained rather than trusted. It is also what makes `retry_after_seconds`
    exact — the oldest request in the window is precisely when a slot frees.

    Locked, because FastAPI runs a synchronous dependency in a threadpool: two
    requests from the same caller really can land in `charge` at the same moment,
    and a read-modify-write on a shared deque is how a ceiling quietly becomes a
    suggestion.
    """

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._requests: dict[tuple[int, str], deque[float]] = {}
        self._swept_at = now()

    @property
    def tracked_callers(self) -> int:
        """How many callers are currently being remembered — what the sweep bounds."""
        with self._lock:
            return len(self._requests)

    def charge(self, *, deployment_id: int, caller: str, limit: int) -> None:
        """Record one request by `caller`, or raise if it would be one too many.

        Charged before the run rather than after it (ADR-0037 §1): the point of
        the ceiling is to bound how often something expensive is set in motion,
        and a caller told off only once their hundred concurrent runs finished
        would have been told off far too late.

        A refused request is **not** recorded. Counting the knocking would let a
        caller who keeps retrying push their own window forward indefinitely and
        never be let back in — a ceiling that punishes impatience with a
        permanent ban is not the ceiling anybody configured.
        """
        with self._lock:
            now = self._now()
            self._sweep(now)
            window = self._requests.setdefault((deployment_id, caller), deque())
            self._trim(window, now)
            if len(window) >= limit:
                raise RateLimitExceededError(limit=limit, retry_after_seconds=self._retry_after(window, now))
            window.append(now)

    def clear(self) -> None:
        """Forget every caller — what a process restart does, and what a test needs."""
        with self._lock:
            self._requests.clear()

    def _trim(self, window: deque[float], now: float) -> None:
        """Drop the requests that have aged out of the window."""
        cutoff = now - WINDOW_SECONDS
        while window and window[0] <= cutoff:
            window.popleft()

    def _retry_after(self, window: deque[float], now: float) -> int:
        """When the oldest request frees its slot, in whole seconds and never zero.

        Rounded up, and floored at one: `Retry-After: 0` reads as "try again now",
        which is an invitation to be refused a second time by the same window.

        An **empty** window means the refusal did not come from a full window at
        all — it came from a ceiling below one, which is a row the API can no
        longer write (`DeploymentRateLimitRequest`) but a fixture, a script or a
        hand-edited row still can. Such a caller is refused for the whole window
        rather than crashing on an index that is not there: a corrupt ceiling must
        still produce the 429 this ticket promises, never the generic 500 it
        forbids.
        """
        if not window:
            return WINDOW_SECONDS
        return max(1, math.ceil(window[0] + WINDOW_SECONDS - now))

    def _sweep(self, now: float) -> None:
        """Drop callers whose window has emptied, at most once per window.

        A caller who is still active pays nothing for this: their own deque is
        trimmed on their own request. The sweep exists for the ones who never come
        back — without it the map grows with every caller the platform has ever
        seen, which for an API-facing product is a leak with a schedule.
        """
        if len(self._requests) <= _SWEEP_ABOVE or now - self._swept_at < WINDOW_SECONDS:
            return
        self._swept_at = now
        cutoff = now - WINDOW_SECONDS
        self._requests = {key: window for key, window in self._requests.items() if window and window[-1] > cutoff}


#: The platform's counters. Process-wide because the limit is per caller per
#: Deployment and not per request, so there is exactly one of it — and reachable
#: through `get_rate_limiter` so a test (or a future shared-storage implementation)
#: replaces it the way every other seam in this codebase is replaced.
_limiter = SlidingWindowLimiter()


def get_rate_limiter() -> SlidingWindowLimiter:
    """FastAPI dependency: the counters this process is enforcing against."""
    return _limiter
