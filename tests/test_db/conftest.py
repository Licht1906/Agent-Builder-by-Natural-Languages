"""Shared by every test in this package that runs migrations for real."""

import os
import uuid
from collections.abc import Generator

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from src.config import get_settings

REPO_ROOT_ALEMBIC_INI = "alembic.ini"


@pytest.fixture
def empty_database_url() -> Generator[str, None, None]:
    """A throwaway database with nothing in it, dropped afterwards."""
    settings = get_settings()
    name = f"migration_check_{uuid.uuid4().hex[:8]}"
    admin_engine = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))

    yield settings.database_url.rsplit("/", 1)[0] + "/" + name

    with admin_engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin_engine.dispose()


def upgrade_to_head(database_url: str) -> None:
    """Run every migration for real against `database_url`.

    `alembic/env.py` reads the URL from settings, so point settings at the
    target database for the length of the upgrade and put it back after —
    `get_settings` is `@lru_cache`d, so nothing else picks up the swap.
    """
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        command.upgrade(Config(REPO_ROOT_ALEMBIC_INI), "head")
    finally:
        if previous is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
