# KẾ HOẠCH & BÁO CÁO TIẾN ĐỘ DỰ ÁN (8 TUẦN)
## Đề tài: Agent Builder by Natural Languages – Trợ lý dựng Agent mới từ mô tả ngôn ngữ tự nhiên

- **Thời gian thực hiện:** 21/09/2026 – 15/11/2026 (8 Tuần)
- **Mục tiêu:** Xây dựng nền tảng cho phép người dùng mô tả bài toán bằng tiếng Việt tự nhiên; hệ thống tự phỏng vấn làm rõ, sinh cấu hình LangGraph, chạy thử sandbox, tự động đánh giá (eval), kiểm soát phê duyệt (HITL) và triển khai production.
- **Nguồn tài liệu đối chiếu thực tế:** 70 Architecture Decision Records (`docs/adr/0001` → `0068`), Đặc tả kỹ thuật (`docs/specs/`), Quy trình làm việc (`docs/WORKFLOW.md`), và mã nguồn triển khai (`src/`, `frontend/`, `alembic/`).

---

## I. TỔNG QUAN LỘ TRÌNH 8 TUẦN (BẮT ĐẦU TỪ 21/09/2026)

| Tuần | Thời gian | Nội dung trọng tâm | ADR thực hiện | Số commit |
|:---:|:---:|---|:---:|:---:|
| **Tuần 1** | 21/09 – 27/09/2026 | Hạ tầng Docker Postgres pgvector, Hợp đồng `AgentSpec`, Auth RBAC & Runtime V1 | ADR 0001 – 0008 (8) | 9 |
| **Tuần 2** | 28/09 – 04/10/2026 | Langfuse tracing, Quản trị Admin, Meta-Agent V1 Slot-Filling & Frontend Wizard | ADR 0009 – 0019 (11) | 10 |
| **Tuần 3** | 05/10 – 11/10/2026 | Kiến trúc V2: Workflow Graph (DAG), Template Catalog, Canvas React Flow | ADR 0020 – 0028 (9) | 10 |
| **Tuần 4** | 12/10 – 18/10/2026 | LangGraph Runtime V2, HITL Pause/Resume trên Postgres Checkpointer & Sandbox Trace | ADR 0029 – 0037 (9) | 10 |
| **Tuần 5** | 19/10 – 25/10/2026 | Hệ sinh thái Tool (MCP, Web Search) & Hybrid RAG Knowledge Base (Qdrant + BM25) | ADR 0038 – 0045 (9) | 10 |
| **Tuần 6** | 26/10 – 01/11/2026 | Đồng nhất kiến trúc UC-1, Meta-Agent đàm thoại tự chẩn đoán & PM Control Room | ADR 0046 – 0053 (8) | 9 |
| **Tuần 7** | 02/11 – 08/11/2026 | Khung Đánh giá Toàn diện: Lưới tình huống (Scenario Grid) & Calibrated LLM Judge | ADR 0054 – 0062 (9) | 9 |
| **Tuần 8** | 09/11 – 15/11/2026 | Tinh chỉnh xử lý biên (`AsksBack`, trần hỏi, query extraction), Benchmark & Nghiệm thu | ADR 0063 – 0068 (7) | 8 |

---

## II. BÁO CÁO TIẾN ĐỘ HÀNG TUẦN

---

### Tuần 1 (21/09 – 27/09/2026)

Em xin gửi báo cáo tuần 1 ạ

**Đề tài:** Agent Builder by Natural Languages – Trợ lý dựng Agent mới từ mô tả ngôn ngữ tự nhiên

**1.Đã làm**
- Dựng môi trường phát triển: cấu hình `pyproject.toml`, Docker Compose chạy PostgreSQL 16 với pgvector extension; khởi tạo migration Alembic đầu tiên cho database quan hệ (`alembic.ini`, `alembic/`).
- Xây schema `AgentSpec` và bộ validator kiểm soát 5 luật bắt buộc: model allowlist, tool registry membership, cấm gán model tier rẻ trên bước có tool, `http_request` phải khai báo domains, và trần iterations/budget (ADR-0001).
- Cài đặt xác thực JWT với hai vai trò tách biệt: `agent_creator` (tạo và sửa agent của mình) và `admin` (duyệt và cấu hình hệ thống) (ADR-0002).
- Xây dựng runtime interpreter V1 thi hành `AgentSpec` tuần tự từng bước, hỗ trợ lưu bộ nhớ hội thoại phiên chat `SessionMemory` (ADR-0003, ADR-0004).
- Cài đặt vòng đời version bất biến (`draft` -> `approved` -> `deployed`) và cơ chế dời con trỏ để rollback an toàn; dựng sandbox chạy thử mô phỏng các tool có side effect và chặn trần ngân sách (ADR-0005, ADR-0007, ADR-0008).

**2.Vấn đề gặp phải**
- Khi validate spec, model allowlist thay đổi động theo cài đặt admin trong database nhưng model catalog lại cố định trong code; đã truyền allowlist qua validation context của Pydantic và chỉ cho phép admin thu hẹp danh mục model, không cho phép mở rộng ngoài code (ADR-0001).
- Ảnh Docker Postgres tiêu chuẩn không có sẵn extension vector khiến migration bị lỗi; đã chuyển base image sang `pgvector/pgvector:pg16` trong `docker-compose.yml`.
- Môi trường sandbox chạy thử nếu gọi trực tiếp các tool ghi dữ liệu thật (như gửi email, HTTP write) sẽ gây tác dụng phụ; đã tách tool thành 2 chế độ (chạy thật cho tool đọc dữ liệu, mock cho tool có tác dụng phụ) (ADR-0007).

