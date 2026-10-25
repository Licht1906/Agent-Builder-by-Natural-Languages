"""ADR-0054 §6 (ticket EVAL-7 / #157) — import `golden/uc1_ar_v3.jsonl` into
its agent's own suite (`src.evals.uc1_suite`), the CI / air-gap route from
seed file to database that replaces "sửa golden set bằng PR".

    python scripts/import_golden_set.py --agent agt_ar_assistant --file golden/uc1_ar_v3.jsonl

Idempotent by each case's own `id`: a second run against an already-imported
suite reports every row `skipped`, never a duplicate `added` — see
`src.evals.uc1_suite.import_golden_set_into_suite`. The file itself does not
change; it stays in git as the **seed** a fresh database (CI, air-gap) is
rebuilt from, not a place a creator edits by hand any more (see the header
comment `golden/README.md` adds).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.base import SessionLocal  # noqa: E402
from src.evals.uc1_golden_set import assert_golden_set_composition, load_golden_set  # noqa: E402
from src.evals.uc1_suite import import_golden_set_into_suite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--agent", required=True, help="UC-1 agent id, vd agt_ar_assistant")
    parser.add_argument("--file", required=True, type=Path, help="đường dẫn tới file JSONL (golden set)")
    args = parser.parse_args(argv)

    cases = load_golden_set(args.file)
    assert_golden_set_composition(cases)

    db = SessionLocal()
    try:
        report = import_golden_set_into_suite(db, args.agent, cases)
    finally:
        db.close()

    print(f"{report.added} added, {report.skipped} skipped, {len(report.rows)} tổng")
    for row in report.rows:
        if row.status != "skipped":
            print(f"  {row.status}: {row.case_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
