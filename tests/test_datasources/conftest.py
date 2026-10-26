"""`backend` — the fixture that runs a dialect suite once per engine (DS-06, DS-07)."""

from functools import cache

import pytest

from tests.test_datasources.backends import (
    MSSQL_URL_VARIABLE,
    MYSQL_URL_VARIABLE,
    Backend,
    mysql_backend,
    postgres_backend,
    sqlserver_backend,
)

#: Built once a run: creating the external databases is a round trip a test need not repeat.
_EXTERNAL = {
    "sqlserver": (cache(sqlserver_backend), MSSQL_URL_VARIABLE),
    "mysql": (cache(mysql_backend), MYSQL_URL_VARIABLE),
}


@pytest.fixture(params=["postgresql", "sqlserver", "mysql"])
def backend(request: pytest.FixtureRequest) -> Backend:
    """The suite's Postgres, then each engine its `TEST_*_URL` names — skipped, by name, when it names none."""
    if request.param == "postgresql":
        return postgres_backend()
    build, variable = _EXTERNAL[request.param]
    found = build()
    if found is None:
        pytest.skip(f"{variable} is not set — no {request.param} to run the dialect suite against")
    return found
