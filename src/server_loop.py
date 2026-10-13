"""The event loop this app has to run on.

psycopg's async driver refuses to run on Windows' `ProactorEventLoop`, and the
conversation checkpointer (#6) is async psycopg. Uvicorn hands `asyncio.run` an
explicit loop factory, which no event-loop *policy* can override, and on Windows
that factory is `ProactorEventLoop` — so on a developer's Windows machine the
first chat hangs for 30 seconds and then fails with `PoolTimeout`.

Uvicorn's `--loop` accepts an import string pointing at a loop factory, which is
how the choice becomes the app's rather than the platform default's. Named by
the Makefile and the Dockerfile, so both get it.

`src.agents.memory` sets the event-loop *policy* for the same reason. That covers
callers that let asyncio pick a loop (pytest-asyncio, a script); this covers the
server, which does not. Neither replaces the other.

On Linux — every deployed target — this defers to uvicorn's own choice, so
uvloop is still used when it is installed.
"""

import asyncio
import sys


def new_event_loop() -> asyncio.AbstractEventLoop:
    """A loop the async Postgres driver will actually talk to."""
    if sys.platform == "win32":  # pragma: no cover — CI and production are Linux
        return asyncio.SelectorEventLoop()

    from uvicorn.loops.auto import auto_loop_factory

    return auto_loop_factory()()
