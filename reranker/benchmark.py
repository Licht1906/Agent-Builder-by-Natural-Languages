#!/usr/bin/env python3
"""Measure `/rerank` latency for 20 documents (RAG-03 acceptance).

Run this against a live reranker — once with the service on its normal
(GPU, if present) device, once with the service started under
`CUDA_VISIBLE_DEVICES=""` — and record both numbers verbatim; ADR-0044 §2's
budget section and RAG-10's `top_k` choice both read off these figures.
This script only exercises the HTTP surface, so it does not care which
device the service actually loaded onto — that's what makes the two runs
comparable.

Usage:
    python reranker/benchmark.py [--url http://localhost:8001] [--n 20] [--repeats 5]
"""

import argparse
import statistics
import time

import httpx

_SAMPLE_DOCUMENTS = [
    "Người lao động được nghỉ 12 ngày phép năm nếu làm việc đủ 12 tháng.",
    "Công ty không được đơn phương chấm dứt hợp đồng khi nhân viên đang mang thai.",
    "Điều 12 quy định về thời giờ làm việc bình thường không quá 8 giờ một ngày.",
    "Mức lương tối thiểu vùng được điều chỉnh hằng năm theo quy định của Chính phủ.",
    "Người sử dụng lao động phải đóng bảo hiểm xã hội cho người lao động.",
    "Hợp đồng lao động phải được giao kết bằng văn bản, trừ một số trường hợp.",
    "Thời gian thử việc không được vượt quá 60 ngày đối với công việc yêu cầu trình độ cao.",
    "Người lao động nữ được nghỉ thai sản 6 tháng trước và sau khi sinh con.",
    "Doanh nghiệp phải thông báo trước 45 ngày khi đơn phương chấm dứt hợp đồng.",
    "Làm thêm giờ không được vượt quá 40 giờ trong một tháng.",
    "Điều 21 quy định về nội dung chủ yếu của hợp đồng lao động.",
    "Người lao động có quyền đơn phương chấm dứt hợp đồng khi bị ngược đãi.",
    "Kỷ luật sa thải chỉ áp dụng trong các trường hợp pháp luật quy định.",
    "Thời gian nghỉ giữa giờ làm việc ít nhất 30 phút liên tục.",
    "Người lao động chưa thành niên không được làm công việc nặng nhọc, độc hại.",
    "Chế độ trợ cấp thôi việc được tính theo thời gian làm việc thực tế.",
    "Công đoàn có quyền đại diện bảo vệ quyền lợi hợp pháp của người lao động.",
    "An toàn lao động là trách nhiệm bắt buộc của cả người lao động và người sử dụng lao động.",
    "Việc xử lý kỷ luật lao động phải có sự tham gia của tổ chức đại diện người lao động.",
    "Người lao động được tạm hoãn thực hiện hợp đồng lao động trong một số trường hợp.",
]


def _run(url: str, n: int, repeats: int, warmup: int) -> list[float]:
    documents = (_SAMPLE_DOCUMENTS * ((n // len(_SAMPLE_DOCUMENTS)) + 1))[:n]
    payload = {"query": "công ty có được cho nghỉ việc khi nhân viên đang mang thai không?", "documents": documents}

    latencies = []
    with httpx.Client(timeout=60.0) as client:
        # Untimed. The first request after startup pays for work that never
        # recurs — on CUDA, context creation and kernel autotuning — and
        # charging it to the GPU run but not the CPU one would flatter
        # exactly the comparison this script exists to make. `/health` is
        # already green by then, so this is the only thing standing between
        # a loaded model and a warm one.
        for _ in range(warmup):
            client.post(f"{url}/rerank", json=payload).raise_for_status()

        for _ in range(repeats):
            start = time.perf_counter()
            response = client.post(f"{url}/rerank", json=payload)
            response.raise_for_status()
            latencies.append(time.perf_counter() - start)
    return latencies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8001")
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1, help="untimed requests before measuring")
    args = parser.parse_args()

    health = httpx.get(f"{args.url}/health", timeout=10.0).json()
    print(f"device={health.get('device')} model_loaded={health.get('model_loaded')}")

    latencies_ms = [s * 1000 for s in _run(args.url, args.n, args.repeats, args.warmup)]
    print(f"n_documents={args.n} repeats={args.repeats} warmup={args.warmup}")
    print(
        f"latency_ms: mean={statistics.mean(latencies_ms):.1f} "
        f"median={statistics.median(latencies_ms):.1f} "
        f"min={min(latencies_ms):.1f} max={max(latencies_ms):.1f}"
    )


if __name__ == "__main__":
    main()
