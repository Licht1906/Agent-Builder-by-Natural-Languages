"""Check the V2 audit chain end to end — DS-10's command.

    PYTHONPATH=. python scripts/verify_audit_chain.py

Prints how many entries hold and, if the chain is broken, the first `seq`
where it breaks and why. Exits 1 when broken, so it can gate a job.
"""

import sys

from src.audit.chain import verify_chain
from src.db.base import SessionLocal


def main() -> int:
    db = SessionLocal()
    try:
        verdict = verify_chain(db)
    finally:
        db.close()
    if verdict.ok:
        print(f"OK    chuỗi audit nguyên vẹn — {verdict.checked} bản ghi")
        return 0
    print(
        f"FAIL  chuỗi audit đứt ở seq {verdict.broken_at}: {verdict.reason} ({verdict.checked} bản ghi trước đó còn nguyên)"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
