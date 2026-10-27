"""Fixtures for the package's runtime — the checkpointer, for `tests/test_api/conftest.py`'s reason.

The package runs the same process-wide checkpointer pool the platform does,
and under pytest-asyncio each test has its own event loop: a pool opened in
one test is bound to a loop gone by the next. Torn down after every test.
"""

import pytest_asyncio

from src.agents.memory import close_checkpointer


@pytest_asyncio.fixture(autouse=True)
async def _checkpointer_scoped_to_one_test():
    yield
    await close_checkpointer()
