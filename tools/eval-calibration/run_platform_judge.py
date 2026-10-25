"""Stage 1 — the platform's own one-call judge, scored over the answer ladder.

ADR-0059. Runs in the **repo's normal interpreter**, not the harness venv: it
imports `src.evals.uc1_deepeval_runner.judge_uc1_case` and calls it, rather than
reimplementing the prompt. A reimplementation would be measuring a copy of the
grader instead of the grader, which is the one thing this whole exercise exists
to avoid.

The split into two interpreters is the price of the isolation rule: `ragas` and
`deepeval` pin their own `langchain`/`pydantic`/`datasets`, and they are not
allowed anywhere near the environment that runs the backend. So the platform
judge runs here, the two libraries run in `.venv` (stage 2), and `summarize.py`
joins the two files on `fixture_id`. Nothing under `src/` imports anything in
this directory, and the directory name has a hyphen in it so it cannot be
imported as a package even by accident.

Spend is filed the way every other platform call is filed — `judge_uc1_case`
goes through `src.evals.model_call.ask`, which goes through `metered()`. That is
deliberate: `model_call.py`'s docstring records an OpenAI project hitting its
enforced limit while the cost board read $1.57, and a calibration harness that
quietly spent money off the ledger would be that incident with a different
author. The rows land under `PlatformCallPurpose.JUDGE`; §"Hệ quả" of ADR-0059
says how many.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT))

import netguard  # noqa: E402  (must be importable from the harness dir, not from src)

from src.evals.uc1_deepeval_runner import judge_uc1_case  # noqa: E402
from src.evals.uc1_golden_set import CorrectLookupCase, load_golden_set  # noqa: E402
from src.models.model_catalog import MODEL_CATALOG, usd_cost  # noqa: E402
from src.services.llm import get_chat_model_factory  # noqa: E402
from src.config import get_settings  # noqa: E402


class _CountingModel:
    """A proxy that writes down the tokens of every call passing through it.

    Sits *inside* `metered()` rather than replacing it: the ledger is the
    platform's number and stays the platform's, and this is the harness's own
    count so the results file can state a cost without querying a database. Two
    counts of one fact is normally the defect this repo names most often — here
    they are counts of two different things (what the platform bills itself vs.
    what this run spent) and §"Hoá đơn" reconciles them.
    """

    def __init__(self, inner: Any, sink: dict[str, int]) -> None:
        self._inner = inner
        self._sink = sink

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def ainvoke(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        reply = await self._inner.ainvoke(messages, *args, **kwargs)
        usage = getattr(reply, "usage_metadata", None) or {}
        self._sink["calls"] += 1
        self._sink["input_tokens"] += int(usage.get("input_tokens") or 0)
        self._sink["output_tokens"] += int(usage.get("output_tokens") or 0)
        return reply


def graded_artifact() -> dict[str, str]:
    """The exact bytes of the judge this run measured, as a git blob hash.

    Not decoration. This harness was built while another change was landing on
    `uc1_deepeval_runner.py` (the DeepEval/RAGAS naming honesty pass), and the
    file's mtime and the results file's mtime were four minutes apart — close
    enough that "which version of the prompt produced these numbers" stopped
    being answerable from the filesystem. A calibration result that cannot name
    the artifact it calibrated is an anecdote, so the artifact names itself here
    and the number goes in the results file. `git cat-file -p <hash>` gets the
    exact prompt back.
    """
    out = {}
    for name in ("src/evals/uc1_deepeval_runner.py", "src/evals/metrics.py"):
        # CRLF → LF first: these files are checked out with Windows line endings
        # but git hashes the normalised bytes, and a hash that does not match
        # `git hash-object` is a hash nobody can look up.
        raw = (REPO_ROOT / name).read_bytes().replace(b"\r\n", b"\n")
        blob = b"blob " + str(len(raw)).encode() + b"\0" + raw
        out[name] = hashlib.sha1(blob).hexdigest()
    return out


def _estimate_usd(fixtures: list[dict[str, Any]], cases: dict[str, CorrectLookupCase], model: str, repeats: int) -> tuple[int, float]:
    """Calls and dollars this run will cost, before a single one is made.

    Vietnamese runs about 2.5 characters to the token on cl100k-family
    tokenizers, and the judge prompt adds a fixed frame; both are deliberate
    over-estimates so the printed number is a ceiling rather than a hope.
    """
    prompt_frame_chars = 900
    output_tokens_per_call = 120
    calls = 0
    input_tokens = 0
    for fixture in fixtures:
        case = cases[fixture["case_id"]]
        chars = prompt_frame_chars + len(case.input) + len("".join(case.retrieval_context)) + len(case.expected_output) + len(fixture["answer"])
        input_tokens += int(chars / 2.5)
        calls += 1
    calls *= repeats
    input_tokens *= repeats
    cost = usd_cost(model, input_tokens=input_tokens, output_tokens=output_tokens_per_call * calls)
    return calls, float(cost or 0.0)


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", default=str(HARNESS_DIR / "fixtures" / "answer_ladder.jsonl"))
    parser.add_argument("--out", default=str(HARNESS_DIR / "results" / "platform_scores.jsonl"))
    parser.add_argument("--limit", type=int, default=0, help="chỉ chấm N fixture đầu (0 = tất cả)")
    parser.add_argument("--repeats", type=int, default=1, help="số lần chạy lại toàn bộ thang, để đo phương sai")
    parser.add_argument("--max-usd", type=float, default=1.50, help="trần cứng; vượt là dừng giữa chừng")
    parser.add_argument("--yes", action="store_true", help="chạy thật; không có cờ này chỉ in dự toán rồi thoát")
    args = parser.parse_args()

    net = netguard.install()

    fixtures = [json.loads(line) for line in Path(args.fixtures).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        fixtures = fixtures[: args.limit]
    cases = {case.id: case for case in load_golden_set() if isinstance(case, CorrectLookupCase)}
    missing = sorted({f["case_id"] for f in fixtures} - set(cases))
    if missing:
        print(f"FIXTURE LỖI: case_id không có trong golden set: {missing}", file=sys.stderr)
        return 2

    settings = get_settings()
    model = settings.judge_model
    if model not in MODEL_CATALOG:
        print(f"CẢNH BÁO: {model!r} không có trong MODEL_CATALOG — dự toán sẽ là 0", file=sys.stderr)

    calls, estimate = _estimate_usd(fixtures, cases, model, args.repeats)
    print("== DỰ TOÁN (stage 1: judge của platform) ==")
    print(f"  judge model            : {model}")
    print(f"  fixture × repeats      : {len(fixtures)} × {args.repeats}")
    print(f"  lời gọi model dự kiến  : {calls}  (1 lời gọi / fixture — đó là cả luận điểm)")
    print(f"  chi phí ước tính       : ${estimate:.4f}   (trần cứng ${args.max_usd:.2f})")
    if estimate > args.max_usd:
        print("DỪNG: dự toán vượt trần.", file=sys.stderr)
        return 3
    if not args.yes:
        print("Chưa có --yes nên không gọi model. Thêm --yes để chạy thật.")
        return 0

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    factory = get_chat_model_factory()
    totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

    def counting_factory(name: str) -> Any:
        return _CountingModel(factory(name), totals)

    rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    for repeat in range(1, args.repeats + 1):
        for index, fixture in enumerate(fixtures, start=1):
            case = cases[fixture["case_id"]]
            call_started = time.perf_counter()
            result = await judge_uc1_case(case, answer=fixture["answer"], model_factory=counting_factory)
            rows.append(
                {
                    "grader": "platform_one_call",
                    "repeat": repeat,
                    "fixture_id": fixture["fixture_id"],
                    "case_id": fixture["case_id"],
                    "rung": fixture["rung"],
                    "faithfulness": result.faithfulness.score,
                    "faithfulness_reason": result.faithfulness.reason,
                    "answer_relevancy": result.answer_relevancy.score,
                    "answer_relevancy_reason": result.answer_relevancy.reason,
                    "passed_at_0_7": result.passed,
                    "seconds": round(time.perf_counter() - call_started, 3),
                }
            )
            spent = float(usd_cost(model, input_tokens=totals["input_tokens"], output_tokens=totals["output_tokens"]) or 0.0)
            print(
                f"  [{repeat}/{args.repeats}] {index:>3}/{len(fixtures)} {fixture['fixture_id']:<52} "
                f"f={result.faithfulness.score:.2f} r={result.answer_relevancy.score:.2f}  ${spent:.4f}"
            )
            if spent > args.max_usd:
                print("DỪNG GIỮA CHỪNG: đã chạm trần chi phí.", file=sys.stderr)
                break

    spent = float(usd_cost(model, input_tokens=totals["input_tokens"], output_tokens=totals["output_tokens"]) or 0.0)
    Path(args.out).write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    pricing = {
        "model": model,
        "usd_per_1m_input": MODEL_CATALOG[model].usd_per_million_input_tokens if model in MODEL_CATALOG else None,
        "usd_per_1m_output": MODEL_CATALOG[model].usd_per_million_output_tokens if model in MODEL_CATALOG else None,
        "source": "src.models.model_catalog.MODEL_CATALOG — một bảng giá, một chỗ",
    }
    (Path(args.out).parent / "pricing.json").write_text(json.dumps(pricing, ensure_ascii=False, indent=2), encoding="utf-8")
    (Path(args.out).parent / "platform_spend.json").write_text(
        json.dumps(
            {
                "stage": "platform_one_call",
                "model": model,
                "estimated_usd": round(estimate, 6),
                "actual_usd": round(spent, 6),
                "calls": totals["calls"],
                "input_tokens": totals["input_tokens"],
                "output_tokens": totals["output_tokens"],
                "wall_seconds": round(time.perf_counter() - started, 1),
                "graded_artifact_git_blob_sha1": graded_artifact(),
                "hosts_resolved": net.hosts(),
                "hosts_blocked": net.blocked,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n== XONG stage 1 == {len(rows)} hàng → {args.out}")
    print(f"   dự toán ${estimate:.4f} · thực chi ${spent:.4f} · {totals['calls']} lời gọi")
    print(f"   host đã resolve: {net.hosts()}  · bị chặn: {net.blocked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
