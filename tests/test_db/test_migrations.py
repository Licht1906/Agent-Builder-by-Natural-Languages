"""The migrations, run for real against an empty database.

The rest of the suite builds its schema from `Base.metadata`, which never touches
a migration file. That leaves the seeded-accounts criterion (#4) resting on code
no test executes — and the seed writes enum *names* (`CREATOR`) where the ORM
would have written them for us, exactly the kind of detail that breaks in
production while the suite stays green.
"""

from sqlalchemy import create_engine, text

from src.db.models import UserRole
from tests.test_db.conftest import upgrade_to_head


def test_migrations_seed_one_account_per_role(empty_database_url: str) -> None:
    upgrade_to_head(empty_database_url)

    engine = create_engine(empty_database_url)
    with engine.connect() as connection:
        roles = connection.execute(text("SELECT role FROM users")).scalars().all()
    engine.dispose()

    # One seeded account per role, written against `UserRole` itself rather than
    # a hand-listed set: V2 seeds `creator`/`admin` (`1135e617defc`), UC1-01 adds
    # `agent_admin` (`d13a1d1e5b78`), ADR-0051 adds `pm`. The two UC-1 audience
    # personas `3f9c7a2e6b18` seeded are `creator`s since DS-21 (`a7d2c4e9f1b3`).
    # Deriving the expectation from the enum means adding a role to the platform
    # fails here until somebody decides whether it gets a seeded account.
    assert sorted(set(roles)) == sorted(role.name for role in UserRole)
    assert roles.count("CREATOR") == 3, "the V2 creator plus the two former sales personas"
