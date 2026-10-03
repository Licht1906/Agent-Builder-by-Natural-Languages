"""Fixtures for the HTTP suite — everything here exists because of the checkpointer.

The conversation checkpointer (#6) is a process-wide singleton holding a
Postgres connection pool. In production that is exactly right: one process, one
loop, one pool for its whole life. Under pytest-asyncio each test gets its own
event loop, so a pool built during one test is bound to a loop that is closed by
the time the next test runs — reusing it is undefined, and closing it from the
wrong loop raises.

So the pool is torn down after every test that could have opened one. Each test
therefore builds and disposes of its own, on its own loop.
"""

import pytest_asyncio

from src.agents.memory import close_checkpointer


@pytest_asyncio.fixture(autouse=True)
async def _checkpointer_scoped_to_one_test():
    yield
    await close_checkpointer()
