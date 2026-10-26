"""`backend` — the fixture that runs a dialect suite once per engine (DS-06)."""

from functools import cache

import pytest

from tests.test_datasources.backends import MSSQL_URL_VARIABLE, Backend, postgres_backend, sqlserver_backend

#: Built once a run: creating the SQL Server database is a round trip a test need not repeat.
_sqlserver = cache(sqlserver_backend)


@pytest.fixture(params=["postgresql", "sqlserver"])
def backend(request: pytest.FixtureRequest) -> Backend:
    """The suite's Postgres, then the SQL Server `TEST_MSSQL_URL` names — skipped, by name, when it names none."""
    if request.param == "postgresql":
        return postgres_backend()
    found = _sqlserver()
    if found is None:
        pytest.skip(f"{MSSQL_URL_VARIABLE} is not set — no SQL Server to run the dialect suite against")
    return found
