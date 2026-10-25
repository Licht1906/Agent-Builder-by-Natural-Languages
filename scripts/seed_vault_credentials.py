#!/usr/bin/env python3
"""Write the connection credentials UC-1's tools resolve at call time into Vault.

Normally a creator puts these there by registering a connection (`POST
/connections`, `src.api.connection_routes`), which stores the DSN in Vault and a
row in Postgres. The two halves then live in two places with very different
lifetimes, and that is the problem this script exists for: **§16.2's Vault runs
in dev mode, which keeps every secret in memory**, so a `docker compose restart
vault` silently empties it while the Postgres row — including the successful
probe that says `v_ar_open` was verified — survives untouched.

Nothing reports that as an error. `resolve_credential_via`
(`src.services.vault_secret_store`) degrades a missing secret to `""` on
purpose, so `erp.query_receivables` returns `{"rows": [], "note": "chưa có kết
nối ERP nào được cấu hình"}` and the agent, having been handed an empty result
rather than a failure, answers that it has no data. Every `correct_lookup` case
in the golden set fails that way, and none of them says "Vault".

Re-run this after any Vault restart. It is idempotent — a `put` of the same
value is a no-op from the caller's point of view — so running it when the
secrets are already present costs nothing.

Usage:
    python scripts/seed_vault_credentials.py
    make seed-vault
"""

import sys

from src.config import get_settings
from src.services.vault_secret_store import VaultUnavailableError, get_vault_store

#: `conn/erp_readonly` is the one path `src.compiler.tools` hard-codes, so it is
#: the one this script must get right; the DSN comes from `DATABASE_URL` because
#: §16.2's single-node demo serves the ERP views out of the same Postgres the
#: platform itself uses. The other two are stubs (`_send_reminder`/`_create_task`
#: only prove their pointer resolves), seeded so a rehearsal does not hit a
#: `VaultSecretNotFoundError` on the approval path.
def _credentials() -> dict[str, str]:
    settings = get_settings()
    return {
        "conn/erp_readonly": settings.database_url,
        "conn/smtp_internal": f"smtp://{settings.smtp_host}:{settings.smtp_port}",
        "conn/tasktracker": "http://tasktracker.internal/api",
    }


def main() -> int:
    vault = get_vault_store()
    try:
        for path, value in _credentials().items():
            vault.put(path, value)
            print(f"  ✓ {path}")
    except VaultUnavailableError as exc:
        print(f"Vault không truy cập được: {exc}", file=sys.stderr)
        print("Kiểm tra VAULT_URL/VAULT_TOKEN trong .env và `docker compose up -d vault`.", file=sys.stderr)
        return 1
    print("Đã ghi credentials vào Vault. Chạy lại sau mỗi lần restart Vault (dev mode không lưu bền).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
