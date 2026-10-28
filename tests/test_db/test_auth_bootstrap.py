"""UC1-01 (#117), FR-03, run for real: the bootstrap `agent_admin` migration
`d13a1d1e5b78` seeds, proven against a database that has actually been
through `alembic upgrade head` — not the ORM-metadata schema the rest of the
suite resets from (`tests/conftest.py`'s `_fresh_postgres_schema`), which
never runs a migration file at all.
"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.auth.registration import Registration, register_user
from src.config import get_settings
from src.db.models import User, UserRole, UserStatus
from tests.test_db.conftest import upgrade_to_head


def test_migrations_seed_an_active_agent_admin(empty_database_url: str) -> None:
    upgrade_to_head(empty_database_url)
    settings = get_settings()
    engine = create_engine(empty_database_url)

    with Session(engine) as session:
        seeded = session.execute(
            select(User).where(User.email == settings.seed_agent_admin_email)
        ).scalar_one()

        assert seeded.role is UserRole.AGENT_ADMIN
        assert seeded.status is UserStatus.ACTIVE
        assert seeded.employee_id is not None
        assert seeded.job_title
        assert seeded.department

    engine.dispose()


def test_the_very_first_self_registration_after_migrating_is_still_pending(empty_database_url: str) -> None:
    """FR-03's second half: nobody self-bootstraps, not even account #1 through this endpoint."""
    upgrade_to_head(empty_database_url)
    engine = create_engine(empty_database_url)

    with Session(engine) as session:
        first_ever_signup = register_user(
            session,
            Registration(
                email="first-signup@example.com",
                password="a-perfectly-fine-password",
                role=UserRole.CREATOR,
                job_title="Nhân viên kinh doanh",
                department="SALES",
            ),
        )

        assert first_ever_signup.status is UserStatus.PENDING
        assert first_ever_signup.role is UserRole.CREATOR

    engine.dispose()
