---
status: accepted
---

# Tài liệu trong gói xuất ra

Agent có node `knowledge_search` trả lời từ các kho tài liệu của người tạo, nằm trong Qdrant của nền tảng. Gói chạy trong mạng công ty không với tới Qdrant đó. DS-18 cho gói mang theo tài liệu và tự index lại.

## Quyết định

1. **Gói mang file gốc, kèm bản trích xuất của nền tảng.**
   - `documents/<id kho>/<file>`: bytes gốc của mỗi Document trong các kho mà node `knowledge_search` tìm.
   - `documents/.extraction/<sha256>.json`: bản trích xuất mà nền tảng đã làm, đúng định dạng `FileExtractionCache`. Nhờ vậy trang scan, vốn phải dùng mô hình thị giác mới đọc được, không bị gửi ra ngoài từ trong mạng công ty. Gói không có mô hình thị giác. File scan không kèm bản trích xuất thì bị báo tên, và các file khác vẫn được index.
   - `src.agents.package_documents.write_documents` ghi thư mục này cho bước xuất gói (DS-19). Chỉ ghi kho của chủ agent. Kho `byodb` không có file để mang theo nên được báo lại.
2. **Index lúc khởi động, mỗi nội dung một lần.**
   - `prepare_knowledge` chạy trước khi mở cổng. File có hash nội dung đã nằm trong Qdrant của gói, và được embed bằng cùng model, thì giữ nguyên. File khác đi qua đúng phần trích xuất và chia đoạn của nền tảng, rồi được embed bằng `embedding.base_url` (OpenAI-compatible) của công ty.
   - Điểm (point) của file đã bị xoá hoặc thay nội dung, và mọi điểm do model khác embed, đều bị xoá. Model mới có cỡ vector khác thì collection được dựng lại.
   - Lần khởi động thứ hai chỉ gọi embedding một lần để probe: vừa kiểm endpoint còn sống, vừa biết cỡ vector.
   - Endpoint embedding hoặc Qdrant không trả lời thì gói không khởi động.
3. **Tìm kiếm giống hệt nền tảng.**
   - Dense + sparse trên một collection, Qdrant gộp bằng RRF, rerank, giới hạn số đoạn mỗi mục. Đây chính là `fused_points` / `passages_of_points` / `ranked_passages`, tách ra từ `QdrantKnowledgeStore` để nền tảng và gói dùng chung.
   - `reranker.enabled` mặc định là tắt. Khi tắt, kết quả giữ thứ tự RRF, không áp ngưỡng điểm và không ghi cảnh báo. Khi bật mà reranker chết, tìm kiếm vẫn chạy theo thứ tự RRF như trên nền tảng.
4. **Kiểm khi nạp gói.** Workflow có tìm trong tài liệu thì phải có `embedding`, `qdrant` và thư mục của từng kho; thiếu cái nào cũng được nêu cùng lúc. Node hỏi một kho mà gói không mang thì bị báo tên, không bị bỏ qua lặng lẽ.
5. **Compose của gói có Qdrant.** `deploy/package/docker-compose.yml` gồm runtime, Postgres riêng của gói, Qdrant riêng của gói, và reranker theo profile `reranker`. Bước xuất (DS-19) đưa file này vào zip.

## Consequences

- Mọi đoạn văn được gửi tới `embedding.base_url`. Giống `llm.base_url`, dữ liệu chỉ ở lại trong công ty khi endpoint này chạy trong công ty.
- Collection của gói chỉ chứa tài liệu của gói, nên không cần lọc theo `owner_id`.
- Kho `byodb` không đi theo gói. Agent dùng kho đó sẽ có node `knowledge_search` báo lỗi kèm tên kho.
