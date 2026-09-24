"""The conversation checkpointer — where in-session memory actually lives.

Postgres, not memory and not SQLite. The point of issue #6 is that a thread
survives the process that created it, so the store has to be the same durable
database everything else in the platform already uses (see issue #3): a
`MemorySaver` would pass every test that runs in one process and lose every
conversation on the first deploy.

Memory is **in-session only**. A thread accumulates until nobody opens it
again; there is no summarisation, no cross-session retrieval, no long-term
memory. That was a deliberate scope decision on #6 — see ADR-0004.

The checkpointer is a process-wide singleton over one connection pool, built on
first use and torn down by the app's lifespan. `setup()` (which creates the
checkpoint tables) runs exactly once behind the same lock, so the tables are
never half-created under a burst of concurrent first requests.
"""

import asyncio
import sys

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from src.config import get_settings

if sys.platform == "win32":  # pragma: no cover — the deployed target is Linux
    # psycopg's async driver refuses to run on Windows' default proactor loop.
    #
    # This policy covers callers that let asyncio pick their loop: pytest-asyncio,
    # a script, `python -c`. It does **not** cover the server — uvicorn hands
    # `asyncio.run` an explicit loop factory, and a factory beats a policy. That
    # half is `src/server_loop.py`, which the Makefile and Dockerfile name via
    # `--loop`. Both are needed; neither subsumes the other.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

#: `autocommit` because `setup()` issues DDL; `prepare_threshold=0` because a
#: pooled connection may be handed a statement it has not prepared. Both are
#: what `AsyncPostgresSaver.from_conn_string` sets for its own connection.
_CONNECTION_KWARGS = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}

#: The pool type `AsyncPostgresSaver` requires. The `dict_row` factory in
#: `_CONNECTION_KWARGS` is what actually produces it; this spells it out so the
#: type checker can see the two agree.
_CheckpointerPool = AsyncConnectionPool[AsyncConnection[DictRow]]

_lock = asyncio.Lock()
_pool: _CheckpointerPool | None = None
_checkpointer: AsyncPostgresSaver | None = None


def _psycopg_dsn(database_url: str) -> str:
    """`DATABASE_URL` as psycopg3 wants it.

    SQLAlchemy accepts a driver suffix in the scheme (`postgresql+psycopg2://`);
    psycopg does not. Stripping it here means one `DATABASE_URL` keeps serving
    both the ORM and the checkpointer, which is the whole point of there being
    only one.
    """
    scheme, separator, rest = database_url.partition("://")
    return f"{scheme.partition('+')[0]}{separator}{rest}"


async def get_checkpointer() -> AsyncPostgresSaver:
    """FastAPI dependency: the process's one conversation checkpointer."""
    global _pool, _checkpointer

    if _checkpointer is not None:
        return _checkpointer

    async with _lock:
        if _checkpointer is None:
            pool: _CheckpointerPool = AsyncConnectionPool(
                conninfo=_psycopg_dsn(get_settings().database_url),
                min_size=1,
                max_size=10,
                open=False,
                kwargs=_CONNECTION_KWARGS,
            )
            await pool.open()
            checkpointer = AsyncPostgresSaver(pool)
            # Idempotent, and the reason no Alembic migration owns these tables:
            # the checkpointer's schema belongs to LangGraph, which migrates it
            # itself across versions.
            await checkpointer.setup()
            _pool, _checkpointer = pool, checkpointer
    return _checkpointer


async def close_checkpointer() -> None:
    """Drop the pool and forget the checkpointer — the app's shutdown path.

    Also what a test uses to simulate a process restart: everything in memory
    goes, and whatever a thread still knows afterwards came from Postgres.
    """
    global _pool, _checkpointer

    async with _lock:
        _checkpointer = None
        if _pool is not None:
            await _pool.close()
            _pool = None