**3.Kế hoạch tuần sau**
- Tích hợp Langfuse telemetry, đo đếm 3 con số chi phí (prompt token, completion token, latency) và lưu trace từng lượt chạy.
- Xây dựng màn hình cấu hình admin (bảng settings) cho allowlist model và ngưỡng eval.
- Phát triển Meta-Agent phỏng vấn slot-filling bằng tiếng Việt, tự động sinh `AgentSpec` từ thông tin thu thập được và hỗ trợ tinh chỉnh qua chat.
- Dựng frontend Wizard Next.js (màn hình mô tả, phỏng vấn, xem trước spec và sơ đồ tĩnh).

---

### Tuần 2 (28/09 – 04/10/2026)

Em xin gửi báo cáo tuần 2 ạ

**Đề tài:** Agent Builder by Natural Languages – Trợ lý dựng Agent mới từ mô tả ngôn ngữ tự nhiên

**1.Đã làm**
- Tích hợp Langfuse theo dõi lượt chạy, tính toán chi phí thực tế cho từng bước và từng model call (ADR-0009).
- Xây dựng bảng `settings` cho admin quản lý allowlist model và ngưỡng đỗ eval tối thiểu; dựng hàng chờ phê duyệt `approval_queue` và bảng audit log bất biến ghi lại mọi thao tác duyệt/từ chối (ADR-0010, ADR-0012).
- Cài đặt cơ chế publish version và cấp endpoint gọi công khai `/api/v1/agents/{id}/invoke` có xác thực API key (ADR-0013).
- Xây dựng Meta-Agent phỏng vấn slot-filling: hỏi từng câu để điền đủ 5 slot nghiệp vụ, tự động biên dịch slot thành `AgentSpec` và cho phép người dùng chat để sửa đổi đặc tả (ADR-0014, ADR-0015, ADR-0016).
- Dựng frontend Next.js: màn hình wizard mô tả nghiệp vụ ban đầu, khung chat phỏng vấn, màn xem trước spec và sơ đồ luồng React Flow chỉ đọc có phủ trạng thái chạy thật (ADR-0017, ADR-0018, ADR-0019).

**2.Vấn đề gặp phải**
- Meta-Agent khi phỏng vấn tự do hay hỏi lan man hoặc hỏi dồn nhiều câu cùng lúc; đã giới hạn thuật toán chỉ hỏi đúng slot còn thiếu và mỗi lượt hỏi tối đa một câu (ADR-0014).
- LLM khi sinh `AgentSpec` đôi khi tạo ra tên tool không tồn tại trong registry; đã ép schema qua Pydantic validator và trả lỗi yêu cầu mô hình sinh lại đúng danh mục tool.
- Kiến trúc V1 chỉ chạy tuần tự (linear pipeline), không giải quyết được các bài toán cần rẽ nhánh điều kiện (if/else), vòng lặp duyệt danh sách và bước con người phê duyệt (Human Approval); cần thiết kế lại lên kiến trúc V2 đồ thị.

**3.Kế hoạch tuần sau**
- Thiết kế lược đồ V2: `AgentSolutionSpecification` (7 trường nghiệp vụ) và `WorkflowJSON` biểu diễn đồ thị DAG có nút và cạnh.
- Định nghĩa chuẩn 8 loại node canvas và xây dựng catalog 5 workflow template mẫu.
- Xây dựng Workflow Canvas trên React Flow: kéo thả node, nối cạnh, bảng thuộc tính và kiểm tra tĩnh lỗi đồ thị (`problems[]`).

---

### Tuần 3 (05/10 – 11/10/2026)

Em xin gửi báo cáo tuần 3 ạ

**Đề tài:** Agent Builder by Natural Languages – Trợ lý dựng Agent mới từ mô tả ngôn ngữ tự nhiên

**1.Đã làm**
- Thiết kế `AgentSolutionSpecification` (7 trường: goal, target_users, inputs, data_sources, actions, constraints, expected_output) và lược đồ đồ thị `WorkflowJSON` có `nodes` và `edges` (ADR-0020, ADR-0022).
- Chuẩn hóa schema cho 8 loại node canvas: `agent_llm`, `knowledge_search`, `web_search`, `mcp`, `if_else`, `while`, `human_approval`, `end` (ADR-0021, ADR-0023).
- Xây dựng catalog 5 mẫu workflow có sẵn (`src/workflow/templates.py`) và module dùng AI chọn mẫu rồi điền cấu hình tham số cho từng node (`src/workflow/fill.py`, ADR-0025).
- Thiết kế cơ chế Human-in-the-loop (HITL) tạm dừng và tiếp tục; mở rộng sandbox chạy thử cho đồ thị rẽ nhánh và vòng lặp while (ADR-0024, ADR-0026).
- Dựng giao diện Canvas biên tập đồ thị bằng React Flow (`frontend/src/pages/Builder.jsx`), bảng thuộc tính nổi và bộ kiểm tra tĩnh phát hiện chu trình chết, node mồ côi (ADR-0027, ADR-0028).

