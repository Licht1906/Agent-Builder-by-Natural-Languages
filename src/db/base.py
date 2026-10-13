"""SQLAlchemy engine, session factory, and declarative base.

One engine, built from `settings.database_url` — the same URL Alembic migrates
and the test suite resets, so app, migrations, and tests never disagree about
which database is "the" database.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import get_settings


class Base(DeclarativeBase):
    """Declarative base every ORM model inherits from."""


def get_engine() -> Engine:
    """Build the engine from the current settings. Not cached: tests reconfigure `DATABASE_URL`."""
    return create_engine(get_settings().database_url, pool_pre_ping=True)


engine = get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: a `Session` scoped to one request, closed afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
