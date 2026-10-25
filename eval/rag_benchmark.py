"""Đo chất lượng truy xuất của node `knowledge_search` trên tài liệu thật.

Vì sao cần file này: bộ test đơn vị chứng minh từng mảnh *cư xử như chính nó* —
nó không nói gì về việc hệ thống có tìm được câu trả lời cho câu hỏi của người
dùng thật trên tài liệu thật hay không. Không có số thì mọi thay đổi với
chunking, embedding hay ranking đều là đoán.

Bản này đo **đúng một stack** đang phục vụ (`QdrantKnowledgeStore`). Trước khi
gộp có hai stack RAG song song, và file này đo cái đã bị bỏ; các con số cũ trong
`results/rag_benchmark.json` vì thế không so sánh được với các con số ở đây.

Toàn bộ đường đi là đường production, gọi lại đúng hàm của nó: bóc chữ
(`src.services.extraction`) → cắt đoạn (`src.services.chunking`) → embed
(`src.workflow.embeddings`) → Qdrant. Chỉ có hai chỗ khác: Qdrant chạy
`:memory:` để không cần dựng service, và truy vấn đi thẳng vào
`QdrantKnowledgeStore._fused_candidates` / `._reranked`, bỏ qua lớp kiểm tra
quyền sở hữu Knowledge Base — thứ cần một hàng trong database và không liên
quan gì tới chất lượng xếp hạng.

Chạy:

    python eval/rag_benchmark.py            # dense / sparse / hybrid, cần API key embedding
    python eval/rag_benchmark.py --rerank   # thêm cột có cross-encoder, cần service reranker

Nhánh `--rerank` cần `reranker` đang chạy (`docker compose up -d reranker`).
Không có nó thì `src.services.reranker` tự lùi về thứ tự RRF và **báo bằng
`reranked=False`** — script dừng thay vì in một cột trùng cột hybrid dưới một
cái tên khác.
"""

import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qdrant_client import QdrantClient, models  # noqa: E402

from src.config import get_settings  # noqa: E402
from src.services.chunking import chunk_document  # noqa: E402
from src.services.extraction import extract_document  # noqa: E402
from src.services.qdrant import (  # noqa: E402
    COLLECTION_NAME,
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    ensure_collection,
    sparse_vector_of,
    upsert_passages,
)
from src.services.reranker import RerankResult, _rerank  # noqa: E402
from src.workflow.embeddings import embedder_for  # noqa: E402
from src.workflow.knowledge import DEFAULT_PASSAGE_LIMIT, QdrantKnowledgeStore, _candidate_of_point  # noqa: E402

DOC = ROOT / "data" / "workspace" / "creators" / "3" / "quy_che_dao_tao_hust.pdf"

#: Chủ sở hữu và tủ tài liệu giả cho payload — không có database nào ở đây, hai
#: giá trị này chỉ cần khớp nhau giữa lúc ghi điểm và lúc lọc truy vấn.
OWNER_ID = "1"
KB_ID = "1"