**2.Vấn đề gặp phải**
- Thao tác kéo thả và nối node trên React Flow cập nhật state liên tục làm re-render toàn bộ canvas; đã tách riêng state tọa độ vẽ hiển thị khỏi state cấu hình nghiệp vụ của node và áp dụng debounce khi lưu (ADR-0027).
- Nguy cơ node `while` lặp vô tận gây tốn token và treo runtime; đã bắt buộc khai báo thuộc tính `max_iterations` trên mọi node `while` ngay từ schema validation (ADR-0026).

**3.Kế hoạch tuần sau**
- Xây dựng Workflow Runtime V2 trên LangGraph StateGraph, ánh xạ từng node với handler xử lý tương ứng.
- Hiện thực hóa cơ chế Human Approval: tạm dừng phiên, lưu snapshot vào Postgres checkpointer của LangGraph, sinh token duyệt và khôi phục chạy tiếp.
- Xây dựng Sandbox transcript chi tiết từng bước và hệ thống tự động đánh giá (eval) sinh test case từ spec.

---

### Tuần 4 (12/10 – 18/10/2026)

Em xin gửi báo cáo tuần 4 ạ

**Đề tài:** Agent Builder by Natural Languages – Trợ lý dựng Agent mới từ mô tả ngôn ngữ tự nhiên

**1.Đã làm**
- Dựng Workflow Runtime V2 bằng LangGraph StateGraph (`src/workflow/runtime.py`), chuyển đổi `WorkflowJSON` thành đồ thị có thể thực thi độc lập (ADR-0031).
- Tích hợp PostgreSQL Checkpointer cho LangGraph; khi gặp node `human_approval`, runtime tự động ngắt an toàn, lưu toàn bộ snapshot state và sinh token phê duyệt (ADR-0033).
- Xây dựng luồng Resume: người duyệt phê duyệt/từ chối từ giao diện admin, runtime tải lại state từ Postgres và chạy tiếp các node phía sau mà không cần chạy lại từ đầu (ADR-0033).
- Hoàn thiện Sandbox V2 hiển thị transcript chi tiết: danh sách node đã chạy, tool đã gọi, payload in/out, lỗi và chi phí từng bước (ADR-0032).
- Xây dựng bảng `deployments`, sinh API key băm SHA-256 chỉ hiển thị một lần, bộ giới hạn tần suất Sliding-window Rate Limiter theo phút và đóng gói Approval Dossier (ADR-0035, ADR-0036, ADR-0037).

**2.Vấn đề gặp phải**
- LangGraph checkpointer không tương thích với SQLite do bị lock database khi truy vấn bất đồng bộ đồng thời giữa tiến trình chạy agent và API resume; đã kiên quyết loại bỏ SQLite và dùng PostgreSQL cho checkpointer (ADR-0033).
- Một lượt chạy có node duyệt người dùng có thể bị dừng nhiều ngày, nếu dồn chi phí vào một hóa đơn sẽ khó theo dõi; đã chia lượt chạy thành các chặng (legs), mỗi chặng có hóa đơn chi phí token riêng (ADR-0033).

**3.Kế hoạch tuần sau**
- Xây dựng cơ chế đăng ký Creator Tool và kho lưu khóa bảo mật mã hóa (Secret Store) cho tool MCP.
- Bổ sung tính năng AI Copilot hỗ trợ chỉnh sửa workflow trên Canvas bằng ngôn ngữ tự nhiên.
- Triển khai Retrieval Stack: RAG tìm kiếm tài liệu (Knowledge Base) kết hợp vector và từ khóa.

---

### Tuần 5 (19/10 – 25/10/2026)

Em xin gửi báo cáo tuần 5 ạ

**Đề tài:** Agent Builder by Natural Languages – Trợ lý dựng Agent mới từ mô tả ngôn ngữ tự nhiên

**1.Đã làm**
- Xây dựng cơ chế Creator tự đăng ký tool mới, dùng Python introspection kiểm tra kiểu dữ liệu và cố định mức độ rủi ro do hệ thống kiểm soát (ADR-0038).
- Xây dựng kho lưu trữ bảo mật Secret Store mã hóa AES-256 các token xác thực của tool bên ngoài như Gmail, Drive, Teams (ADR-0045-secret).
- Cài đặt tính năng chỉnh sửa workflow trên Canvas bằng chat ngôn ngữ tự nhiên và tính năng Diff so sánh trực quan giữa 2 phiên bản (ADR-0039, ADR-0040, ADR-0041).
- Tối ưu node `WebSearch` với `query_hint` (ADR-0042).
- Chuẩn hóa khái niệm Knowledge Base (Tủ tài liệu): node `KnowledgeSearch` trỏ tới tủ tài liệu thay vì trỏ vào file đơn lẻ (ADR-0043).
- Xây dựng Retrieval Stack: bóc tách văn bản và cắt đoạn ngữ nghĩa lúc nạp tài liệu (ingest time); triển khai Hybrid RAG kết hợp vector search, BM25 và mô hình reranker (ADR-0044, ADR-0045-hybrid).

