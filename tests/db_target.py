"""Point the test suite at a database of its own, before anything opens one.

`src.db.base` builds its engine at import time from `settings.database_url`, and
`conftest`'s schema fixture drops every table on that engine. Those two facts
together mean that running the test suite with the ordinary `.env` loaded
**destroys the development database** — not a risk, a certainty, and one that
looks exactly like a passing test run.

So the URL is redirected here instead, and this module is imported by `conftest`
*before* any `src` import so the redirect happens while there is still only one
place holding the URL.

Two rules, and the second is the one that matters:

1. `TEST_DATABASE_URL` wins if it is set. Anything else is derived from
   `DATABASE_URL` by suffixing the database name with `_test`, so a checkout
   that has never heard of this file still gets a separate database.
2. `require_disposable` refuses any database whose name does not end in
   `_test`. A suffix is a weak signal on its own; what makes it enough is that
   it is checked immediately before the `DROP`, so the only way to lose real
   data is to have deliberately named the real database `..._test`.
"""

import os

import sqlalchemy
from sqlalchemy.engine import make_url

#: The suffix that marks a database as this suite's to destroy.
TEST_DB_SUFFIX = "_test"


class NotADisposableDatabaseError(RuntimeError):
    """Raised instead of dropping the schema on a database that is not the suite's."""


def disposable_url(configured: str) -> str:
    """`configured` with its database name marked as the test suite's.

    Idempotent: a URL already naming a `_test` database comes back unchanged, so
    an explicitly configured `TEST_DATABASE_URL` is not mangled into
    `..._test_test` by passing through here.
    """
    url = make_url(configured)
    name = url.database or ""
    if name.endswith(TEST_DB_SUFFIX):
        return configured
    # `str(URL)` renders the password as `***`. Round-tripping through it would
    # hand back a URL that looks right in a log and cannot authenticate — which
    # surfaces as "password authentication failed" pointing at a password nobody
    # changed.
    return url.set(database=f"{name}{TEST_DB_SUFFIX}").render_as_string(hide_password=False)


def require_disposable(url: str) -> None:
    """Raise unless `url` names a database this suite is allowed to erase.

    Called immediately before the schema is dropped rather than once at startup,
    because the value that matters is the one the engine is actually bound to at
    the moment of the `DROP` — not the one that was configured earlier.
    """
    name = make_url(url).database or ""
    if not name.endswith(TEST_DB_SUFFIX):
        raise NotADisposableDatabaseError(
            f"Refusing to reset schema on database {name!r}: the test suite drops every table, "
            f"and only a database whose name ends in {TEST_DB_SUFFIX!r} is treated as disposable. "
            f"Set TEST_DATABASE_URL to a throwaway database, or leave it unset and one named "
            f"{name}{TEST_DB_SUFFIX} will be used."
        )


def ensure_database_exists(url: str) -> None:
    """Create the database `url` names if it is not there yet.

    Connects to the server's default `postgres` database to do it, because a
    database cannot be created from inside itself. `AUTOCOMMIT` because
    `CREATE DATABASE` cannot run in a transaction block.
    """
    target = make_url(url)
    admin = target.set(database="postgres")
    engine = sqlalchemy.create_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                sqlalchemy.text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target.database}
            ).scalar()
            if not exists:
                connection.execute(sqlalchemy.text(f'CREATE DATABASE "{target.database}"'))
    finally:
        engine.dispose()


def redirect_to_test_database() -> str:
    """Set `DATABASE_URL` to the suite's own database, creating it if needed.

    Returns the URL now in force. Must run before `src.db.base` is imported —
    see this module's docstring for what happens when it does not.

    The URL it derives from is read through `src.config`, not off `os.environ`.
    That is the whole reliability of this: the project configures itself from
    `.env`, so a redirect that consulted only the process environment would
    derive its test database from the *default* URL while the app — and the
    engine this suite is about to drop tables on — used the one in `.env`. It
    would look like it worked, right up until the two disagreed.

    `src.config` imports nothing that opens a connection, so importing it here
    is safe in a way importing `src.db.base` would not be.
    """
    from src.config import get_settings

    configured = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url
    url = disposable_url(configured)
    require_disposable(url)
    ensure_database_exists(url)

    # Both the variable and the cache: `Settings` reads `.env` on construction,
    # so a cached instance built a moment ago still names the real database.
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    return url


__all__ = [
    "TEST_DB_SUFFIX",
    "NotADisposableDatabaseError",
    "disposable_url",
    "ensure_database_exists",
    "redirect_to_test_database",
    "require_disposable",
]
