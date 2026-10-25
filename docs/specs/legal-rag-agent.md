# GitHub Issues — Legal Knowledge RAG Agent

> Tổng hợp ticket triển khai tính năng "Agent tra cứu chính sách pháp luật" (VLM extraction + RAG + generation).
> Copy từng block bên dưới thành 1 GitHub Issue riêng (đã đánh số sẵn, có label/priority gợi ý).

## Ghi chú kiến trúc: 2 chế độ tạo Knowledge Base

Hệ thống hỗ trợ **2 cách** để agent có dữ liệu tra cứu, user chọn 1 trong 2 khi tạo tủ tài liệu:

- **Managed Mode (Cách 1)** — mặc định, dành cho user đại trà. Toàn bộ pipeline (extraction → chunking → embedding → Qdrant nội bộ) do hệ thống tự vận hành, đúng logic đã thiết kế ở các issue #1–#11 bên dưới. User không cần biết gì về hạ tầng.
- **Bring-Your-Own-Database Mode / BYODB (Cách 2)** — dành cho user kỹ thuật/tổ chức đã có sẵn hạ tầng dữ liệu riêng (giống OpenAI Agent Builder). User cung cấp connection string tới vector DB của họ (Qdrant/Pinecone/Weaviate/pgvector...), hệ thống chỉ đóng vai trò **query qua**, không lưu trữ hay index hộ.