**2.Vấn đề gặp phải**
- Nhánh phát triển bị phân mảnh giữa 2 giải pháp RAG: một bên dùng pgvector, một bên dùng Qdrant; đo thực tế cho thấy Qdrant xử lý lọc theo tủ tài liệu (`source_ids`) và tìm kiếm lai tốt hơn hẳn, quyết định chuẩn bị dọn dẹp để thống nhất về Qdrant.
- Khi người dùng hỏi câu dài có nhiều ngữ cảnh cá nhân, node `WebSearch` gửi nguyên câu lên search engine dẫn đến kết quả trả về rỗng; đã thêm `query_hint` và lên kế hoạch chèn node trích xuất từ khóa trước WebSearch.

**3.Kế hoạch tuần sau**
- Tinh giản và đồng nhất kiến trúc nền tảng theo chuẩn UC-1 (`AgentSpec`, capability, connection).
- Tái cấu trúc Meta-Agent thành Agent có tool, có bộ nhớ và có khả năng tự chẩn đoán lỗi lặp.
- Triển khai Node `collect` tự đọc schema để hỏi tham số thiếu.
- Xây dựng phòng điều hành PM (PM Control Room) với sổ run tập trung `agent_runs`.

---

### Tuần 6 (26/10 – 01/11/2026)

Em xin gửi báo cáo tuần 6 ạ

**Đề tài:** Agent Builder by Natural Languages – Trợ lý dựng Agent mới từ mô tả ngôn ngữ tự nhiên

**1.Đã làm**
- Hợp nhất toàn bộ kiến trúc nền tảng theo chuẩn UC-1: chuẩn hóa thực thể `AgentSpec`, capability, connection, data_scope; áp dụng cổng xác thực "Một cổng, hai cửa" (nới lỏng cho bản nháp, siết chặt cho bản duyệt) (ADR-0046, ADR-0047).
- Xây dựng bộ API-level tests làm lá chắn kiểm thử bảo vệ an toàn toàn diện (ADR-0048).
- Nâng cấp Meta-Agent thành Agent có tool, có bộ nhớ ngữ cảnh và có vòng lặp sinh - sửa - chẩn đoán lỗi khi sandbox thất bại (ADR-0049, ADR-0050).
- Cài đặt Node `collect`: tự đọc schema của action đích để chỉ hỏi người dùng đúng các tham số còn thiếu, không suy đoán tùy tiện (ADR-0052).
- Xây dựng PM Control Room: bảng `agent_runs` lưu sổ run tập trung, định nghĩa 12 mã lỗi chẩn đoán `TRACE_CODES` (missing_lookup, fabricated_content...) và dựng giao diện PM Dashboard (ADR-0051, ADR-0053).

**2.Vấn đề gặp phải**
- Trước đây phụ thuộc vào export dữ liệu ra ngoài cho Langfuse khiến chu kỳ phát hiện lỗi và sửa prompt bị chậm; đã tích hợp toàn bộ dashboard PM vào trực tiếp trong nền tảng (ADR-0051).
- LLM hay hỏi lại cả những thông tin người dùng đã cung cấp ở các câu trước; đã giải quyết bằng Node `collect`, đối chiếu tham số trực tiếp với Pydantic schema của action, tham số đã có trong state thì bỏ qua không hỏi lại (ADR-0052).

**3.Kế hoạch tuần sau**
- Xây dựng Khung Đánh giá Toàn diện (Automatic Evaluation): Lưới tình huống (Scenario Grid) và Metric Catalog.
- Xây dựng LLM Judge độc lập 1 lời gọi, đo tiêu chí "không mâu thuẫn" và hiệu chuẩn (calibration) với nhãn chuyên gia.
- Phân tách rõ ràng giữa lỗi do nền tảng tự hỏng và lỗi chất lượng do agent.

---

### Tuần 7 (02/11 – 08/11/2026)

Em xin gửi báo cáo tuần 7 ạ

**Đề tài:** Agent Builder by Natural Languages – Trợ lý dựng Agent mới từ mô tả ngôn ngữ tự nhiên

**1.Đã làm**
- Xây dựng bộ test suite theo agent, hỗ trợ import test case từ CSV/JSON hoặc tự sinh qua mô hình (ADR-0054).
- Thiết kế Ma trận Lưới tình huống (Scenario Grid): LLM sinh đề bài, máy giữ cấu trúc ô ma trận; quy định nguyên tắc một ô chỉ chạy đúng một lần (ADR-0055, ADR-0058).
- Xây dựng LLM Judge chuyên biệt 1 lời gọi: đo tiêu chí "không mâu thuẫn với tài liệu nguồn" và trích xuất bằng chứng (Evidence), loại bỏ phụ thuộc vào thư viện ngoài nặng nề (`ragas`/`deepeval`) (ADR-0059).
- Hiệu chuẩn (Calibration) kết quả chấm của LLM Judge với tập nhãn của chuyên gia: đo TPR, TNR, độ đồng thuận và hệ số Cohen's Kappa.
- Bổ sung trạng thái thứ ba cho ô đánh giá để tách biệt "Agent trả lời sai" và "Nền tảng tự hỏng" (timeout, mạng); loại bỏ các luật kiểm tra hiển nhiên đúng (vacuous pass) (ADR-0061, ADR-0062).
- Chuẩn hóa layout giao diện Tab Đánh giá trên frontend (`EvaluationTab.jsx`, ADR-0056, ADR-0057).

