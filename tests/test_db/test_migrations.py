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

    # Literally one per role now, and the assertion is written against
    # `UserRole` itself rather than a hand-listed set: V2 seeds `creator`/`admin`
    # (`1135e617defc`), UC1-01 adds `agent_admin` (`d13a1d1e5b78`), and
    # `3f9c7a2e6b18` adds the `sales_rep`/`sales_manager` personas without which
    # a freshly migrated install has nobody a deployed UC-1 agent will answer —
    # `audience.roles` names those two, and the PEP refuses everyone else.
    # Deriving the expectation from the enum means adding a role to the platform
    # fails here until somebody decides whether it gets a seeded account.
    assert sorted(roles) == sorted(role.name for role in UserRole)
