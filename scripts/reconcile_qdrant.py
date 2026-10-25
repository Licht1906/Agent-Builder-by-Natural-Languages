#!/usr/bin/env python3
"""Find (and optionally delete) orphaned Qdrant points (RAG-09 / ADR-0044 §1).

Report-only by default — this is a manual job for a person to read, run
after anything outside the ordinary delete paths might have touched
Postgres directly (a manual `DELETE`, a migration, a bug predating this
ticket). See `src.services.reconciliation` for the full reasoning.

Usage:
    python scripts/reconcile_qdrant.py            # report only
    python scripts/reconcile_qdrant.py --apply     # report, then delete what it found
"""

import argparse

from src.db.base import SessionLocal
from src.services.qdrant import get_qdrant_client
from src.services.reconciliation import delete_orphans, find_orphans


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete the orphaned points found, instead of only reporting them")
    args = parser.parse_args()

    client = get_qdrant_client()
    db = SessionLocal()
    try:
        report = find_orphans(client, db)
    finally:
        db.close()

    if report.is_clean:
        print("No orphaned points found.")
        return

    print(f"Orphaned kb_id ({len(report.orphaned_kb_ids)}): {list(report.orphaned_kb_ids)}")
    print(f"Orphaned doc_id ({len(report.orphaned_doc_ids)}): {list(report.orphaned_doc_ids)}")

    if args.apply:
        delete_orphans(client, report)
        print("Deleted.")
    else:
        print("Run again with --apply to delete these.")


if __name__ == "__main__":
    main()