**2.Vấn đề gặp phải**
- Tiêu chí chấm "có đầy đủ căn cứ" quá mơ hồ làm LLM Judge bị bias khắt khe, đánh trượt câu trả lời đúng của agent chỉ vì dùng từ đồng nghĩa; đã chuyển tiêu chí chấm sang "không mâu thuẫn" (uncontradicted) dựa trên passages trích xuất được (ADR-0059).
- Các luật kiểm tra hiển nhiên đúng (như kiểm tra chuỗi rỗng trả về True) tạo điểm xanh giả tạo trên báo cáo; đã rà soát và loại bỏ toàn bộ các luật vacuous pass này ra khỏi evidence (ADR-0062).

**3.Kế hoạch tuần sau**
- Xử lý các bài toán biên: nhận diện câu hỏi ngược `AsksBack`, đặt trần số câu hỏi phỏng vấn, node trích xuất từ khóa trước Web Search.
- Thống nhất dứt điểm stack truy xuất trên Qdrant.
- Hoàn thiện tài liệu vận hành `RUN_GUIDE.md`, kiểm thử hồi quy toàn bộ hệ thống và nghiệm thu.

---

### Tuần 8 (09/11 – 15/11/2026)

Em xin gửi báo cáo tuần 8 ạ

**Đề tài:** Agent Builder by Natural Languages – Trợ lý dựng Agent mới từ mô tả ngôn ngữ tự nhiên

**1.Đã làm**
- Nhận diện câu hỏi ngược (`AsksBack`): giao việc nhận diện ngữ nghĩa câu hỏi làm rõ cho LLM Judge thay vì kiểm tra dấu câu đơn thuần (ADR-0063).
- Tách biệt danh sách quyết định trong quá khứ khỏi hàng đợi chờ duyệt để đảm bảo tính bất biến (ADR-0064).
- Chuẩn hóa Qdrant làm vector store độc quyền, dọn dẹp các tàn dư pgvector cũ (ADR-0065-qdrant).
- Tách API gợi ý câu trả lời phỏng vấn thành lời gọi riêng theo yêu cầu (ADR-0066); đặt trần tối đa 3 câu hỏi cho vòng phỏng vấn (ADR-0067).
- Tự động chèn node trích xuất từ khóa trước node `WebSearch` giúp tránh tìm kiếm nguyên văn câu hỏi thoại dài của người dùng (ADR-0068).
- Hoàn thiện tài liệu hướng dẫn vận hành chi tiết `RUN_GUIDE.md`, `README.md`, slide trình chiếu `slide.html`, và bảng chứng cứ thực nghiệm chạy 4 agent mẫu trong `eval/results/evidence.md`.

**2.Vấn đề gặp phải**
- Chạy toàn bộ các ô của Scenario Grid cùng lúc bị chạm trần rate limit của OpenAI; đã tích hợp cơ chế exponential backoff retry kèm jitter trong `src/evals/runner.py`.
- Node `WebSearch` chạy trên câu hỏi thoại dài không tìm thấy kết quả nào; đã giải quyết triệt để bằng cách tự chèn node trích xuất từ khóa đứng ngay trước `WebSearch` (ADR-0068).

**3.Kế hoạch tuần sau**
- Báo cáo bảo vệ nghiệm thu trước Hội đồng / Ban Giám khảo.
- Thu thập phản hồi từ người dùng thử nghiệm phòng ban để chuẩn bị lộ trình phiên bản tiếp theo.

---

## III. KẾ HOẠCH PHÂN BỔ COMMIT CHI TIẾT THEO TUẦN (74 COMMITS)

### Tuần 1: 21/09 – 27/09/2026 (9 Commits)
| ID | Ngày | Conventional Commit | Files / Thư mục thay đổi | Nội dung kỹ thuật |
|:---:|:---:|---|---|---|
| `c01` | 2026-09-21 | `chore: scaffold project structure and dependencies` | `pyproject.toml`, `requirements.txt`, `Makefile`, `.env.example`, `.gitignore` | Cấu hình môi trường và thư viện Python 3.11 |
| `c02` | 2026-09-21 | `feat(infra): setup docker-compose with postgres pgvector` | `docker-compose.yml`, `.dockerignore`, `Dockerfile` | Thiết lập PostgreSQL 16 với pgvector trên Docker |
| `c03` | 2026-09-22 | `feat(db): initialize alembic migrations and base models` | `alembic/`, `alembic.ini`, `src/models/` | Cấu hình migration Alembic và base ORM models |
| `c04` | 2026-09-22 | `feat(spec): implement AgentSpec schema with validation boundaries` | `src/agentspec/`, `docs/adr/0001-...` | Pydantic schema AgentSpec và validator 5 luật |
| `c05` | 2026-09-23 | `feat(auth): implement two-role jwt authentication` | `src/auth/`, `docs/adr/0002-...` | Xác thực JWT 2 vai trò Creator và Admin |
| `c06` | 2026-09-24 | `feat(runtime): build linear runtime interpreter v1` | `src/workflow/runtime.py`, `docs/adr/0003-...` | Interpreter thực thi AgentSpec tuần tự từng bước |
| `c07` | 2026-09-24 | `feat(memory): add in-session conversation memory` | `docs/adr/0004-...` | Quản lý bộ nhớ hội thoại phiên chat |
| `c08` | 2026-09-25 | `feat(version): implement immutable version lifecycle and pointer` | `docs/adr/0005-...`, `docs/adr/0006-...` | Quản lý version bất biến và dời con trỏ deploy |
| `c09` | 2026-09-26 | `feat(sandbox): add budget enforcement and sandbox runs` | `src/workflow/sandbox.py`, `docs/adr/0007-...`, `0008-...` | Sandbox chạy thử mô phỏng tool và chặn trần ngân sách |

