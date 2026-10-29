#!/usr/bin/env python3
"""Pre-flight for the eight-minute demo — run it 30 minutes before going on stage (DS-24).

    ERP_MAU_DSN=postgresql://erp:erp@localhost:5433/erp_mau \
    DEMO_BACKEND_URL=http://localhost:8000 \
    DEMO_MAILHOG_URL=http://localhost:8025 \
    python scripts/demo_preflight.py

Checks what `docs/uc1-demo-runbook.md` §Trước giờ diễn otherwise asks a person
to eyeball, and that a person cannot fix on stage:

- "ERP mẫu" holds today's numbers — the counts beat 4 shows (3 / 15) are
  counted back from the day the seed ran, so yesterday's seed is wrong today;
- the backend answers `/health`;
- the fallback mailbox (MailHog) answers, when `DEMO_MAILHOG_URL` is set.

What it cannot check is named in the runbook instead: the Gmail connection
(an OAuth grant on the presenter's own account) and the LLM's latency.

Prints one line per check and exits 1 if any failed. Like the seed, it never
imports the platform — only `psycopg` and the standard library.
"""

import os
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from seed_erp_mau import DEFAULT_DSN, EXPECTED_OVERDUE, overdue_counts  # noqa: E402

DEFAULT_BACKEND = "http://localhost:8000"


def check_erp(dsn: str) -> str | None:
    """`None` when every demo asker's overdue count is today's; otherwise what is wrong."""
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            counts = overdue_counts(conn)
    except psycopg.Error as error:
        return f"không kết nối được ERP mẫu ({error.__class__.__name__}) — chạy `make up` hoặc job seed"
    wrong = [
        f"{code} có {counts.get(code)}, cần {want}"
        for code, want in EXPECTED_OVERDUE.items()
        if counts.get(code) != want
    ]
    return ("số liệu lệch — seed lại: " + "; ".join(wrong)) if wrong else None


def check_url(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 — an operator-given http(s) URL
            return None if response.status == 200 else f"{url} trả {response.status}"
    except OSError as error:
        return f"{url} không trả lời ({error})"


def main() -> int:
    checks: list[tuple[str, Callable[[], str | None]]] = [
        ("ERP mẫu có số liệu của hôm nay", lambda: check_erp(os.environ.get("ERP_MAU_DSN") or DEFAULT_DSN)),
        (
            "Backend trả lời /health",
            lambda: check_url((os.environ.get("DEMO_BACKEND_URL") or DEFAULT_BACKEND) + "/health"),
        ),
    ]
    mailhog = os.environ.get("DEMO_MAILHOG_URL")
    if mailhog:
        checks.append(
            ("Hộp thư dự phòng (MailHog) trả lời", lambda: check_url(mailhog.rstrip("/") + "/api/v2/messages"))
        )

    failed = 0
    for label, check in checks:
        problem = check()
        failed += problem is not None
        print(f"{'OK  ' if problem is None else 'LỖI '} {label}" + (f": {problem}" if problem else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
