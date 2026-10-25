"""Stage 2 — RAGAS and DeepEval scored over the same ladder, in the isolated venv.

ADR-0059 §2. Runs **only** under `tools/eval-calibration/.venv`. It never
imports `src`: it reads `golden/uc1_ar_v3.jsonl` with the standard library,
which is all a JSONL file has ever required, and takes the platform's price
table from `results/pricing.json`, which stage 1 wrote from
`src.models.model_catalog`. One price table, one place — the harness just does
not get to be the second place.

**Both libraries phone home, not just one.** The brief named DeepEval
(`us.i.posthog.com`, via `deepeval.telemetry.client`). RAGAS does it too, from
`ragas._analytics`, to `https://t.explodinggradients.com`, and its own metric
modules call `track()` on import paths this run walks. So both opt-outs are set,
both are asserted by reading the libraries' own predicates back, and the whole
process runs under `netguard`, which raises on any hostname outside the model
provider. Setting a variable is a request; the host list at the bottom of
`results/library_spend.json` is the evidence.

**Both libraries are pointed at the platform's judge model** through their own
custom-LLM hooks — `LangchainLLMWrapper` for RAGAS, a `DeepEvalBaseLLM` subclass
for DeepEval — so all three graders in this comparison ask the same weights the
same question. What they cannot be pointed at is `src.evals.model_call.ask`:
that is the second seam ADR-0054 §4 refused, and it is still refused *in the
product*. Here it is a dev script that ships to nobody, and the calls it makes
are counted by `_TokenMeter` below and reported rather than left off a bill.

RAGAS `ResponseRelevancy` additionally needs an **embedding** model — its
algorithm generates questions back from the answer and measures cosine
similarity to the real one. That is a second model in the comparison and it is
named here rather than hidden: `text-embedding-3-small`, the platform's own
first embedding model (`Settings.embedding_model`).
"""

import argparse
import asyncio
import json
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Any

# LangChain's `with_structured_output` hands pydantic a parsed model where the
# response schema declares `None`, and pydantic says so once per call. It is
# noise from a third party over a path that works; silencing it keeps the run
# log readable enough to be evidence.
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.main")

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[1]

# Before any library import: every opt-out this run relies on. `verify_opt_outs`
# reads them back out of the libraries afterwards, because an env var set in the
# wrong order is exactly the failure mode "set it and assume" produces.
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "YES"
os.environ["DEEPEVAL_DISABLE_DOTENV"] = "1"
os.environ["DEEPEVAL_DISABLE_LEGACY_KEYFILE"] = "1"
os.environ["DEEPEVAL_GRPC_LOGGING"] = "0"
os.environ["RAGAS_DO_NOT_TRACK"] = "true"
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ.pop("CONFIDENT_API_KEY", None)

sys.path.insert(0, str(HARNESS_DIR))
import netguard  # noqa: E402

#: Per-fixture LLM calls each metric makes — **measured** on a two-fixture smoke
#: run, not read off the docs. RAGAS faithfulness = statement generation + one
#: NLI pass; RAGAS response relevancy = one generation (its `strictness=3` asks
#: for three questions inside a single call, which is not what the parameter
#: name suggests and is why this table is measured); DeepEval faithfulness =
#: truths + claims + verdicts + reason; DeepEval answer relevancy = statements +
#: verdicts + reason.
#:
#: Ten calls per fixture against the platform judge's **one** is the shape of the
#: whole comparison, and it is the reason a budget line exists at all.
#: Used only to print an estimate *before* spending; the number that gets
#: reported afterwards comes from `_TokenMeter`, which counts what the provider
#: said.
EXPECTED_CALLS_PER_FIXTURE = {
    "ragas_faithfulness": 2,
    "ragas_answer_relevancy": 1,
    "deepeval_faithfulness": 4,
    "deepeval_answer_relevancy": 3,
}