### Tuần 2: 28/09 – 04/10/2026 (10 Commits)
| ID | Ngày | Conventional Commit | Files / Thư mục thay đổi | Nội dung kỹ thuật |
|:---:|:---:|---|---|---|
| `c10` | 2026-09-28 | `feat(tracing): integrate langfuse telemetry and cost metering` | `docker-compose.langfuse.yml`, `docs/adr/0009-...` | Langfuse tracing và tính toán 3 chỉ số chi phí |
| `c11` | 2026-09-29 | `feat(admin): build admin platform settings and model allowlist` | `src/admin/`, `docs/adr/0010-...`, `docs/adr/0011-...` | Bảng settings admin và ngưỡng eval tối thiểu |
| `c12` | 2026-09-30 | `feat(approvals): setup approval queue and audit logging` | `src/approvals/`, `docs/adr/0012-...` | Hàng chờ duyệt và audit log bất biến |
| `c13` | 2026-09-30 | `feat(deployments): add v1 deployment pointer and public invoke api` | `docs/adr/0013-...` | Cấp endpoint gọi public cho agent |
| `c14` | 2026-10-01 | `feat(interview): implement slot-filling conversational meta-agent` | `src/interview/`, `docs/adr/0014-...` | Meta-agent phỏng vấn bóc tách 5 slot nghiệp vụ |
| `c15` | 2026-10-01 | `feat(interview): generate agentspec from filled slots` | `docs/adr/0015-...` | Biên dịch slot thành AgentSpec hoàn chỉnh |
| `c16` | 2026-10-02 | `feat(interview): support chat-driven spec refinement` | `docs/adr/0016-...` | Tinh chỉnh đặc tả bằng chat ngôn ngữ tự nhiên |
| `c17` | 2026-10-02 | `feat(frontend): scaffold nextjs wizard screens` | `frontend/src/pages/Designer.jsx`, `docs/adr/0017-...` | Dựng màn hình Wizard: mô tả, phỏng vấn, xem spec |
| `c18` | 2026-10-03 | `feat(frontend): add read-only flow diagram visualization` | `docs/adr/0018-...`, `docs/adr/0019-...` | Sơ đồ luồng React Flow và phủ trạng thái chạy thật |
| `c19` | 2026-10-03 | `test(v1): add end-to-end integration tests for v1 pipeline` | `tests/conftest.py`, `tests/test_api/` | Test tích hợp toàn bộ luồng V1 |

### Tuần 3: 05/10 – 11/10/2026 (10 Commits)
| ID | Ngày | Conventional Commit | Files / Thư mục thay đổi | Nội dung kỹ thuật |
|:---:|:---:|---|---|---|
| `c20` | 2026-10-05 | `feat(schema): define v2 agent solution specification schema` | `src/solution/`, `docs/adr/0020-...` | Đặc tả bài toán V2 với 7 trường nghiệp vụ |
| `c21` | 2026-10-05 | `feat(schema): define 8 standard canvas node types schema` | `docs/adr/0021-...`, `docs/adr/0023-...` | Chuẩn hóa schema cho 8 loại node canvas |
| `c22` | 2026-10-06 | `feat(schema): implement v2 workflow json graph schema` | `docs/adr/0022-...` | Lược đồ WorkflowJSON hỗ trợ đồ thị DAG |
| `c23` | 2026-10-07 | `feat(templates): implement 5 standard workflow template catalog` | `docs/adr/0025-...` | Catalog 5 mẫu workflow chuẩn doanh nghiệp |
| `c24` | 2026-10-07 | `feat(workflow): build template selection and ai config filling` | `docs/adr/0024-...` | Tự động chọn template và điền config bằng AI |
| `c25` | 2026-10-08 | `feat(sandbox): extend sandbox for branching and while loops` | `docs/adr/0026-...` | Sandbox chạy thử rẽ nhánh if/else và lặp while |
| `c26` | 2026-10-09 | `feat(canvas): build react-flow editable workflow canvas` | `frontend/src/pages/Builder.jsx`, `docs/adr/0027-...` | Canvas kéo thả node và panel thuộc tính nổi |
| `c27` | 2026-10-09 | `feat(validation): implement canvas static validation and problems detection` | `docs/adr/0028-...` | Kiểm tra tĩnh đồ thị, phát hiện chu trình chết |
| `c28` | 2026-10-10 | `feat(approval): design hitl pause and resume mechanics` | `frontend/src/pages/AdminApprovals.jsx` | Thiết kế cơ chế Human-in-the-loop phê duyệt |
| `c29` | 2026-10-10 | `test(workflow): add unit tests for canvas validation and templates` | `tests/test_workflow/` | Unit test cho canvas validation và template catalog |

