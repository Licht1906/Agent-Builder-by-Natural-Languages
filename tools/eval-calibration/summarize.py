"""Stage 3 — joins the two score files and answers the question the harness was built to ask.

ADR-0059 §3. Runs in the repo's normal interpreter, for `scipy` (already a
dependency via `statsmodels`, ADR-0054 §3) and for `src.models.model_catalog`,
which prices both stages' token counts off the platform's own table so there is
exactly one price of a token in this repo.

Three questions, and they are not the same question:

1. **Ranking.** Does each grader put known-good above known-bad? Spearman ρ
   between the grader's 0..1 score and the rung's intended rank. A grader that
   ranks correctly is usable even if its absolute numbers are odd.
2. **Boundary.** Where does 0.7 — `src.evals.metrics.DEFAULT_THRESHOLD`, the
   line every judge verdict in the platform is cut at — actually fall on each
   grader's scale? A grader can rank perfectly and still put its pass/fail line
   in the wrong place, and it is the line, not the ranking, that gates approval.
3. **Separation.** The platform's judge asks for both numbers in **one** call.
   Two numbers written by one completion can be correlated by the writing rather
   than by the answer, so the ladder deliberately contains two rungs where the
   two axes disagree (`unsupported_addition`: unfaithful but relevant;
   `grounded_nonanswer`: faithful but irrelevant). Whether a grader keeps them
   apart there is measurable, and it is the specific risk of a one-call judge.
"""

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scipy.stats import spearmanr  # noqa: E402

from src.evals.metrics import DEFAULT_THRESHOLD  # noqa: E402
from src.models.model_catalog import usd_cost  # noqa: E402

#: The rungs in the order a reader should see them: best first, then each way of
#: being wrong. Not alphabetical — the ordering *is* the argument.
RUNG_ORDER = (
    "grounded_full",
    "grounded_partial",
    "unsupported_addition",
    "contradicted_fact",
    "grounded_nonanswer",
    "off_topic",
)

#: Which of the six graded columns measures which axis. `platform_*` is one
#: model call producing both; the other four are four separate algorithms.
FAITHFULNESS_COLUMNS = ("platform_faithfulness", "ragas_faithfulness", "deepeval_faithfulness")
RELEVANCY_COLUMNS = ("platform_answer_relevancy", "ragas_answer_relevancy", "deepeval_answer_relevancy")


