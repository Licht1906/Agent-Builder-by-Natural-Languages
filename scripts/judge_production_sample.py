"""Judge a sample of production runs — the ops form of the board's "Chấm mẫu judge" button.

    python scripts/judge_production_sample.py                 # up to 10 unjudged runs
    python scripts/judge_production_sample.py --limit 25
    python scripts/judge_production_sample.py --agent-key 83  # one agent only
    python scripts/judge_production_sample.py --dry-run       # list candidates, spend nothing

ADR-0053 §3, layer two. Picks production runs (`manual`, `success`, with an
answer and a trace) that no judge has seen, grades each on its version's own
judge criteria with the run's tool evidence beside the answer, and files one
binary `quality_scores` row per criterion plus `judge_pass`. Runs already
judged are never re-judged, so this is safe to schedule (cron, a deploy
hook) — every call spends only on what is new. The deck's sampling rate
(1–10 %, `src.pm.thresholds.PM_THRESHOLDS.judge_sample_rate`) is a matter of
how often and with what `--limit` this is run, not of this script.

Needs the same model key the platform runs on. Tokens are spent: one model
call per criterion per run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.base import SessionLocal  # noqa: E402
from src.pm import judge  # noqa: E402
from src.services.llm import get_chat_model_factory  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=10, help="tối đa bao nhiêu run (mặc định 10)")
    parser.add_argument("--agent-key", default=None, help="chỉ chấm run của một agent (agent_key trên bảng agent_runs)")
    parser.add_argument("--dry-run", action="store_true", help="chỉ liệt kê run sẽ chấm, không tốn token")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        if args.dry_run:
            runs = judge.candidate_runs(db, limit=args.limit, agent_key=args.agent_key)
            print(f"{len(runs)} run chưa chấm (tối đa {args.limit}):")
            for run in runs:
                criteria = judge.criteria_for(db, run)
                print(
                    f"  #{run.id}  {run.agent_name[:40]!r:42} v{run.version_key:<4} {len(criteria)} tiêu chí  {run.created_at:%Y-%m-%d %H:%M}"
                )
            return 0
        report = asyncio.run(
            judge.judge_sample(db, model_factory=get_chat_model_factory(), limit=args.limit, agent_key=args.agent_key)
        )
    finally:
        db.close()

    print(
        f"Đã chấm {len(report.judged)} run — {report.passed} đạt, {len(report.judged) - report.passed} trượt, {report.skipped} bỏ qua (không có câu trả lời/trace)."
    )
    for run in report.judged:
        flag = "ĐẠT " if run.passed else "TRƯỢT"
        print(f"  {flag} #{run.run_id}  {run.agent_name[:48]}")
        for verdict in run.verdicts:
            if not verdict.passed:
                print(f"        ✗ {verdict.category}: {verdict.reason[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