#: (câu hỏi viết theo lối người dùng thật hỏi, chuỗi vàng phải nằm trong đoạn trả về)
#:
#: Câu hỏi cố ý **không** dùng lại từ ngữ của tài liệu — "nghỉ ốm dài ngày" chứ
#: không phải "bị ốm, thai sản" — vì một bộ đo mà câu hỏi copy nguyên văn tài
#: liệu thì chỉ đo được khả năng so khớp chuỗi.
GOLD = [
    ("học lại nhiều môn quá thì bằng có bị hạ loại không", "vượt quá 5% của tổng số tc"),
    ("em nghỉ ốm dài ngày có bị tính là chậm tiến độ không", "không vượt quá 04 học kỳ chính"),
    ("trượt bao nhiêu tín chỉ thì bị cảnh báo học tập", "không đạt trong học kỳ lớn hơn 8"),
    ("khi nào thì sinh viên bị đuổi học", "cảnh báo học tập mức 3 lần thứ hai liên tiếp"),
    ("đang bị cảnh báo thì kỳ này được đăng ký tối đa mấy tín chỉ", "tối đa 14 tc"),
    ("bao nhiêu điểm thì được điểm a", "điểm chữ quy đổi"),
    ("dưới mấy điểm thì coi là trượt môn", "dưới 3 đối với các học phần"),
    ("muốn chuyển sang ngành khác cần điều kiện gì", "cpa ≥ 2,5"),
    ("em muốn học thêm bằng thứ hai thì cần gì", "xếp trình độ năm thứ hai"),
    ("thi trượt thì được thi lại mấy lần", "không có lần thi lại"),
    ("muốn xin phúc khảo bài thi thì trong bao lâu", "thời hạn 7 ngày"),
    ("bị ốm không đi thi cuối kỳ được thì xử lý thế nào", "hoãn thi và được dự thi cuối kỳ"),
    ("một tín chỉ tương đương bao nhiêu giờ học", "50 giờ học"),
    ("một môn học có tối đa bao nhiêu tín chỉ", "từ 1 tc đến 4 tc"),
    ("nghiên cứu sinh được gia hạn làm luận án tối đa bao lâu", "gia hạn không quá 24 tháng"),
    ("tốt nghiệp muộn nhất được chậm mấy kỳ", "không được vượt quá 5 học kỳ chính"),
    ("nghỉ học nhiều buổi có bị trừ điểm không", "số lần vắng mặt"),
    ("điểm i trong bảng điểm nghĩa là gì", "điểm i: điểm học phần chưa hoàn thiện"),
    ("hai môn trùng nội dung bao nhiêu phần trăm thì được công nhận thay thế", "trùng lặp tối thiểu 70%"),
    ("học phần tiên quyết là gì", "phải hoàn thành học phần a"),
    ("bảo vệ luận văn thạc sĩ không đạt thì có được bảo vệ lại không", "bảo vệ luận văn lần thứ hai"),
    ("nộp học phí muộn thì bị làm sao", "học phí"),
]


def flat(text: str) -> str:
    """Văn bản đã chuẩn hoá để so khớp: NFC, gộp khoảng trắng, viết thường.

    Gộp khoảng trắng là bắt buộc — PDF xuống dòng giữa câu, nên một chuỗi vàng
    viết liền sẽ không bao giờ khớp với văn bản bóc ra nếu giữ nguyên newline.
    """
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).lower()


def position_of(passages, gold: str) -> int | None:
    """Vị trí (từ 0) của đoạn chứa `gold`, hoặc `None` nếu không đoạn nào chứa."""
    for position, passage in enumerate(passages):
        if gold in flat(passage.text):
            return position
    return None


def evaluate(name: str, search, *, limit: int) -> dict:
    """Chạy cả bộ câu hỏi qua một cấu hình truy xuất, trả về hit@limit và MRR."""
    rows = [
        {"question": question, "gold": gold, "position": position_of(search(question, limit), gold)}
        for question, gold in GOLD
    ]
    found = [row for row in rows if row["position"] is not None]
    return {
        "name": name,
        "hit": len(found),
        "total": len(rows),
        "mrr": round(sum(1 / (row["position"] + 1) for row in found) / len(rows), 3),
        "misses": [row["question"] for row in rows if row["position"] is None],
    }


def _identity_rerank(_query: str, documents) -> RerankResult:
    """Giữ nguyên thứ tự RRF, không ngưỡng — để đo hybrid *trước* cross-encoder."""
    return RerankResult(ranked_indices=list(range(len(documents))), scores=None, reranked=False)


def _one_sided(client: QdrantClient, using: str, query, limit: int):
    """Một nửa của hybrid: chỉ dense, hoặc chỉ sparse, cùng bộ lọc production dùng."""
    return client.query_points(
        COLLECTION_NAME,
        query=query,
        using=using,
        query_filter=models.Filter(
            must=[
                models.FieldCondition(key="kb_id", match=models.MatchAny(any=[KB_ID])),
                models.FieldCondition(key="owner_id", match=models.MatchValue(value=OWNER_ID)),
            ]
        ),
        limit=limit,
        with_payload=True,
    ).points


