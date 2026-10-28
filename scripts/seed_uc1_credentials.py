#!/usr/bin/env python3
"""Write the connection credentials UC-1's tools resolve at call time into the platform's `SecretStore`.

Normally a creator puts these there by registering a connection (`POST
/connections`, `src.api.connection_routes`). This seeds the three the demo's
tools name without anyone going through that form.

Since DS-20 the store is `EncryptedDbSecretStore` — Postgres — so these survive
a restart; the old reason to re-run this after every restart (a dev-mode Vault
holding secrets in memory) is gone. They do not survive `pytest`, which resets
the schema on this same database: `make seed-uc1` puts them back with the data.

Idempotent: `put` overwrites a key with the same value.

Usage:
    python scripts/seed_uc1_credentials.py
    make seed-uc1-credentials
"""

from src.config import get_settings
from src.db.base import SessionLocal
from src.services.secret_store import EncryptedDbSecretStore


#: `conn/erp_readonly` is the one key `src.compiler.tools` hard-codes, so it is
#: the one this script must get right; the DSN comes from `DATABASE_URL` because
#: §16.2's single-node demo serves the ERP views out of the same Postgres the
#: platform itself uses. The other two are stubs (`_send_reminder`/`_create_task`
#: only prove their pointer resolves).
def _credentials() -> dict[str, str]:
    settings = get_settings()
    return {
        "conn/erp_readonly": settings.database_url,
        "conn/smtp_internal": f"smtp://{settings.smtp_host}:{settings.smtp_port}",
        "conn/tasktracker": "http://tasktracker.internal/api",
    }


def main() -> int:
    with SessionLocal() as db:
        store = EncryptedDbSecretStore(db=db)
        for key, value in _credentials().items():
            store.put(key, value)
            print(f"  ✓ {key}")
    print("Đã ghi credentials vào kho secret của nền tảng.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