def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def join(platform: list[dict[str, Any]], library: list[dict[str, Any]], fixtures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (fixture, repeat), carrying every grader's two numbers.

    An inner join on purpose: a fixture one stage scored and the other did not
    (a run stopped at its cost cap, say) is not a comparison, and carrying it
    with blanks would let a mean be computed over a different set of answers for
    each grader.
    """
    intended = {f["fixture_id"]: f for f in fixtures}
    library_by_key = {(row["fixture_id"], row["repeat"]): row for row in library}
    joined = []
    for row in platform:
        key = (row["fixture_id"], row["repeat"])
        other = library_by_key.get(key)
        if other is None:
            continue
        meta = intended[row["fixture_id"]]
        joined.append(
            {
                "fixture_id": row["fixture_id"],
                "case_id": row["case_id"],
                "rung": row["rung"],
                "repeat": row["repeat"],
                "faithfulness_rank": meta["faithfulness_rank"],
                "relevancy_rank": meta["relevancy_rank"],
                "platform_faithfulness": row["faithfulness"],
                "platform_answer_relevancy": row["answer_relevancy"],
                "ragas_faithfulness": other["ragas_faithfulness"],
                "ragas_answer_relevancy": other["ragas_answer_relevancy"],
                "deepeval_faithfulness": other["deepeval_faithfulness"],
                "deepeval_answer_relevancy": other["deepeval_answer_relevancy"],
                "platform_faithfulness_reason": row["faithfulness_reason"],
                "platform_answer_relevancy_reason": row["answer_relevancy_reason"],
            }
        )
    return joined


def _pairs(rows: list[dict[str, Any]], column: str, rank_column: str) -> tuple[list[float], list[float]]:
    """Score/rank pairs with the NaNs dropped, and the count of what was dropped stays visible upstream."""
    scores, ranks = [], []
    for row in rows:
        if row[column] is None:
            continue
        scores.append(float(row[column]))
        ranks.append(float(row[rank_column]))
    return scores, ranks


def rank_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for column, rank_column in [(c, "faithfulness_rank") for c in FAITHFULNESS_COLUMNS] + [
        (c, "relevancy_rank") for c in RELEVANCY_COLUMNS
    ]:
        scores, ranks = _pairs(rows, column, rank_column)
        rho, p = spearmanr(ranks, scores)
        out.append(
            {
                "grader": column,
                "axis": "faithfulness" if rank_column == "faithfulness_rank" else "relevancy",
                "n": len(scores),
                "n_nan": len(rows) - len(scores),
                "spearman_rho": round(float(rho), 3),
                "p_value": float(p),
            }
        )
    return out


def rung_table(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["rung"]].append(row)
    out = []
    for rung in RUNG_ORDER:
        entry: dict[str, Any] = {"rung": rung, "n": len(grouped[rung])}
        for column in columns:
            values = [float(r[column]) for r in grouped[rung] if r[column] is not None]
            entry[column] = round(statistics.mean(values), 3) if values else None
            entry[f"{column}__pass_rate"] = (
                round(sum(1 for v in values if v >= DEFAULT_THRESHOLD) / len(values), 3) if values else None
            )
        out.append(entry)
    return out


def variance_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """How far a grader moves when asked the identical question twice.

    Reported as both a mean absolute gap and a **verdict flip rate**, because
    those are different failures: a score that wobbles 0.9→0.8 changes no
    decision, and one that wobbles 0.75→0.65 changes every decision downstream
    of it. Only the second is a reason not to trust a gate.
    """
    by_fixture: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        by_fixture[row["fixture_id"]][row["repeat"]] = row
    out = []
    for column in FAITHFULNESS_COLUMNS + RELEVANCY_COLUMNS:
        gaps, flips, pairs = [], 0, 0
        for repeats in by_fixture.values():
            if len(repeats) < 2:
                continue
            first, second = repeats[min(repeats)], repeats[max(repeats)]
            if first[column] is None or second[column] is None:
                continue
            pairs += 1
            gaps.append(abs(float(first[column]) - float(second[column])))
            if (float(first[column]) >= DEFAULT_THRESHOLD) != (float(second[column]) >= DEFAULT_THRESHOLD):
                flips += 1
        out.append(
            {
                "grader": column,
                "pairs": pairs,
                "mean_abs_gap": round(statistics.mean(gaps), 3) if gaps else None,
                "max_abs_gap": round(max(gaps), 3) if gaps else None,
                "verdict_flips": flips,
                "flip_rate": round(flips / pairs, 3) if pairs else None,
            }
        )
    return out


def separation_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Does the grader hold the two axes apart where the ladder pulls them apart?

    `gap` is (faithfulness − relevancy) on the two rungs built to make that
    difference large and of a known sign. On `unsupported_addition` the honest
    answer is negative (unfaithful, relevant); on `grounded_nonanswer` it is
    positive (faithful, irrelevant). A grader that returns ≈0 on both is
    reporting one judgement twice.
    """
    out = []
    for name, faith, rel in (
        ("platform_one_call", "platform_faithfulness", "platform_answer_relevancy"),
        ("ragas", "ragas_faithfulness", "ragas_answer_relevancy"),
        ("deepeval", "deepeval_faithfulness", "deepeval_answer_relevancy"),
    ):
        entry: dict[str, Any] = {"grader": name}
        for rung, expected_sign in (("unsupported_addition", "âm"), ("grounded_nonanswer", "dương")):
            gaps = [
                float(r[faith]) - float(r[rel])
                for r in rows
                if r["rung"] == rung and r[faith] is not None and r[rel] is not None
            ]
            entry[rung] = round(statistics.mean(gaps), 3) if gaps else None
            entry[f"{rung}__expected"] = expected_sign
        faith_scores = [float(r[faith]) for r in rows if r[faith] is not None and r[rel] is not None]
        rel_scores = [float(r[rel]) for r in rows if r[faith] is not None and r[rel] is not None]
        rho, _ = spearmanr(faith_scores, rel_scores)
        entry["rho_between_axes"] = round(float(rho), 3)
        out.append(entry)
    return out


def agreement_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for axis, columns in (("faithfulness", FAITHFULNESS_COLUMNS), ("relevancy", RELEVANCY_COLUMNS)):
        for i, left in enumerate(columns):
            for right in columns[i + 1 :]:
                left_scores = [float(r[left]) for r in rows if r[left] is not None and r[right] is not None]
                right_scores = [float(r[right]) for r in rows if r[left] is not None and r[right] is not None]
                rho, p = spearmanr(left_scores, right_scores)
                out.append(
                    {
                        "axis": axis,
                        "pair": f"{left} ~ {right}",
                        "n": len(left_scores),
                        "spearman_rho": round(float(rho), 3),
                        "p_value": float(p),
                    }
                )
    return out


def _markdown(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        lines.append("| " + " | ".join("—" if cell is None else str(cell) for cell in row) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default=str(HARNESS_DIR / "results"))
    args = parser.parse_args()
    results = Path(args.results)

    fixtures = load(HARNESS_DIR / "fixtures" / "answer_ladder.jsonl")
    platform = load(results / "platform_scores.jsonl")
    library = load(results / "library_scores.jsonl")
    rows = join(platform, library, fixtures)
    if not rows:
        print("Không có hàng nào ghép được — chạy cả hai stage trước.", file=sys.stderr)
        return 2

    platform_spend = json.loads((results / "platform_spend.json").read_text(encoding="utf-8"))
    library_spend = json.loads((results / "library_spend.json").read_text(encoding="utf-8"))
    model = platform_spend["model"]
    library_usd = float(
        usd_cost(model, input_tokens=library_spend["input_tokens"], output_tokens=library_spend["output_tokens"]) or 0.0
    )

    csv_columns = [
        "fixture_id", "case_id", "rung", "repeat", "faithfulness_rank", "relevancy_rank",
        "platform_faithfulness", "ragas_faithfulness", "deepeval_faithfulness",
        "platform_answer_relevancy", "ragas_answer_relevancy", "deepeval_answer_relevancy",
        "platform_faithfulness_reason", "platform_answer_relevancy_reason",
    ]
    with (results / "scores.csv").open("w", encoding="utf-8", newline="") as sink:
        writer = csv.DictWriter(sink, fieldnames=csv_columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    ranks = rank_table(rows)
    faith_rungs = rung_table(rows, FAITHFULNESS_COLUMNS)
    rel_rungs = rung_table(rows, RELEVANCY_COLUMNS)
    variance = variance_table(rows)
    separation = separation_table(rows)
    agreement = agreement_table(rows)

    summary = {
        "n_rows": len(rows),
        "n_fixtures": len({r["fixture_id"] for r in rows}),
        "repeats": sorted({r["repeat"] for r in rows}),
        "threshold": DEFAULT_THRESHOLD,
        "model": model,
        "spend_usd": {
            "platform_stage": platform_spend["actual_usd"],
            "library_stage": round(library_usd, 6),
            "total": round(platform_spend["actual_usd"] + library_usd, 6),
            "estimated_total": round(platform_spend["estimated_usd"] + library_spend["estimated_usd"], 6),
        },
        "calls": {"platform_stage": platform_spend["calls"], "library_stage": library_spend["calls"]},
        "hosts_resolved": sorted(set(platform_spend["hosts_resolved"]) | set(library_spend["hosts_resolved"])),
        "hosts_blocked": platform_spend["hosts_blocked"] + library_spend["hosts_blocked"],
        "telemetry_opt_outs": library_spend["telemetry_opt_outs"],
        "library_versions": library_spend["versions"],
        "rank_correlation": ranks,
        "faithfulness_by_rung": faith_rungs,
        "relevancy_by_rung": rel_rungs,
        "run_to_run_variance": variance,
        "axis_separation": separation,
        "grader_agreement": agreement,
    }
    (results / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        "# Kết quả hiệu chuẩn máy chấm — ADR-0059",
        "",
        f"Sinh bởi `tools/eval-calibration/summarize.py`. {len(rows)} hàng "
        f"= {summary['n_fixtures']} câu trả lời × {len(summary['repeats'])} lần chạy. "
        f"Ngưỡng nhị phân của platform: **{DEFAULT_THRESHOLD}** (`src/evals/metrics.py::DEFAULT_THRESHOLD`). "
        f"Cả ba máy chấm chạy trên cùng model `{model}`.",
        "",
        "## 1. Thứ hạng — máy chấm có xếp đúng tốt trên xấu không",
        "",
        "Spearman ρ giữa điểm 0..1 của máy chấm và hạng ý đồ của bậc thang.",
        "",
        _markdown(
            ["máy chấm", "trục", "n", "n NaN", "ρ", "p"],
            [[r["grader"], r["axis"], r["n"], r["n_nan"], r["spearman_rho"], f"{r['p_value']:.2e}"] for r in ranks],
        ),
        "",
        "## 2. Điểm trung bình theo bậc thang — và tỉ lệ ĐẠT ở ngưỡng 0.7",
        "",
        "### faithfulness / source_groundedness",
        "",
        _markdown(
            ["bậc", "hạng ý đồ", "n", "platform", "đạt", "ragas", "đạt", "deepeval", "đạt"],
            [
                [
                    r["rung"],
                    next(f["faithfulness_rank"] for f in fixtures if f["rung"] == r["rung"]),
                    r["n"],
                    r["platform_faithfulness"], r["platform_faithfulness__pass_rate"],
                    r["ragas_faithfulness"], r["ragas_faithfulness__pass_rate"],
                    r["deepeval_faithfulness"], r["deepeval_faithfulness__pass_rate"],
                ]
                for r in faith_rungs
            ],
        ),
        "",
        "### answer_relevancy / relevance",
        "",
        _markdown(
            ["bậc", "hạng ý đồ", "n", "platform", "đạt", "ragas", "đạt", "deepeval", "đạt"],
            [
                [
                    r["rung"],
                    next(f["relevancy_rank"] for f in fixtures if f["rung"] == r["rung"]),
                    r["n"],
                    r["platform_answer_relevancy"], r["platform_answer_relevancy__pass_rate"],
                    r["ragas_answer_relevancy"], r["ragas_answer_relevancy__pass_rate"],
                    r["deepeval_answer_relevancy"], r["deepeval_answer_relevancy__pass_rate"],
                ]
                for r in rel_rungs
            ],
        ),
        "",
        "## 3. Hai trục có tách nhau không (rủi ro riêng của judge một lời gọi)",
        "",
        "`gap` = faithfulness − answer_relevancy, trung bình trên bậc thang được dựng để hai trục lệch nhau.",
        "",
        _markdown(
            ["máy chấm", "unsupported_addition (kỳ vọng âm)", "grounded_nonanswer (kỳ vọng dương)", "ρ giữa hai trục"],
            [
                [r["grader"], r["unsupported_addition"], r["grounded_nonanswer"], r["rho_between_axes"]]
                for r in separation
            ],
        ),
        "",
        "## 4. Phương sai giữa hai lần chạy giống hệt nhau",
        "",
        _markdown(
            ["máy chấm", "cặp", "lệch tuyệt đối TB", "lệch lớn nhất", "số lần đảo phán quyết", "tỉ lệ đảo"],
            [
                [r["grader"], r["pairs"], r["mean_abs_gap"], r["max_abs_gap"], r["verdict_flips"], r["flip_rate"]]
                for r in variance
            ],
        ),
        "",
        "## 5. Ba máy chấm có đồng ý với nhau không",
        "",
        _markdown(
            ["trục", "cặp", "n", "ρ", "p"],
            [[r["axis"], r["pair"], r["n"], r["spearman_rho"], f"{r['p_value']:.2e}"] for r in agreement],
        ),
        "",
        "## 6. Hoá đơn",
        "",
        _markdown(
            ["chặng", "lời gọi", "dự toán USD", "thực chi USD"],
            [
                ["platform (1 lời gọi/câu)", platform_spend["calls"], f"{platform_spend['estimated_usd']:.4f}", f"{platform_spend['actual_usd']:.4f}"],
                ["ragas + deepeval", library_spend["calls"], f"{library_spend['estimated_usd']:.4f}", f"{library_usd:.4f}"],
                ["**tổng**", platform_spend["calls"] + library_spend["calls"], f"{summary['spend_usd']['estimated_total']:.4f}", f"{summary['spend_usd']['total']:.4f}"],
            ],
        ),
        "",
        f"Host đã resolve trong cả hai chặng: `{summary['hosts_resolved']}`. Host bị `netguard` chặn: `{summary['hosts_blocked']}`.",
        f"Opt-out telemetry đọc lại từ chính thư viện: `{summary['telemetry_opt_outs']}`.",
        f"Phiên bản thư viện: `{summary['library_versions']}`.",
        "",
    ]
    (results / "calibration.md").write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