def main() -> int:
    settings = get_settings()
    if not DOC.is_file():
        print(f"Không tìm thấy tài liệu mẫu: {DOC}", file=sys.stderr)
        return 1

    embedder = embedder_for(
        settings.embedding_models[0],
        openai_api_key=settings.openai_api_key,
        voyage_api_key=settings.voyage_api_key,
        record_usage=lambda _model, _tokens: None,
    )
    if embedder is None:
        print("Không có API key cho model embedding đang cấu hình — không đo được.", file=sys.stderr)
        return 1

    extraction = extract_document(DOC.read_bytes(), DOC.name)
    passages = chunk_document(extraction)
    print(f"tài liệu     : {DOC.name}, bóc bằng {extraction.source}, {len(extraction.blocks)} khối")
    print(f"đoạn         : {len(passages)} (không có Section Path: "
          f"{sum(1 for passage in passages if not passage.section_path)})")

    vectors = embedder.embed_documents([passage.embeddable for passage in passages])
    client = QdrantClient(":memory:")
    ensure_collection(client)
    upsert_passages(
        client,
        [
            models.PointStruct(
                id=index,
                vector={
                    DENSE_VECTOR_NAME: list(vector),
                    SPARSE_VECTOR_NAME: sparse_vector_of(passage.embeddable),
                },
                payload={
                    "kb_id": KB_ID,
                    "owner_id": OWNER_ID,
                    "doc_id": "1",
                    "doc_name": DOC.name,
                    "section_path": passage.section_path,
                    "page_number": passage.page_number,
                    "chunk_text": passage.text,
                    "chunk_index": index,
                },
            )
            for index, (passage, vector) in enumerate(zip(passages, vectors, strict=True))
        ],
    )

    reranking = "--rerank" in sys.argv
    store = QdrantKnowledgeStore(
        db=None,  # type: ignore[arg-type]  # `_owned_kbs` is the only user, and it is bypassed below
        owner_id=int(OWNER_ID),
        qdrant_client=client,
        embedding_client=lambda texts: [list(vector) for vector in embedder.embed_documents(texts)],
        reranker_client=_rerank if reranking else _identity_rerank,
        secret_store=None,  # type: ignore[arg-type]  # BYODB only; every shelf here is managed
    )

    def dense(question: str, limit: int):
        hits = _one_sided(client, DENSE_VECTOR_NAME, list(embedder.embed_query(question)), limit)
        return [_candidate_of_point(point) for point in hits]

    def sparse(question: str, limit: int):
        hits = _one_sided(client, SPARSE_VECTOR_NAME, sparse_vector_of(question), limit)
        return [_candidate_of_point(point) for point in hits]

    def fused(question: str, limit: int):
        points = store._fused_candidates(question, [KB_ID], list(embedder.embed_query(question)))
        return store._reranked(question, [_candidate_of_point(point) for point in points], limit)

    if reranking:
        probe = _rerank("thử", ["một đoạn văn bản"])
        if not probe.reranked:
            print("Service reranker không trả lời — bỏ `--rerank` hoặc dựng nó lên trước.", file=sys.stderr)
            return 1

    limit = DEFAULT_PASSAGE_LIMIT
    results = [
        evaluate("dense (embeddings)", dense, limit=limit),
        evaluate("sparse (bag-of-words băm)", sparse, limit=limit),
        evaluate(
            "HYBRID + rerank — đường production" if reranking else "HYBRID (RRF, chưa rerank)",
            fused,
            limit=limit,
        ),
    ]

    print()
    for result in results:
        print(f"{result['name']:<45} hit@{limit} = {result['hit']}/{result['total']}   MRR = {result['mrr']}")
    print()
    for result in results:
        print(f"-- {result['name']} trượt ({len(result['misses'])}):")
        for miss in result["misses"]:
            print(f"     {miss}")

    (Path(__file__).parent / "results" / "rag_benchmark.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