### Tuần 4: 12/10 – 18/10/2026 (10 Commits)
| ID | Ngày | Conventional Commit | Files / Thư mục thay đổi | Nội dung kỹ thuật |
|:---:|:---:|---|---|---|
| `c30` | 2026-10-12 | `feat(runtime): build v2 langgraph workflow runtime engine` | `docs/adr/0029-...`, `0030-...`, `0031-...` | Runtime thực thi đồ thị bằng LangGraph StateGraph |
| `c31` | 2026-10-13 | `feat(runtime): integrate postgres checkpointer for langgraph` | `src/db/` | Tích hợp PostgreSQL Checkpointer cho LangGraph |
| `c32` | 2026-10-14 | `feat(runtime): implement hitl pause and resume execution flow` | `docs/adr/0033-...` | Ngắt và khôi phục run an toàn khi duyệt HITL |
| `c33` | 2026-10-14 | `feat(sandbox): build execution transcript and node-by-node tracing` | `docs/adr/0032-...` | Bảng theo dõi transcript và chi phí từng node |
| `c34` | 2026-10-15 | `feat(evals): implement automatic evaluation rule-based checks` | `src/evals/`, `docs/adr/0034-...` | Tự động sinh test case và kiểm tra rule-based |
| `c35` | 2026-10-16 | `feat(approvals): assemble approval dossier from sandbox and eval` | `docs/adr/0035-...` | Hồ sơ phê duyệt tổng hợp kết quả sandbox và eval |
| `c36` | 2026-10-16 | `feat(deployments): implement v2 deployments table and api keys` | `src/deployments/`, `docs/adr/0036-...` | Bảng deployments và quản lý API key bảo mật |
| `c37` | 2026-10-17 | `feat(deployments): add sliding-window per-caller rate limiting` | `docs/adr/0037-...` | Bộ giới hạn tần suất Sliding-window Rate Limiter |
| `c38` | 2026-10-17 | `test(runtime): add integration tests for pause resume flow` | `tests/test_workflow/` | Kiểm thử tích hợp luồng pause/resume trên Postgres |
| `c39` | 2026-10-18 | `docs: document v2 runtime execution and state transitions` | `docs/specs/v2-agent-platform.md` | Tài liệu kiến trúc runtime V2 |

### Tuần 5: 19/10 – 25/10/2026 (10 Commits)
| ID | Ngày | Conventional Commit | Files / Thư mục thay đổi | Nội dung kỹ thuật |
|:---:|:---:|---|---|---|
| `c40` | 2026-10-19 | `feat(tools): implement creator tool registration with risk tiers` | `docs/adr/0038-...` | Creator tự đăng ký tool mới kèm phân loại rủi ro |
| `c41` | 2026-10-19 | `feat(deployments): add version diff and deployment rollback` | `docs/adr/0039-...` | So sánh khác biệt giữa 2 version và rollback |
| `c42` | 2026-10-20 | `feat(canvas): enable conversational workflow editing on canvas` | `docs/adr/0040-...` | Sửa đổi workflow trên Canvas bằng ngôn ngữ tự nhiên |
| `c43` | 2026-10-20 | `feat(canvas): support workflow composition and subgraph embedding` | `docs/adr/0041-...` | Ghép nối workflow con tái sử dụng |
| `c44` | 2026-10-21 | `feat(tools): refine web search node and query hint handling` | `docs/adr/0042-...` | Tối ưu hóa node WebSearch với query hint |
| `c45` | 2026-10-22 | `feat(rag): model knowledge base shelf and document management` | `frontend/src/pages/KnowledgeBases.jsx`, `docs/adr/0043-...` | Định nghĩa Tủ tài liệu và quản lý file tài liệu |
| `c46` | 2026-10-22 | `feat(rag): implement ingest-time semantic chunking and extraction` | `src/services/`, `docs/adr/0044-...` | Cắt đoạn ngữ nghĩa và bóc tách lúc ingest tài liệu |
| `c47` | 2026-10-23 | `feat(security): implement encrypted secret store for mcp credentials` | `docs/adr/0045-secret...` | Kho lưu khóa bảo mật mã hóa cho credentials MCP |
| `c48` | 2026-10-23 | `feat(rag): build hybrid rag with qdrant, bm25 and reranker` | `reranker/`, `docs/adr/0045-hybrid...` | Tìm kiếm lai kết hợp vector, BM25 và reranker |
| `c49` | 2026-10-24 | `test(rag): add comprehensive rag retrieval precision benchmarks` | `tests/test_workflow/` | Đo kiểm độ chính xác trích dẫn tài liệu RAG |