def read_env_value(name: str) -> str:
    """One key out of the repo's `.env`, without importing a settings library.

    Returned, never printed and never written anywhere — the brief's rule, and
    the reason this is six lines instead of a dependency.
    """
    for line in (REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{name} không có trong .env")


class _TokenMeter:
    """Every token both libraries spend, counted at the LangChain callback seam.

    The libraries do not report usage themselves for a custom model — DeepEval's
    `evaluation_cost` is `None` unless you use one of *its* native clients, and
    RAGAS does not count at all. So the count is taken one layer down, where
    `ChatOpenAI` reports what the provider said, which is the same rule
    `src.services.platform_spend.record` follows: what the provider reported and
    nothing else.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.by_grader: dict[str, dict[str, int]] = {}
        self.current = "unattributed"

    def handler(self) -> Any:
        from langchain_core.callbacks import BaseCallbackHandler

        meter = self

        class _Handler(BaseCallbackHandler):
            def on_llm_end(self, response: Any, **kwargs: Any) -> None:
                usage = (response.llm_output or {}).get("token_usage") or {}
                if not usage:
                    generations = [g for batch in (response.generations or []) for g in batch]
                    for generation in generations:
                        message = getattr(generation, "message", None)
                        usage = getattr(message, "usage_metadata", None) or {}
                        if usage:
                            usage = {
                                "prompt_tokens": usage.get("input_tokens", 0),
                                "completion_tokens": usage.get("output_tokens", 0),
                            }
                            break
                meter.calls += 1
                bucket = meter.by_grader.setdefault(meter.current, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
                bucket["calls"] += 1
                bucket["input_tokens"] += int(usage.get("prompt_tokens") or 0)
                bucket["output_tokens"] += int(usage.get("completion_tokens") or 0)
                meter.input_tokens += int(usage.get("prompt_tokens") or 0)
                meter.output_tokens += int(usage.get("completion_tokens") or 0)

        return _Handler()


def verify_opt_outs() -> dict[str, Any]:
    """Read both libraries' own telemetry predicates back. Raises if either says "on".

    This is the difference between "we set the variable" and "the library agrees
    it is off", and it is the whole reason the function exists rather than a
    comment saying the variables are set at the top of the file.
    """
    from deepeval.config.settings import get_settings as deepeval_settings
    from deepeval.telemetry.client import telemetry_opt_out
    from ragas._analytics import do_not_track

    facts = {
        "deepeval.telemetry.client.telemetry_opt_out()": bool(telemetry_opt_out()),
        "ragas._analytics.do_not_track()": bool(do_not_track()),
        "deepeval CONFIDENT_API_KEY set": deepeval_settings().CONFIDENT_API_KEY is not None,
    }
    if not facts["deepeval.telemetry.client.telemetry_opt_out()"]:
        raise RuntimeError("DeepEval telemetry vẫn BẬT — dừng, không chạy.")
    if not facts["ragas._analytics.do_not_track()"]:
        raise RuntimeError("RAGAS analytics vẫn BẬT — dừng, không chạy.")
    if facts["deepeval CONFIDENT_API_KEY set"]:
        raise RuntimeError("CONFIDENT_API_KEY có giá trị — DeepEval sẽ đẩy kết quả lên Confident AI. Dừng.")
    return facts


def build_models(model_name: str, meter: _TokenMeter) -> tuple[Any, Any]:
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings

    key = read_env_value("OPENAI_API_KEY")
    # `timeout` and `max_retries` are not defaults worth inheriting. The first
    # full run of this harness stalled after 57 of 96 rows: two TLS connections
    # open, zero CPU for sixteen minutes, no row written. A request with no
    # deadline had gone unanswered and the whole run was blocked behind it. A
    # script that spends money must fail a call rather than hang holding one.
    chat = ChatOpenAI(
        model=model_name,
        temperature=0.0,
        api_key=key,
        timeout=90,
        max_retries=3,
        callbacks=[meter.handler()],
    )
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small", api_key=key)
    return chat, embeddings


def build_deepeval_llm(chat: Any, model_name: str) -> Any:
    """DeepEval's custom-model hook, pointed at the same `ChatOpenAI` RAGAS uses.

    `a_generate` is handed a pydantic `schema` by every metric in this run
    (`a_generate_with_schema` → `a_generate(..., schema=...)`), so the structured
    path is the real path and the string fallback below is only there for a
    metric that asks for prose.
    """
    from deepeval.models import DeepEvalBaseLLM

    class PlatformJudgeModel(DeepEvalBaseLLM):
        def __init__(self) -> None:
            self._chat = chat
            super().__init__(model=model_name)

        def load_model(self, *args: Any, **kwargs: Any) -> Any:
            return self._chat

        def get_model_name(self, *args: Any, **kwargs: Any) -> str:
            return model_name

        def generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
            if schema is not None:
                return self._chat.with_structured_output(schema).invoke(prompt)
            return self._chat.invoke(prompt).content

        async def a_generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
            if schema is not None:
                return await self._chat.with_structured_output(schema).ainvoke(prompt)
            return (await self._chat.ainvoke(prompt)).content

    return PlatformJudgeModel()


async def score_one(fixture: dict[str, Any], case: dict[str, Any], graders: dict[str, Any], meter: _TokenMeter) -> dict[str, Any]:
    from deepeval.test_case import LLMTestCase
    from ragas.dataset_schema import SingleTurnSample

    sample = SingleTurnSample(
        user_input=case["input"],
        response=fixture["answer"],
        retrieved_contexts=list(case["retrieval_context"]),
        reference=case["expected_output"],
    )
    test_case = LLMTestCase(
        input=case["input"],
        actual_output=fixture["answer"],
        retrieval_context=list(case["retrieval_context"]),
        expected_output=case["expected_output"],
    )

    row: dict[str, Any] = {"fixture_id": fixture["fixture_id"], "case_id": fixture["case_id"], "rung": fixture["rung"]}

    meter.current = "ragas_faithfulness"
    row["ragas_faithfulness"] = _finite(await graders["ragas_faithfulness"].single_turn_ascore(sample))
    meter.current = "ragas_answer_relevancy"
    row["ragas_answer_relevancy"] = _finite(await graders["ragas_answer_relevancy"].single_turn_ascore(sample))

    meter.current = "deepeval_faithfulness"
    metric = graders["deepeval_faithfulness"]
    await metric.a_measure(test_case)
    row["deepeval_faithfulness"] = _finite(metric.score)
    row["deepeval_faithfulness_reason"] = metric.reason

    meter.current = "deepeval_answer_relevancy"
    metric = graders["deepeval_answer_relevancy"]
    await metric.a_measure(test_case)
    row["deepeval_answer_relevancy"] = _finite(metric.score)
    row["deepeval_answer_relevancy_reason"] = metric.reason

    meter.current = "unattributed"
    return row


def _finite(value: Any) -> float | None:
    """`None` for a NaN, never a silent 0.0.

    RAGAS returns NaN when a metric had nothing to work with — an answer it
    could extract no statement from, for instance. Zero would read as "scored
    badly", which is a different claim entirely, and the same rule
    `platform_spend.record` applies to an unreported cost.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else round(number, 4)


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", default=str(HARNESS_DIR / "fixtures" / "answer_ladder.jsonl"))
    parser.add_argument("--golden", default=str(REPO_ROOT / "golden" / "uc1_ar_v3.jsonl"))
    parser.add_argument("--out", default=str(HARNESS_DIR / "results" / "library_scores.jsonl"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-usd", type=float, default=2.00)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="giữ các hàng đã có trong --out và chỉ chấm phần còn thiếu; mặc định là ghi đè",
    )
    args = parser.parse_args()

    net = netguard.install()
    opt_outs = verify_opt_outs()
    print("== KIỂM TELEMETRY (đọc lại từ chính thư viện) ==")
    for name, value in opt_outs.items():
        print(f"  {name}: {value}")

    pricing_path = Path(args.out).parent / "pricing.json"
    if not pricing_path.exists():
        print(f"THIẾU {pricing_path} — chạy run_platform_judge.py trước (nó ghi bảng giá từ MODEL_CATALOG).", file=sys.stderr)
        return 2
    pricing = json.loads(pricing_path.read_text(encoding="utf-8"))
    model_name = pricing["model"]
    price_in = float(pricing["usd_per_1m_input"])
    price_out = float(pricing["usd_per_1m_output"])

    fixtures = [json.loads(line) for line in Path(args.fixtures).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        fixtures = fixtures[: args.limit]
    cases = {}
    for line in Path(args.golden).read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = json.loads(line)
            if case.get("judge") == "llm":
                cases[case["id"]] = case

    calls_per_fixture = sum(EXPECTED_CALLS_PER_FIXTURE.values())
    calls = calls_per_fixture * len(fixtures) * args.repeats
    est_in_tokens = calls * 800
    est_out_tokens = calls * 200
    estimate = (est_in_tokens * price_in + est_out_tokens * price_out) / 1_000_000
    print("\n== DỰ TOÁN (stage 2: ragas + deepeval) ==")
    print(f"  model                  : {model_name} (+ text-embedding-3-small cho ragas answer_relevancy)")
    print(f"  fixture × repeats      : {len(fixtures)} × {args.repeats}")
    print(f"  lời gọi / fixture      : {calls_per_fixture}  {EXPECTED_CALLS_PER_FIXTURE}")
    print(f"  lời gọi model dự kiến  : {calls}")
    print(f"  chi phí ước tính       : ${estimate:.4f}   (trần cứng ${args.max_usd:.2f})")
    if estimate > args.max_usd:
        print("DỪNG: dự toán vượt trần.", file=sys.stderr)
        return 3
    if not args.yes:
        print("Chưa có --yes nên không gọi model. Thêm --yes để chạy thật.")
        return 0

    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, ResponseRelevancy

    meter = _TokenMeter()
    chat, embeddings = build_models(model_name, meter)
    deepeval_llm = build_deepeval_llm(chat, model_name)
    ragas_llm = LangchainLLMWrapper(chat)
    graders = {
        "ragas_faithfulness": Faithfulness(llm=ragas_llm),
        "ragas_answer_relevancy": ResponseRelevancy(llm=ragas_llm, embeddings=LangchainEmbeddingsWrapper(embeddings)),
        "deepeval_faithfulness": FaithfulnessMetric(threshold=0.7, model=deepeval_llm, async_mode=True, verbose_mode=False),
        "deepeval_answer_relevancy": AnswerRelevancyMetric(threshold=0.7, model=deepeval_llm, async_mode=True, verbose_mode=False),
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    # Resume rather than re-spend. Every row is flushed as it is scored, so a
    # killed run leaves a usable prefix; `--resume` reads that prefix back and
    # scores only what is missing. Without it the first hang would have cost the
    # whole ladder a second time.
    done: set[tuple[str, int]] = set()
    if args.resume and Path(args.out).exists():
        for line in Path(args.out).read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing = json.loads(line)
                done.add((existing["fixture_id"], existing["repeat"]))
        print(f"  --resume: bỏ qua {len(done)} hàng đã có trong {args.out}")

    rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    stopped_early = False
    with Path(args.out).open("a" if args.resume else "w", encoding="utf-8") as sink:
        for repeat in range(1, args.repeats + 1):
            for index, fixture in enumerate(fixtures, start=1):
                if (fixture["fixture_id"], repeat) in done:
                    continue
                row = await score_one(fixture, cases[fixture["case_id"]], graders, meter)
                row["repeat"] = repeat
                rows.append(row)
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                sink.flush()
                spent = (meter.input_tokens * price_in + meter.output_tokens * price_out) / 1_000_000
                print(
                    f"  [{repeat}/{args.repeats}] {index:>3}/{len(fixtures)} {fixture['fixture_id']:<52} "
                    f"rF={_fmt(row['ragas_faithfulness'])} rR={_fmt(row['ragas_answer_relevancy'])} "
                    f"dF={_fmt(row['deepeval_faithfulness'])} dR={_fmt(row['deepeval_answer_relevancy'])}  ${spent:.4f}"
                )
                if spent > args.max_usd:
                    print("DỪNG GIỮA CHỪNG: đã chạm trần chi phí.", file=sys.stderr)
                    stopped_early = True
                    break
            if stopped_early:
                break

    spend_path = Path(args.out).parent / "library_spend.json"
    # A resumed run only metered the rows it actually scored. The tokens the
    # killed run really spent are not undone by its being killed, so they are
    # carried forward rather than dropped — a bill that forgets the attempt that
    # failed is the same defect `platform_spend` exists to prevent.
    carried = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "segments": 0}
    carried_note: str | None = None
    if args.resume and spend_path.exists():
        previous = json.loads(spend_path.read_text(encoding="utf-8"))
        carried["calls"] = int(previous.get("calls") or 0)
        carried["input_tokens"] = int(previous.get("input_tokens") or 0)
        carried["output_tokens"] = int(previous.get("output_tokens") or 0)
        carried["segments"] = int(previous.get("resumed_segments") or 0) + 1
        # A note saying "these carried tokens were reconstructed, not measured"
        # must survive every later resume, or the caveat quietly disappears from
        # the bill it qualifies while the numbers it qualifies stay.
        carried_note = previous.get("reconstruction_note")

    total_input = meter.input_tokens + carried["input_tokens"]
    total_output = meter.output_tokens + carried["output_tokens"]
    spent = (total_input * price_in + total_output * price_out) / 1_000_000
    spend_path.write_text(
        json.dumps(
            {
                "stage": "ragas + deepeval",
                "model": model_name,
                "estimated_usd": round(estimate, 6),
                "actual_usd": round(spent, 6),
                "calls": meter.calls + carried["calls"],
                "input_tokens": total_input,
                "output_tokens": total_output,
                "resumed_segments": carried["segments"],
                "reconstruction_note": carried_note,
                "by_grader_this_segment": meter.by_grader,
                "embeddings_note": "text-embedding-3-small cho ragas answer_relevancy, không đếm ở đây (LangChain không phát callback cho embeddings); ~$0.00002/fixture ở giá $0.02/1M token",
                "wall_seconds": round(time.perf_counter() - started, 1),
                "stopped_early": stopped_early,
                "telemetry_opt_outs": opt_outs,
                "hosts_resolved": net.hosts(),
                "hosts_blocked": net.blocked,
                "versions": _versions(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n== XONG stage 2 == {len(rows)} hàng → {args.out}")
    print(f"   dự toán ${estimate:.4f} · thực chi ${spent:.4f} · {meter.calls} lời gọi")
    print(f"   host đã resolve: {net.hosts()}  · bị chặn: {net.blocked}")
    return 0


def _fmt(value: float | None) -> str:
    return " nan" if value is None else f"{value:.2f}"


def _versions() -> dict[str, str]:
    import importlib.metadata as metadata

    names = ("ragas", "deepeval", "langchain-core", "langchain-openai", "langchain-community", "openai", "pydantic")
    return {name: metadata.version(name) for name in names}


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