Cả 2 mode dùng chung `search_legal_knowledge` tool ở tầng agent (Issue #6) — khác nhau ở tầng adapter phía sau. Các issue mới cho BYODB: **#14, #15, #16, #17**.

---

## ISSUE #1 — [Infra] Setup Qdrant local + collection schema

**Labels:** `infra`, `p0-blocker`
**Priority:** P0

### Mô tả
Khởi tạo Qdrant chạy local (Docker) và định nghĩa schema collection dùng chung cho toàn bộ hệ thống (multi-tenant qua payload filter, không tạo collection riêng theo user/kb).

### Việc cần làm
- [ ] Chạy Qdrant qua `docker-compose` (volume mount để persist data ra ngoài container)
- [ ] Tạo 1 collection duy nhất `documents_kb` với vector size = 1024 (dimension của bge-m3), distance = Cosine
- [ ] Định nghĩa payload schema cho mỗi point:
  ```json
  {
    "kb_id": "string",
    "user_id": "string",
    "doc_id": "string",
    "doc_name": "string",
    "section_path": "string",   // VD: "Chương 3 > Điều 12 > Khoản 2"
    "article_number": "string", // VD: "Điều 12"
    "source_type": "public_law | internal_doc",
    "law_name": "string | null",
    "effective_date": "string | null",
    "status": "active | superseded | expired | null",
    "chunk_text": "string",
    "page_number": "int | null"
  }
  ```
- [ ] Tạo payload index cho `kb_id`, `user_id`, `source_type` (để filter nhanh)
- [ ] Viết script khởi tạo collection (idempotent — chạy lại không lỗi nếu đã tồn tại)
- [ ] Bật Scalar Quantization ngay từ đầu (`int8`, `always_ram: true`) theo khuyến nghị tối ưu RAM

### Acceptance Criteria
- Qdrant chạy được qua `docker-compose up`, healthcheck OK
- Collection `documents_kb` tồn tại đúng schema, verify qua Qdrant dashboard
- Insert thử 10 point mẫu + query filter theo `kb_id` trả về đúng kết quả

---

## ISSUE #2 — [Infra] Setup Embedding + Reranker service (local, GPU)

**Labels:** `infra`, `ml`, `p0-blocker`
**Priority:** P0

### Mô tả
Dựng service local chạy `bge-m3` (embedding) và `bge-reranker-v2-m3` (reranker) trên RTX 4050, expose qua HTTP API nội bộ để orchestrator gọi.

### Việc cần làm
- [ ] Chạy bge-m3 qua Ollama (`ollama pull bge-m3`) HOẶC serve qua `sentence-transformers` nếu cần control nhiều hơn
- [ ] Chạy bge-reranker-v2-m3 qua `FlagEmbedding` hoặc HuggingFace `transformers` (cross-encoder)
- [ ] Viết wrapper FastAPI endpoint:
  - `POST /embed` — input: `{texts: string[]}` → output: `{embeddings: float[][]}`
  - `POST /rerank` — input: `{query: string, documents: string[]}` → output: `{scores: float[], ranked_indices: int[]}`
- [ ] Đo VRAM usage thực tế khi chạy đồng thời cả 2 model (đảm bảo fit trong 6GB, để dành chỗ cho Qwen2.5-3B routing model nếu chạy cùng lúc)
- [ ] Thêm batch processing cho `/embed` (tránh gọi từng câu một khi ingest tài liệu dài)

### Acceptance Criteria
- `/embed` trả về vector đúng 1024-dim cho input tiếng Việt
- `/rerank` trả về thứ tự đúng (test bằng 1 query + 5 đoạn văn mẫu, so sánh thủ công)
- Benchmark latency: < 200ms cho embed 1 câu, < 500ms cho rerank 10 documents

---

## ISSUE #3 — [Pipeline] Document extraction service (Vision LLM qua OpenRouter)

**Labels:** `pipeline`, `api-integration`, `p0-blocker`
**Priority:** P0

### Mô tả
Xây module gọi Qwen2.5-VL (72B hoặc 32B) qua OpenRouter để extract nội dung từ PDF/ảnh (bao gồm scan mờ, viết tay) thành JSON có cấu trúc theo điều/khoản.

### Việc cần làm
- [ ] Setup OpenRouter API client (env var cho API key, không hardcode)
- [ ] Convert PDF → ảnh từng trang (dùng `pdf2image` hoặc `PyMuPDF`)
- [ ] Viết prompt ép JSON schema output, ví dụ:
  ```json
  {
    "sections": [
      {
        "article_number": "Điều 12",
        "title": "string",
        "content": "string",
        "sub_clauses": [
          {"clause_number": "Khoản 1", "text": "string"}
        ]
      }
    ],
    "extraction_confidence": "high | medium | low",
    "notes": "string | null"
  }
  ```
- [ ] Implement retry logic (timeout, rate limit từ OpenRouter)
- [ ] Validate JSON output (dùng Pydantic model) — nếu parse lỗi, retry với prompt nhắc lại format
- [ ] Fallback: nếu extraction_confidence = "low" trên >30% sections → flag toàn bộ file để cảnh báo user (không tự động index)
- [ ] Cache theo file hash (SHA256) — tránh gọi lại API nếu user upload trùng file

### Acceptance Criteria
- Test với ít nhất 15 file mẫu: 5 PDF text-native, 5 PDF scan rõ, 5 ảnh/scan mờ hoặc viết tay
- Tỷ lệ parse JSON thành công (không lỗi schema) ≥ 95%
- Ghi log riêng các case `extraction_confidence = low` để review thủ công

### Note kỹ thuật
So sánh song song Qwen2.5-VL-32B vs 72B trên cùng bộ test — chọn bản rẻ hơn nếu chất lượng không chênh đáng kể.

---

## ISSUE #4 — [Pipeline] Structure-aware chunking (theo Điều/Khoản)

**Labels:** `pipeline`, `nlp`, `p1`
**Priority:** P1

### Mô tả
Chunk nội dung đã extract (từ Issue #3) theo ranh giới điều/khoản thay vì fixed-size, giữ metadata phân cấp.

### Việc cần làm
- [ ] Viết rule/regex nhận diện pattern văn bản pháp luật tiếng Việt: `Điều \d+`, `Khoản \d+`, `Điểm [a-z]\)`, `Chương [IVX]+`
- [ ] Từ JSON structured (output Issue #3), map trực tiếp thành chunk theo `article_number` + `sub_clause` (không cần regex nếu extraction JSON đã có cấu trúc sẵn — regex chỉ cần cho fallback khi extraction trả về text thô)
- [ ] Mỗi chunk giữ `section_path` đầy đủ (breadcrumb) để hiển thị UI trích dẫn
- [ ] Xử lý case điều khoản quá dài (> ~500 tokens) → chia nhỏ tiếp theo câu nhưng vẫn giữ chung `article_number`
- [ ] Xử lý case điều khoản quá ngắn (< 20 tokens, VD điều chỉ có tiêu đề) → gộp với điều liền kề để tránh chunk rỗng nghĩa

### Acceptance Criteria
- Input 1 file luật mẫu (VD: Bộ luật Lao động trích đoạn) → output đúng số lượng điều khoản, không cắt ngang giữa câu
- Mỗi chunk có đầy đủ metadata: `article_number`, `section_path`, `source_type`

---

## ISSUE #5 — [Pipeline] Ingestion orchestrator (upload → index end-to-end)

**Labels:** `pipeline`, `backend`, `p0-blocker`
**Priority:** P0
**Depends on:** #1, #2, #3, #4

### Mô tả
Ghép toàn bộ luồng: nhận file upload → extraction → chunking → embedding → ghi vào Qdrant. Chạy dạng background job (không block request upload).

### Việc cần làm
- [ ] Endpoint `POST /kb/{kb_id}/documents` nhận file upload
- [ ] Đẩy job vào queue (dùng `Celery` + Redis, hoặc đơn giản hơn: `FastAPI BackgroundTasks` nếu quy mô nhỏ chưa cần queue phức tạp)
- [ ] Job pipeline: extraction (#3) → chunking (#4) → batch embed (#2) → upsert vào Qdrant (#1) với đúng `kb_id`, `user_id`
- [ ] Cập nhật trạng thái xử lý real-time: `pending → extracting → chunking → embedding → indexed → failed`
- [ ] Endpoint `GET /kb/{kb_id}/documents/{doc_id}/status` để FE poll trạng thái
- [ ] Xử lý lỗi từng bước — nếu extraction fail, không tiếp tục chunking, trả lỗi rõ ràng kèm lý do

### Acceptance Criteria
- Upload 1 file → theo dõi qua status endpoint thấy đủ các trạng thái tuần tự
- File sau khi `indexed` → query thử ngay lập tức trả về đúng chunk liên quan
- Upload file lỗi (corrupt/không đọc được) → trạng thái `failed` kèm message rõ ràng, không crash service

---

## ISSUE #6 — [Tool] `search_legal_knowledge` tool cho Agent

**Labels:** `agent`, `backend`, `p0-blocker`
**Priority:** P0
**Depends on:** #1, #2

### Mô tả
Xây orchestrator function expose ra cho agent như 1 tool duy nhất, ẩn toàn bộ chi tiết embedding/Qdrant/rerank bên trong.

### Việc cần làm
- [ ] Định nghĩa function signature:
  ```python
  def search_legal_knowledge(
      query: str,
      kb_ids: list[str],
      top_k: int = 5
  ) -> SearchResult
  ```
- [ ] Luồng bên trong: embed query (#2) → Qdrant search với filter `kb_id in kb_ids` (#1) → lấy top-20 → rerank (#2) → trả top-5 sau rerank
- [ ] `SearchResult` schema:
  ```json
  {
    "chunks": [
      {
        "text": "string",
        "source_type": "public_law | internal_doc",
        "law_name": "string | null",
        "article_number": "string",
        "section_path": "string",
        "relevance_score": "float"
      }
    ]
  }
  ```
- [ ] Đăng ký tool này vào agent framework đang dùng (function-calling schema cho gpt-oss-120b)
- [ ] Xử lý case không tìm thấy kết quả liên quan (score thấp dưới threshold) → trả rõ `chunks: []` để agent biết trả lời "không tìm thấy"

### Acceptance Criteria
- Agent gọi tool này qua function-calling thành công, nhận đúng format
- Test 5 câu hỏi mẫu, kiểm tra thủ công độ liên quan của top-5 kết quả trả về

---

## ISSUE #7 — [Agent] System prompt + generation logic (gpt-oss-120b)

**Labels:** `agent`, `prompt-engineering`, `p1`
**Priority:** P1
**Depends on:** #6

### Mô tả
Viết system prompt cho agent generation, đảm bảo bám sát context retrieve được, luôn trích dẫn nguồn, và biết báo "không chắc" khi cần.

### Việc cần làm
- [ ] Viết system prompt yêu cầu: chỉ trả lời dựa trên `chunks` nhận từ tool, luôn kèm `law_name` + `article_number` khi trích dẫn
- [ ] Set `temperature` thấp (0.1–0.3) cho generation call
- [ ] Thêm rule: nếu 2 nguồn (public_law vs internal_doc) mâu thuẫn nhau → agent phải nêu rõ cả 2, không tự chọn 1 bên
- [ ] Thêm rule: nếu `chunks` rỗng hoặc relevance_score thấp → trả lời "Không tìm thấy quy định cụ thể trong tài liệu hiện có", không bịa
- [ ] Output format ép cấu trúc: câu trả lời ngắn + danh sách citation riêng (để FE dễ render UI trích dẫn)

### Acceptance Criteria
- Test 10 câu hỏi (bao gồm 2-3 câu ngoài phạm vi corpus) → agent không hallucinate, luôn có citation hoặc từ chối rõ ràng
- Review thủ công độ chính xác trích dẫn (đúng điều/khoản) trên tập test

---

## ISSUE #8 — [Backend] "Tủ tài liệu" (Knowledge Base) CRUD + quản lý nhiều agent

**Labels:** `backend`, `api`, `p1`
**Priority:** P1
**Depends on:** #1

### Mô tả
API quản lý Knowledge Base (tủ tài liệu), quan hệ many-to-many với agent con.

### Việc cần làm
- [ ] Schema DB (Postgres/MongoDB tuỳ stack hiện có), có thêm field phân biệt mode:
  ```
  knowledge_bases: id, user_id, name, icon, color, mode ("managed" | "byodb"),
                   external_connection_id (nullable, FK → external_connections nếu mode = byodb),
                   created_at, updated_at
  documents: id, kb_id, file_name, status, uploaded_at   (chỉ áp dụng cho mode = managed)
  agent_kb_links: agent_id, kb_id   (bảng many-to-many, dùng chung cho cả 2 mode)
  ```
- [ ] Endpoint `POST /kb` — nhận thêm param `mode`. Nếu `mode = managed` → luồng như cũ (tạo tủ rỗng, chờ upload). Nếu `mode = byodb` → yêu cầu `external_connection_id` đã tồn tại (xem Issue #14), không tạo bảng `documents`
- [ ] Tạo tủ mới (gợi ý tên tự động dựa trên nội dung file đầu tiên nếu chưa đặt tên, chỉ áp dụng mode managed)
- [ ] Endpoint `GET /kb` — danh sách tủ + số tài liệu + danh sách agent đang dùng
- [ ] Endpoint `DELETE /kb/{kb_id}` — xoá tủ, kèm cascade xoá toàn bộ point trong Qdrant có `kb_id` tương ứng, cảnh báo nếu đang được ≥1 agent dùng
- [ ] Endpoint `POST /agents/{agent_id}/kb-links` — gán/gỡ tủ tài liệu cho agent con
- [ ] Check trùng lặp file khi upload (theo file hash) → gợi ý gộp vào tủ hiện có thay vì tạo tủ mới

### Acceptance Criteria
- Tạo 2 tủ, gán cùng 1 tủ cho 2 agent khác nhau, verify quan hệ many-to-many đúng
- Xoá tủ đang được dùng → có cảnh báo, sau khi confirm thì xoá sạch cả Qdrant lẫn DB

---

## ISSUE #9 — [Frontend] UX Upload tài liệu + trạng thái xử lý

**Labels:** `frontend`, `ux`, `p1`
**Priority:** P1
**Depends on:** #5, #8

### Mô tả
Giao diện upload tài liệu vào tủ, ẩn hoàn toàn khái niệm kỹ thuật (vector store, embedding...), hiển thị trạng thái xử lý thân thiện.

### Việc cần làm
- [ ] Component drag-and-drop upload, giới hạn rõ dung lượng/số trang hiển thị ngay trên UI trước khi upload
- [ ] Hiển thị trạng thái theo thời gian thực (poll `GET /kb/{kb_id}/documents/{doc_id}/status` từ Issue #5): "Đang đọc tài liệu... → Đang lập chỉ mục... → Sẵn sàng tra cứu"
- [ ] Sau khi index xong, hiển thị tóm tắt: tên file, số điều/mục detect được
- [ ] Nếu `extraction_confidence = low` (từ Issue #3) → hiển thị cảnh báo thân thiện: "Một số phần trong tài liệu khó đọc, độ chính xác trích dẫn có thể giảm"
- [ ] Danh sách tài liệu trong tủ, nút xem lại/xoá từng file
- [ ] Thông báo lỗi bằng ngôn ngữ đời thường (map lỗi kỹ thuật → message dễ hiểu), kèm gợi ý hành động (VD: "Thử chụp ảnh rõ hơn hoặc upload bản Word")

### Acceptance Criteria
- Upload file thật, theo dõi đủ các trạng thái trên UI không cần refresh
- Test case file lỗi/mờ → thấy đúng cảnh báo, không hiện lỗi kỹ thuật (stack trace, mã lỗi thô)

---

## ISSUE #10 — [Frontend] UX Màn hình quản lý "Tủ tài liệu"

**Labels:** `frontend`, `ux`, `p2`
**Priority:** P2
**Depends on:** #8

### Mô tả
Màn hình danh sách tủ tài liệu, cho phép tạo/đổi tên/xoá, xem agent nào đang dùng.

### Việc cần làm
- [ ] List view: mỗi tủ hiển thị icon/màu, tên, số tài liệu, danh sách agent đang dùng
- [ ] Modal tạo tủ mới: gợi ý tên tự động (LLM tóm tắt từ file đầu tiên), cho phép sửa tay
- [ ] Xác nhận trước khi xoá tủ đang dùng bởi ≥1 agent (liệt kê rõ agent nào sẽ bị ảnh hưởng)
- [ ] Trong luồng tạo agent con mới (liên kết với meta-agent): hiển thị chip chọn nhanh tủ có sẵn + nút "Upload tài liệu mới"

### Acceptance Criteria
- Test đầy đủ luồng: tạo tủ mới ngay trong lúc tạo agent con → gán thành công → hiển thị đúng ở màn hình danh sách

---

## ISSUE #11 — [UI] Hiển thị câu trả lời kèm trích dẫn (Citation UI)

**Labels:** `frontend`, `ux`, `p1`
**Priority:** P1
**Depends on:** #7

### Mô tả
UI hiển thị câu trả lời của agent kèm phần trích dẫn nguồn rõ ràng, phân biệt nguồn công khai vs nội bộ.

### Việc cần làm
- [ ] Câu trả lời chính hiển thị ngắn gọn ở trên
- [ ] Phần "Nguồn tham khảo" dạng expandable bên dưới: tên văn bản/tài liệu, số điều/khoản, đoạn trích gốc
- [ ] Phân biệt màu/nhãn giữa nguồn `public_law` và `internal_doc`
- [ ] Nếu agent trả lời "không tìm thấy" → hiển thị UI riêng (không phải lỗi, mà là trạng thái rõ ràng "chưa có thông tin")
- [ ] Disclaimer cố định 1 dòng nhỏ trong sidebar/header của agent pháp luật: "Thông tin mang tính tham khảo, không thay thế tư vấn pháp lý chuyên nghiệp"
- [ ] Hiển thị "Cơ sở dữ liệu cập nhật đến: [ngày]" nếu áp dụng cho corpus public law

### Acceptance Criteria
- Test với câu trả lời có 2+ nguồn khác nhau → hiển thị tách bạch, không lẫn lộn
- Test case "không tìm thấy" → UI rõ ràng, không gây hiểu nhầm là lỗi hệ thống

---

## ISSUE #12 — [Ops] Cost tracking & rate limiting cho OpenRouter API calls

**Labels:** `ops`, `cost-control`, `p2`
**Priority:** P2
**Depends on:** #3, #7

### Mô tả
Theo dõi chi phí API (extraction + generation) theo user/kb để tránh vượt ngân sách, đặc biệt vì tính năng này là phụ trợ.

### Việc cần làm
- [ ] Log mỗi lần gọi OpenRouter: model, input/output tokens, chi phí ước tính, `user_id`/`kb_id`
- [ ] Dashboard/endpoint tổng hợp chi phí theo ngày/tháng
- [ ] Set soft-limit cảnh báo (VD: gửi alert nếu chi phí tháng vượt ngưỡng đặt trước)
- [ ] Cache extraction theo file hash (nhắc lại từ Issue #3) để tránh double-charge khi user re-upload file trùng
- [ ] Rate limit số lượt upload/query theo user (tránh abuse)

### Acceptance Criteria
- Sau khi chạy test end-to-end, xem log thấy đúng chi phí ước tính khớp với pricing OpenRouter thực tế
- Trigger thử ngưỡng cảnh báo, nhận được alert đúng

---

## ISSUE #13 — [QA] Test suite tổng hợp end-to-end

**Labels:** `qa`, `testing`, `p1`
**Priority:** P1
**Depends on:** all above

### Mô tả
Bộ test end-to-end cho toàn bộ luồng: upload → index → query → trả lời có trích dẫn.

### Việc cần làm
- [ ] Chuẩn bị bộ dữ liệu test: 5 văn bản luật public, 5 tài liệu nội bộ mẫu (chính sách công ty giả lập), 5 file khó (scan mờ/viết tay)
- [ ] Test case: upload → verify status chuyển đúng thứ tự → verify chunk trong Qdrant đúng `kb_id`
- [ ] Test case: query đúng phạm vi tủ (không bị lẫn dữ liệu giữa các `kb_id` khác nhau — test bảo mật multi-tenant)
- [ ] Test case: câu hỏi mơ hồ/ngoài phạm vi → agent xử lý đúng (không hallucinate)
- [ ] Test case: 2 nguồn mâu thuẫn → agent nêu rõ cả 2
- [ ] Load test nhẹ: 50 query đồng thời, đo latency retrieval

### Acceptance Criteria
- Toàn bộ test case pass, không có leak dữ liệu giữa `kb_id`
- Latency retrieval trung bình < 1s cho mỗi query (chưa tính thời gian generation)

---

## ISSUE #14 — [Backend] External Connection management (BYODB — Cách 2)

**Labels:** `backend`, `byodb`, `p2`
**Priority:** P2
**Depends on:** #8

### Mô tả
Cho phép user kỹ thuật kết nối tới vector DB riêng của họ thay vì dùng Qdrant nội bộ (giống flow "point to your own database" của OpenAI Agent Builder), nhưng đơn giản hoá UX cho phù hợp sản phẩm.

### Việc cần làm
- [ ] Schema DB:
  ```
  external_connections: id, user_id, name, provider ("qdrant" | "pinecone" | "weaviate" | "pgvector"),
                         connection_config (JSON, đã mã hoá — xem Issue #15),
                         embedding_dimension, distance_metric,
                         status ("untested" | "connected" | "failed"),
                         last_checked_at, created_at
  ```
- [ ] Endpoint `POST /external-connections` — nhận provider + connection string/API key + tên collection/index phía user
- [ ] Endpoint `POST /external-connections/{id}/test` — thử kết nối thật (ping/list collections), trả về `connected` hoặc `failed` kèm lý do
- [ ] Endpoint `GET /external-connections` — danh sách connection của user, không trả về secret thô (mask key)
- [ ] Endpoint `DELETE /external-connections/{id}` — xoá, chặn nếu đang được ≥1 tủ (BYODB) sử dụng
- [ ] Validate bắt buộc: `embedding_dimension` user khai báo phải khớp dimension thực tế của collection (query thử 1 vector rỗng để verify) — tránh lỗi mismatch khi search

### Acceptance Criteria
- Test kết nối tới 1 Qdrant instance khác (không phải instance nội bộ) → status chuyển đúng `connected`
- Nhập sai connection string → trả lỗi rõ ràng, không crash
- Xoá connection đang dùng bởi 1 tủ → bị chặn, có message giải thích

---

## ISSUE #15 — [Security] Mã hoá & quản lý credentials cho External Connection

**Labels:** `security`, `backend`, `p1`
**Priority:** P1
**Depends on:** #14

### Mô tả
Vì BYODB yêu cầu lưu API key/connection string của bên thứ 3, đây là dữ liệu nhạy cảm bắt buộc phải xử lý an toàn — không được lưu plaintext.

### Việc cần làm
- [ ] Mã hoá `connection_config` bằng envelope encryption (VD: AES-256 với key quản lý qua KMS/Vault, hoặc tối thiểu là biến môi trường riêng cho encryption key nếu quy mô nhỏ chưa cần KMS)
- [ ] Không bao giờ log connection string/API key ra log hệ thống (rà lại toàn bộ log statement liên quan)
- [ ] Endpoint trả về connection info cho FE phải mask secret (VD: `qdrant://***:***@host`)
- [ ] Thêm cơ chế rotate/update credentials mà không cần xoá-tạo-lại connection
- [ ] Rate limit endpoint `/test` để tránh bị lợi dụng dò quét nội bộ (SSRF risk — user có thể nhập URL nội bộ để scan mạng nội bộ của server)
- [ ] Whitelist/validate URL đầu vào để chặn SSRF (chặn IP nội bộ, localhost, metadata endpoint của cloud provider)

### Acceptance Criteria
- Audit thủ công: không có secret nào xuất hiện plaintext trong DB hoặc log
- Test case SSRF (nhập `http://169.254.169.254/...` hoặc `localhost:...`) → bị chặn, không thực hiện request

---

## ISSUE #16 — [Pipeline] Adapter layer — Query external vector DB

**Labels:** `pipeline`, `backend`, `byodb`, `p2`
**Priority:** P2
**Depends on:** #6, #14

### Mô tả
Xây adapter cho phép `search_legal_knowledge` tool (Issue #6) query được cả Qdrant nội bộ (managed) lẫn vector DB bên ngoài (BYODB), ẩn sự khác biệt provider.

### Việc cần làm
- [ ] Định nghĩa interface chung:
  ```python
  class VectorDBAdapter(ABC):
      def search(self, query_vector: list[float], top_k: int, filters: dict) -> list[SearchHit]
  ```
- [ ] Implement `QdrantAdapter` (dùng lại cho managed mode, Issue #1)
- [ ] Implement thêm tối thiểu 1-2 adapter phổ biến cho BYODB: `PineconeAdapter`, `WeaviateAdapter` (ưu tiên theo nhu cầu thực tế, có thể chỉ làm Qdrant-external trước vì cùng API)
- [ ] Cập nhật `search_legal_knowledge` (Issue #6): dựa vào `kb.mode`, chọn đúng adapter — nếu `managed` dùng `QdrantAdapter` nội bộ, nếu `byodb` khởi tạo adapter tương ứng từ `external_connections`
- [ ] Xử lý trường hợp field payload phía user không khớp schema chuẩn (Issue #1) — cho phép user map field (VD: field chứa text là `content` thay vì `chunk_text`) qua cấu hình ở Issue #14
- [ ] Timeout ngắn hơn cho external call (DB ngoài có thể chậm/không ổn định) + fallback trả lỗi rõ ràng cho agent thay vì treo

### Acceptance Criteria
- Cùng 1 câu query, test chạy qua cả `managed` và `byodb` (trỏ tới Qdrant khác) đều trả kết quả đúng định dạng `SearchResult` như Issue #6
- Test case external DB timeout/down → agent nhận lỗi rõ ràng, không crash toàn hệ thống

---

## ISSUE #17 — [Frontend] UX kết nối Database riêng (Cách 2)

**Labels:** `frontend`, `ux`, `byodb`, `p2`
**Priority:** P2
**Depends on:** #14, #10

### Mô tả
Giao diện cho phép user kỹ thuật chọn "Cách 2" khi tạo tủ tài liệu, khác biệt rõ với luồng upload thông thường — nhưng vẫn giữ tinh thần dễ hiểu, không đánh đố.

### Việc cần làm
- [ ] Ở màn hình tạo tủ mới (nối tiếp Issue #10): thêm bước chọn chế độ dạng 2 thẻ lựa chọn rõ ràng:
  - **"Để hệ thống tự quản lý"** (mặc định, khuyến nghị cho hầu hết user) — mô tả ngắn: "Chỉ cần upload file, chúng tôi lo phần còn lại"
  - **"Kết nối tới database riêng của bạn"** (gắn nhãn "Nâng cao") — mô tả ngắn: "Dành cho đội kỹ thuật đã có sẵn hạ tầng dữ liệu"
- [ ] Nếu chọn Cách 2, hiển thị form: chọn provider (dropdown: Qdrant/Pinecone/Weaviate/pgvector), connection string/API key, tên collection, embedding dimension
- [ ] Nút "Kiểm tra kết nối" gọi Issue #14 `/test`, hiển thị trạng thái ngay (✓ Kết nối thành công / ✗ kèm lý do lỗi dễ hiểu, không phải raw error)
- [ ] Cảnh báo rõ trong form: dữ liệu trong DB ngoài không được hệ thống index/chỉnh sửa, user tự chịu trách nhiệm cấu trúc dữ liệu đúng schema tương thích (link tới tài liệu hướng dẫn field mapping)
- [ ] Badge nhỏ hiển thị trong danh sách tủ (Issue #10) để phân biệt tủ nào là "Tự quản lý" vs "Kết nối ngoài"

### Acceptance Criteria
- User không rành kỹ thuật vẫn hiểu được sự khác biệt giữa 2 lựa chọn qua mô tả, không cần đọc thêm tài liệu
- Luồng kết nối thành công tới 1 Qdrant test instance, hiển thị đúng trạng thái, và query thử qua agent trả kết quả đúng

---

## Gợi ý thứ tự triển khai (Milestones)

| Milestone | Issues | Mục tiêu |
|---|---|---|
| M1 — Hạ tầng nền | #1, #2 | Qdrant + embedding/rerank chạy được local |
| M2 — Pipeline ingest | #3, #4, #5 | Upload file → index thành công end-to-end |
| M3 — Agent hoạt động | #6, #7 | Query trả lời được, có trích dẫn |
| M4 — Quản lý đa tủ | #8, #10 | Nhiều tủ, nhiều agent, many-to-many |
| M5 — UX hoàn thiện | #9, #11 | Trải nghiệm upload + citation UI mượt |
| M6 — Vận hành | #12, #13 | Cost control + test coverage đầy đủ |
| M7 — BYODB (Cách 2, tùy chọn) | #14, #15, #16, #17 | User kỹ thuật tự kết nối vector DB riêng, an toàn & tách biệt Managed Mode |

> **Lưu ý:** M7 độc lập tương đối với M1–M6, có thể làm song song sau khi M3 (agent hoạt động) hoàn tất, vì cả 2 mode dùng chung tool `search_legal_knowledge` (#6). Nên ưu tiên M1–M6 trước vì đó là trải nghiệm mặc định cho đa số user.