### Tuần 6: 26/10 – 01/11/2026 (9 Commits)
| ID | Ngày | Conventional Commit | Files / Thư mục thay đổi | Nội dung kỹ thuật |
|:---:|:---:|---|---|---|
| `c50` | 2026-10-26 | `refactor: align system architecture with uc1 specification` | `CONTEXT.md`, `docs/adr/0046-...` | Đồng nhất nền tảng theo kiến trúc UC-1 |
| `c51` | 2026-10-26 | `feat(spec): enforce two-door validation gate for agentspec` | `docs/adr/0047-...` | Cổng kiểm thực "Một cổng, hai cửa" cho AgentSpec |
| `c52` | 2026-10-27 | `test(api): establish api-level test suite as primary safety net` | `tests/test_api/`, `docs/adr/0048-...` | Bộ API-level tests làm lá chắn kiểm thử hệ thống |
| `c53` | 2026-10-28 | `feat(metaagent): build conversational multi-turn meta-agent` | `src/metaagent/`, `docs/adr/0049-...` | Meta-agent đàm thoại đa lượt có nhớ và chẩn đoán |
| `c54` | 2026-10-29 | `feat(metaagent): empower meta-agent with tool invocation loop` | `docs/adr/0050-...` | Meta-agent sinh, sửa và chẩn đoán theo vòng lặp tool |
| `c55` | 2026-10-30 | `feat(workflow): implement collect node for schema-driven param asking` | `docs/adr/0052-...` | Node collect đọc schema để chỉ hỏi tham số thiếu |
| `c56` | 2026-10-30 | `feat(pm): setup pm control room and failure taxonomy trace codes` | `src/pm/`, `docs/adr/0051-...` | Bảng phân loại 12 mã lỗi trace codes cho PM |
| `c57` | 2026-10-31 | `feat(pm): establish shared agent_runs ledger and pm dashboard` | `frontend/src/pages/PMDashboard.jsx`, `docs/adr/0053-...` | Sổ run tập trung `agent_runs` và giao diện PM Dashboard |
| `c58` | 2026-10-31 | `test(metaagent): add golden dataset evaluation for meta-agent` | `golden/`, `docs/eval-meta-agent-golden-v1.md` | Kiểm thử Meta-Agent trên tập golden đề bài mẫu |

### Tuần 7: 02/11 – 08/11/2026 (9 Commits)
| ID | Ngày | Conventional Commit | Files / Thư mục thay đổi | Nội dung kỹ thuật |
|:---:|:---:|---|---|---|
| `c59` | 2026-11-02 | `feat(evals): build creator test suite and csv upload pipeline` | `docs/adr/0054-...` | Test suite theo agent và cơ chế nạp đề thi CSV |
| `c60` | 2026-11-03 | `feat(evals): implement scenario grid matrix generator` | `docs/adr/0055-...` | Ma trận Lưới tình huống (Scenario Grid) |
| `c61` | 2026-11-03 | `feat(evals): separate evaluation view by metrics dimension` | `docs/adr/0056-...` | Bố cục màn hình Đánh giá chia theo thước đo |
| `c62` | 2026-11-04 | `feat(frontend): unify layout frame and typography across pages` | `frontend/src/theme.js`, `docs/adr/0057-...` | Khung layout và typography thống nhất trên frontend |
| `c63` | 2026-11-04 | `feat(evals): enforce one-trial-per-cell execution principle` | `docs/adr/0058-...` | Nguyên tắc một ô một lượt chạy độc lập |
| `c64` | 2026-11-05 | `feat(evals): build standalone calibrated single-call llm judge` | `docs/adr/0059-...` | LLM Judge độc lập 1 lời gọi đo không mâu thuẫn |
| `c65` | 2026-11-06 | `feat(evals): support draggable threshold sliders on evaluation axes` | `docs/adr/0060-...` | Thanh trượt điều chỉnh ngưỡng chất lượng |
| `c66` | 2026-11-06 | `feat(evals): isolate platform infra failures from agent quality verdict` | `docs/adr/0061-...` | Tách lỗi nền tảng tự hỏng khỏi điểm đánh giá agent |
| `c67` | 2026-11-07 | `feat(evals): eliminate vacuous pass rules from test evidence` | `docs/adr/0062-...` | Loại bỏ luật không thể sai ra khỏi bằng chứng eval |

### Tuần 8: 09/11 – 15/11/2026 (8 Commits)
| ID | Ngày | Conventional Commit | Files / Thư mục thay đổi | Nội dung kỹ thuật |
|:---:|:---:|---|---|---|
| `c68` | 2026-11-09 | `feat(evals): enhance asks-back semantic evaluation via judge` | `docs/adr/0063-...` | Nhận diện câu hỏi ngược bằng ngữ nghĩa qua Judge |
| `c69` | 2026-11-09 | `feat(approvals): decouple decision history audit from pending queue` | `docs/adr/0064-...` | Tách biệt danh sách đã quyết khỏi hàng chờ duyệt |
| `c70` | 2026-11-10 | `refactor(rag): standardize qdrant as exclusive retrieval backend` | `docs/adr/0065-...` | Chuẩn hóa Qdrant làm vector store độc quyền |
| `c71` | 2026-11-11 | `feat(interview): isolate interview suggestion as on-demand model call` | `docs/adr/0066-...` | Tách gợi ý câu trả lời phỏng vấn thành API riêng |
| `c72` | 2026-11-12 | `feat(interview): enforce question ceiling limit on meta-agent` | `docs/adr/0067-...` | Đặt trần tối đa 3 câu hỏi cho vòng phỏng vấn |
| `c73` | 2026-11-13 | `feat(workflow): auto insert query extraction node before web search` | `docs/adr/0068-...` | Tự chèn node trích xuất từ khóa trước WebSearch |
| `c74` | 2026-11-14 | `chore(release): finalize documentation, run guide, and presentation slide` | `README.md`, `RUN_GUIDE.md`, `slide.html`, `eval/results/` | Hoàn thiện tài liệu hướng dẫn, slide và bằng chứng nghiệm thu |
