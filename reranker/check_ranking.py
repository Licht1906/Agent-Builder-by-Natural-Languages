#!/usr/bin/env python3
"""Eyeball `/rerank`'s ordering on Vietnamese prose (RAG-03 acceptance).

The ticket asks for "1 query + 5 đoạn văn tiếng Việt → thứ tự đúng khi so
bằng mắt". That is a judgement a person makes, not an assertion a test can
make for them — so this prints the ranking rather than passing or failing,
and the passages are chosen so a wrong order is obvious at a glance.

The query is ADR-0044's own paraphrase example: it shares almost no surface
vocabulary with the passages that actually answer it, so lexical overlap
ranks the decoy (P5) near the top and only a cross-encoder sinks it. That is
the whole point of the service — if this ordering looks right, reranking is
doing something retrieval alone did not.

What it does on the reference machine is recorded in ADR-0044 §2: P5 lands
fourth at +0.003, two orders of magnitude below the two provisions about
dismissing a pregnant employee. Note that it separates those top two by a
margin thinner than a reader would, so read this as "the right passages
reached the top", not as a ranking to trust at position 1 alone.

Usage:
    python reranker/check_ranking.py [--url http://localhost:8001]
"""

import argparse

import httpx

_QUERY = "công ty có được cho nghỉ việc khi nhân viên đang mang thai không?"

#: Ordered by how well each answers `_QUERY`, best first, purely so a reader
#: can compare intent against the printed ranking. The service is handed them
#: in label order below, so the input order carries no hint.
_PASSAGES_BEST_FIRST = [
    (
        "P2",
        "Người sử dụng lao động không được sa thải hoặc đơn phương chấm dứt hợp đồng lao "
        "động đối với người lao động nữ vì lý do kết hôn, mang thai, nghỉ thai sản hoặc "
        "nuôi con dưới 12 tháng tuổi.",
    ),
    (
        "P4",
        "Lao động nữ mang thai có quyền đơn phương chấm dứt hợp đồng lao động nếu có xác "
        "nhận của cơ sở khám bệnh, chữa bệnh rằng việc tiếp tục làm việc sẽ ảnh hưởng "
        "xấu tới thai nhi.",
    ),
    (
        "P1",
        "Lao động nữ được nghỉ thai sản trước và sau khi sinh con là 06 tháng; thời gian "
        "nghỉ trước khi sinh không quá 02 tháng.",
    ),
    (
        "P5",
        # The decoy: "công ty", "chấm dứt hợp đồng" and "người lao động" all
        # come straight from the query's surface, but it is about a different
        # situation entirely.
        "Công ty có quyền đơn phương chấm dứt hợp đồng lao động khi người lao động thường "
        "xuyên không hoàn thành công việc theo hợp đồng đã giao kết.",
    ),
    (
        "P3",
        "Thời giờ làm việc bình thường không quá 08 giờ trong một ngày và không quá 48 giờ trong một tuần.",
    ),
]

#: What the service sees: P1..P5 in label order, which is not the answer
#: order above. Sorted rather than randomised so two runs are comparable —
#: a ranking that changed between runs should mean the model changed, not
#: that the input did.
_BY_LABEL = sorted(_PASSAGES_BEST_FIRST, key=lambda item: item[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8001")
    args = parser.parse_args()

    health = httpx.get(f"{args.url}/health", timeout=10.0).json()
    print(f"device={health.get('device')} model_loaded={health.get('model_loaded')}\n")
    print(f"query: {_QUERY}\n")

    labels = [label for label, _ in _BY_LABEL]
    documents = [text for _, text in _BY_LABEL]
    response = httpx.post(
        f"{args.url}/rerank",
        json={"query": _QUERY, "documents": documents},
        timeout=60.0,
    )
    response.raise_for_status()
    body = response.json()

    print("ranking (best first):")
    for rank_position, index in enumerate(body["ranked_indices"], start=1):
        text = documents[index][:78]
        print(f"  {rank_position}. {labels[index]}  score={body['scores'][index]:+.4f}  {text}…")

    expected = [label for label, _ in _PASSAGES_BEST_FIRST]
    actual = [labels[index] for index in body["ranked_indices"]]
    print(f"\nexpected (by hand): {' > '.join(expected)}")
    print(f"actual:             {' > '.join(actual)}")
    print("\nJudge it yourself — exact agreement is not required, but the passage that")
    print("answers the question must beat the decoy that merely repeats its words.")


if __name__ == "__main__":
    main()
