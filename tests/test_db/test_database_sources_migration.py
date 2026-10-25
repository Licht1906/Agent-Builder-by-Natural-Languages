"""`uc1_connections` rows become Hệ CSDL that ask for their DSN again — DS-02.

Their credentials lived in a dev-mode Vault that loses everything on restart,
so nothing can carry them over; what the migration owes a creator is the row
itself — name, owner, the views it allowed — waiting on a new DSN.
"""

import os

from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from src.config import get_settings
from tests.test_db.conftest import REPO_ROOT_ALEMBIC_INI, upgrade_to_head

#: The revision just before `database_sources` existed.
BEFORE = "b6f1e9a37c04"


def _upgrade_to(database_url: str, revision: str) -> None:
    """`upgrade_to_head`, stopping at `revision`."""
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        command.upgrade(Config(REPO_ROOT_ALEMBIC_INI), revision)
    finally:
        if previous is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()


def test_uc1_connections_are_carried_over_needing_a_new_dsn(empty_database_url: str) -> None:
    _upgrade_to(empty_database_url, BEFORE)
    engine = create_engine(empty_database_url)
    with engine.begin() as connection:
        owner = connection.execute(text("SELECT id FROM users ORDER BY id LIMIT 1")).scalar_one()
        connection.execute(
            text(
                "INSERT INTO uc1_connections (name, type, allowed_objects, vault_path, credential_ref, created_by_id) "
                "VALUES ('erp_readonly', 'postgres', '[\"v_ar_open\"]', 'conn/erp', 'vault://conn/erp', :owner), "
                "('mail', 'smtp', '[]', 'conn/mail', 'vault://conn/mail', :owner)"
            ),
            {"owner": owner},
        )

    upgrade_to_head(empty_database_url)

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT owner_id, name, dialect, dsn_secret_ref, allowed_views, last_probe_status FROM database_sources"
            )
        ).all()
    engine.dispose()

    assert [tuple(row) for row in rows] == [(owner, "erp_readonly", "POSTGRESQL", "", ["v_ar_open"], "NEEDS_DSN")]
