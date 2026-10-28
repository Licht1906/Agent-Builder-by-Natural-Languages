# Agent Builder by Natural Languages

Nhân viên văn phòng mô tả công việc bằng tiếng Việt. Hệ thống phỏng vấn lại cho
đủ ý, dựng thành một workflow LangGraph chạy được, chạy thử trong sandbox, tự
sinh test case rồi tự chấm, và chỉ cho publish sau khi có người duyệt.

> Dự án **Agent Builder by Natural Languages**. Xem
> [`TOPIC.md`](TOPIC.md) và [`VERSION2-REQUIREMENT.md`](VERSION2-REQUIREMENT.md).

**Ví dụ một vòng đầy đủ, đã chạy thật:** cán bộ phòng đào tạo mô tả *"sinh viên
hay hỏi em về quy chế đào tạo, em muốn một trợ lý đọc file PDF quy chế rồi trả
lời, trích rõ điều khoản, câu nào quy chế không nói thì phải nói là không có"* →
hệ thống dựng workflow 6 node → hỏi *"em trượt 10 tín chỉ thì có bị cảnh báo
không?"* → agent trả lời **"Theo Điều 19, khoản 1, mục a (trang 15)…"**. Toàn bộ
transcript ở [`eval/results/evidence.md`](eval/results/evidence.md).

---

## Mục lục

- [Có gì trong này](#có-gì-trong-này)
- [Yêu cầu môi trường](#yêu-cầu-môi-trường)
- [Cài đặt](#cài-đặt)
- [Biến môi trường](#biến-môi-trường)
- [Sample queries](#sample-queries)
- [Cấu trúc dự án](#cấu-trúc-dự-án)
- [Kiểm thử](#kiểm-thử)
- [Xử lý sự cố](#xử-lý-sự-cố)
- [Tài liệu khác](#tài-liệu-khác)

---

## Có gì trong này

| # | Phần | Trạng thái | Ở đâu |
|---|---|---|---|
| 1 | **AI Solution Designer** — mô tả tự nhiên → specification 7 trường, tự hỏi lại chỗ thiếu | ✅ | `src/solution/`, `src/interview/` |
| 2 | **Workflow Generator** — chọn 1 trong 5 template rồi để AI điền cấu hình từng node | ✅ | `src/workflow/{templates,selection,fill}.py` |
| 3 | **Workflow Canvas** — 8 node type, nối/sửa/xoá, validate trả `problems[]`, sửa bằng lời | ✅ | `frontend/`, `src/workflow/validation.py` |
| 4 | **Workflow Runtime** — LangGraph, state, condition, pause/resume, execution trace | ✅ | `src/workflow/runtime.py` |
| 5 | **Tool cốt lõi** — Knowledge Search (PDF/DOCX), MCP, Web search | ✅ | `src/workflow/{knowledge,mcp,tools}.py` |
| 6 | **Sandbox** — chạy thử, xem từng node/tool/input/output, token + chi phí thật | ✅ | `src/workflow/sandbox.py` |
| 7 | **Automatic Evaluation** — sinh case từ spec, rule-based check + LLM judge, gate chặn deploy | ✅ | `src/evals/` |
| 8 | **Publish & quản lý** — version, so sánh, HITL duyệt, deployment, rate limit, quyền truy cập | ✅ | `src/deployments/`, `src/admin/` |

Kiến trúc: [`docs/architecture_diagram.md`](docs/architecture_diagram.md) ·
file draw.io: [`docs/architecture.drawio`](docs/architecture.drawio) ·
bằng chứng chạy thật: [`eval/results/evidence.md`](eval/results/evidence.md).

### 8 node type của canvas

`agent_llm` · `knowledge_search` · `web_search` · `mcp` · `if_else` · `while` ·
`human_approval` · `end`

Trong sandbox, node **không** có side effect (`knowledge_search`, `web_search`)
chạy thật; node **có** side effect (`mcp`) dùng mock — chạy thử trên một bản giả
lập của chính tài liệu mình thì không chứng minh được gì.

---

## Yêu cầu môi trường

| Thứ | Phiên bản | Ghi chú |
|---|---|---|
| Python | ≥ 3.11 | `pyproject.toml` yêu cầu 3.11+ |
| Docker Desktop | bất kỳ | Chạy bốn dịch vụ nền: Postgres, Qdrant, reranker, MailHog |
| Node.js | ≥ 18 | Cho frontend |
| OpenAI API key | — | Bắt buộc: nếu không có thì Solution Designer, Workflow Generator và mọi node `agent_llm` đều không chạy được |

**Không dùng SQLite.** LangGraph checkpointer — thứ làm nên pause/resume của
Human Approval — chỉ chạy trên Postgres. Ảnh Docker phải là `pgvector/pgvector:pg16`
chứ không phải `postgres:16`: migration `a7f5c34e91d2` gọi `CREATE EXTENSION vector`,
ảnh Postgres thường không có thư viện đó. (Bảng dùng extension ấy đã bị drop ở
ADR-0065 — embedding nằm trong Qdrant — nhưng migration cũ vẫn phải chạy được
từ đầu, nên ảnh vẫn phải có `vector`.)

---

## Cài đặt

### 1. Môi trường Python

```bash
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux / Git Bash
source .venv/bin/activate

pip install -e ".[dev]"
```

### 2. Cấu hình

```bash
cp .env.example .env
```

Mở `.env` và điền tối thiểu **một** dòng:

```dotenv
OPENAI_API_KEY=sk-...
```

Mọi biến còn lại đã có mặc định chạy được ở máy cá nhân. Xem
[bảng biến môi trường](#biến-môi-trường) bên dưới.

### 3. Dịch vụ nền + database

```bash
make up                     # db, qdrant, reranker, mailhog — đợi tới khi tất cả healthy
alembic upgrade head        # hoặc: make migrate
make init-qdrant            # tạo collection, idempotent
```

Không có `make` (Windows):
`docker compose up -d --wait db qdrant reranker mailhog`

Kiểm tra:

```bash
docker compose ps           # sáu dòng, tất cả (healthy)
```

> Bật thiếu `qdrant` là lỗi khó thấy nhất: upload vẫn trả `201`, job ingest chết
> ngầm, và dấu hiệu đầu tiên là agent trả lời "tài liệu không đề cập" về đúng
> tài liệu bạn vừa tải lên.

Migration seed sẵn hai tài khoản, một cho mỗi vai trò:

| Vai trò | Email | Mật khẩu |
|---|---|---|
| Người tạo agent | `creator@example.com` | `creator-dev-password` |
| Admin duyệt | `admin@example.com` | `admin-dev-password` |

> Đổi mật khẩu qua `SEED_CREATOR_PASSWORD` / `SEED_ADMIN_PASSWORD` trước khi
> deploy ra chỗ nào truy cập được từ ngoài.

### 4. Backend

```bash
make run
# tương đương:
# uvicorn src.main:app --reload --host 0.0.0.0 --port 8000 --loop src.server_loop:new_event_loop
```

Swagger UI: <http://localhost:8000/docs> · health check: <http://localhost:8000/health>

> ⚠️ **Dùng `make run`, đừng gọi `uvicorn` trần trên Windows.** Cờ
> `--loop src.server_loop:new_event_loop` là thứ driver Postgres bất đồng bộ cần
> để chạy trên event loop mặc định của Windows. Thiếu nó, lượt chat đầu tiên treo
> 30 giây rồi báo `PoolTimeout`. Trên Linux cờ này không có tác dụng gì.

### 5. Frontend

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

Trình duyệt không gọi thẳng FastAPI: mọi thứ dưới `/api` được Vite dev server
proxy sang backend, nên không có CORS trong luồng phát triển. Bản build cũng chạy
sau bất kỳ reverse proxy nào map `/api` về backend — không có URL nào bị nhúng
cứng vào bundle.

Đăng nhập bằng tài khoản seed ở bảng trên.

### Cài đặt đóng gói (single-node)

Thay cho Bước 2–4 ở trên (dev loop từ source) — dựng cả nền tảng trên một máy:

```bash
git clone <repo> && cd <repo>
./install.sh            # sinh .env + secret, migrate DB (seed agent_admin luôn trong bước này)
```

`install.sh` tự gọi `docker compose up -d` ở bước cuối. Pipeline UC-1 song song
và các màn hình "Enterprise" đã được gỡ (DS-21, ADR-0059 §11); năng lực của nó
nằm trong V2 dưới tên "Nguồn dữ liệu".

Chế độ ngoại tuyến hoàn toàn (§12.3): đóng gói trên máy có mạng bằng
`scripts/package_airgap_artifact.sh` (ký bằng cosign hoặc minisign), rồi
`scripts/import_airgap_artifact.sh` trên máy đích — script import không gọi
ra mạng ở bất kỳ bước nào. Chi tiết trong docstring của hai script đó.

### Bước 5: Đọc hướng dẫn

---

## Biến môi trường

Đọc từ `src/config.py`. Cột **Mặc định** là giá trị khi không khai báo gì.

### Bắt buộc trên thực tế

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `OPENAI_API_KEY` | `""` | Không có thì không có bước nào gọi LLM chạy được |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/app_dev` | Postgres, không phải SQLite |
| `JWT_SECRET` | `dev-only-insecure-secret-change-me` | ≥ 32 ký tự. `APP_ENV=production` **từ chối khởi động** nếu vẫn là giá trị mặc định public này |

### Ứng dụng

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `APP_ENV` | `development` | `development` / `production` / `test` |
| — (hằng số trong code) | `META_AGENT_MODEL` / `DOCUMENT_MODEL` | Nền tảng gọi model cho **hai việc khác nhau** và dùng hai model khác nhau. `META_AGENT_MODEL` (`gpt-4.1`) là chỗ **quyết định gọi tool** — không có gì đứng giữa quyết định sai và hành động thật, nên ADR-0010 §1 vẫn nguyên giá trị. `DOCUMENT_MODEL` (`gpt-4.1-mini`) là chỗ **viết tài liệu JSON** — spec, graph, bộ đề, lời cho template — nơi validator chặn ở cuối và `write_document` hỏi lại tới 3 lần; đo 12 lần soạn mỗi phe thì mini **12/12** còn `gpt-4.1` **10/12**, rẻ hơn **5×**. Cả hai là hằng số trong code, không phải setting: model nền tảng tự dùng là quyết định của đội, không phải của admin |
| `JUDGE_MODEL` | `META_AGENT_MODEL` | Model chấm eval (ADR-0054 §4). Bài giảng khuyên judge khác họ model với generator, và một model rẻ vẫn đủ cho phán quyết đạt/không. Đổi giá trị này là **đổi baseline** — `eval_runs.judge_model` ghi lại model đã chấm từng lần, và report không so delta giữa hai judge khác nhau |
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8000` | |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Chỉ dùng cho request trình duyệt gọi thẳng FastAPI |
| `MODEL_NAME` | `gpt-4o-mini` | Model mặc định của agent V1 |
| `LLM_TEMPERATURE` | `0.7` | 0.0–2.0 |

### Xác thực

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `JWT_EXPIRE_MINUTES` | `60` | |
| `SEED_CREATOR_EMAIL` / `SEED_CREATOR_PASSWORD` | `creator@example.com` / `creator-dev-password` | Tài khoản seed vai trò người tạo |
| `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` | `admin@example.com` / `admin-dev-password` | Tài khoản seed vai trò admin |
| `ADMIN_SIGNUP_CODE` | `""` | Mã để tự đăng ký tài khoản admin. **Rỗng = đóng hẳn**, không phải "không cần mã" |

### Knowledge Search / RAG (ADR-0044, ADR-0065)

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `EMBEDDING_MODEL` | `voyage-4,text-embedding-3-small` | Voyage được ưu tiên; OpenAI là fallback. Nhà cung cấp đọc từ chính tên model: `text-embedding-*` = OpenAI, `voyage-*` = Voyage. Ghi nhiều model cách nhau bằng dấu phẩy = thứ tự dự phòng |
| `VOYAGE_API_KEY` | `""` | Chỉ cần khi `EMBEDDING_MODEL` có model `voyage-*` |
| `EMBEDDING_TOKEN_ALLOWANCE` | `0` | Trần token cho **mỗi** model, `0` = không giới hạn |
| `QDRANT_URL` | `http://localhost:6333` | Nơi passage thực sự nằm |
| `RERANKER_URL` | `http://localhost:8001` | `bge-reranker-v2-m3` sau một service riêng |
| `KNOWLEDGE_DENSE_TOP_K` | `20` | Số ứng viên nhánh dense lấy trước khi fuse |
| `KNOWLEDGE_SPARSE_TOP_K` | `20` | Như trên, cho nhánh sparse |
| `KNOWLEDGE_RERANK_SCORE_THRESHOLD` | `0.0` | Ngưỡng điểm **cross-encoder**; không áp lên điểm RRF vì hai thang không so được |

> **Không có key embedding thì tài liệu không index được.** Đây là thay đổi từ
> [ADR-0065](docs/adr/0065-mot-stack-truy-xuat-qdrant-thang-pgvector-vi-source-ids-la-id-tu-tai-lieu.md):
> stack pgvector chạy BM25 thuần đã bị bỏ, chỉ còn một đường truy xuất và nó đọc
> vector trong Qdrant. Ingest **từ chối** thay vì ghi một tài liệu không vector.
>
> Đổi `EMBEDDING_MODEL` thì phải `reingest`: vector của hai model khác nhau không
> so sánh được, kể cả gần đúng.

Không còn `CHUNK_TARGET_CHARS` / `CHUNK_OVERLAP_CHARS` / `CHUNK_MIN_CHARS` /
`SEMANTIC_BREAKPOINT_PERCENTILE`. Cắt đoạn giờ theo **cấu trúc tác giả viết**
(`src/services/chunking.py`) — `Chương` / `Điều` / `Khoản`, hoặc heading của tài
liệu thường — nên không có ngân sách ký tự nào để chỉnh.

Đo chất lượng trên tài liệu thật (cần `qdrant` + `reranker` đang chạy):

```bash
python eval/rag_benchmark.py --rerank
```

### Tool

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `TOOL_FILE_BASE_DIR` | `./data/workspace` | Nơi cất bytes gốc của tài liệu đã tải lên (ingest và `reingest` đọc lại từ đây). **Không** phải thứ `source_ids` trỏ tới — từ ADR-0043 §1, `source_ids` là **id tủ tài liệu**, và passage nằm trong Qdrant |
| `SEARCH_API_KEY` | `""` | Tavily, cho node `web_search`. Rỗng thì node ghi "chưa cấu hình" vào transcript và run vẫn chạy tiếp |
| `SEARCH_API_URL` | `https://api.tavily.com/search` | |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM` | rỗng / `587` / … | Cho tool gửi email |
| `TOOL_HTTP_TIMEOUT_SECONDS` | `15.0` | |

### Quan sát

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | `""` | Để trống cả hai = không trace. Chi phí vẫn được ghi đủ vào bảng `run_costs`; trace không bao giờ chặn một lần chạy |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | |

> `CHROMA_PERSIST_DIR` còn sót trong `.env.example` từ template gốc. Hệ thống
> **không dùng ChromaDB** — vector nằm trong Qdrant. Bỏ qua dòng đó.

---

## Sample queries

Toàn bộ lệnh dưới đây đã chạy thật trên máy sạch; output đầy đủ nằm ở
[`eval/results/evidence.md`](eval/results/evidence.md).

### 0. Kiểm tra hệ thống sống

```bash
curl http://localhost:8000/health
# {"status":"ok","env":"development","database":"ok"}
```

### 1. Đăng nhập, lấy token

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"creator@example.com","password":"creator-dev-password"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s http://localhost:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN"
# {"id":1,"email":"creator@example.com","role":"creator"}
```

### 2. Mô tả bài toán bằng tiếng Việt → specification

```bash
curl -s -X POST http://localhost:8000/api/v1/solution-interviews \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"description":"Tôi làm ở phòng đào tạo. Sinh viên liên tục hỏi em về quy chế đào tạo: bao nhiêu tín chỉ trượt thì bị cảnh báo, nghỉ ốm có bị tính chậm tiến độ không. Tôi muốn một trợ lý đọc file quy chế PDF rồi trả lời, trích rõ điều khoản. Câu nào quy chế không nói thì phải nói là không có, tuyệt đối không được đoán."}'
```

Còn thiếu ý thì trả lời tiếp từng câu:

```bash
curl -s -X POST http://localhost:8000/api/v1/solution-interviews/1/answers \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"answer":"Người dùng là sinh viên đại học của trường."}'
```

Đủ ý rồi thì chốt thành specification (trả về `agent_id` + `version_id`):

```bash
curl -s -X POST http://localhost:8000/api/v1/solution-interviews/1/spec \
  -H "Authorization: Bearer $TOKEN"
```

### 3. Tải tài liệu nguồn

```bash
curl -s -X POST http://localhost:8000/api/v1/documents \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@data/workspace/creators/3/quy_che_dao_tao_hust.pdf"
# {"ref":"quy_che_dao_tao_hust.pdf","size_bytes":...,"modified_at":"..."}
```

Gắn `ref` đó vào nguồn dữ liệu trong spec (`PUT …/solution-spec`) rồi xác nhận:

```bash
curl -s -X POST http://localhost:8000/api/v1/agents/14/versions/15/solution-spec/confirmation \
  -H "Authorization: Bearer $TOKEN"
```

> Cố ý bắt người gắn `ref`, không để AI đoán: một model đọc mô tả không thể biết
> người ta định trỏ vào **file nào**.

### 4. Sinh workflow

```bash
curl -s -X POST http://localhost:8000/api/v1/agents/14/versions/15/workflow \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}'
```

Trả về `WorkflowJSON` đầy đủ node/edge kèm `problems: []`. Muốn ép một template
cụ thể thì gửi `{"template_id": "document_qa"}`. Xem danh sách:

```bash
curl -s http://localhost:8000/api/v1/workflow/templates -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/api/v1/workflow/node-types -H "Authorization: Bearer $TOKEN"
```

### 5. Chạy thử trong sandbox (SSE)

```bash
curl -N -X POST http://localhost:8000/api/v1/agents/14/versions/15/workflow-sandbox-runs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"message":"Em trượt 10 tín chỉ trong học kỳ này thì có bị cảnh báo học tập không?"}'
```

Trả về stream sự kiện: `node_started` → `tool` → `tool_result` → `token` →
`node` → `usage` → `cost` → `sandbox_run`.

### 6. Sinh test case rồi chấm

```bash
curl -s -X POST http://localhost:8000/api/v1/agents/14/versions/15/workflow-eval-cases/generate \
  -H "Authorization: Bearer $TOKEN"

curl -s -X POST http://localhost:8000/api/v1/agents/14/versions/15/workflow-eval-runs \
  -H "Authorization: Bearer $TOKEN"
# {"passed_count":8,"total_count":10,"pass_rate":0.8,"threshold":0.6,"meets_threshold":true,...}
```

### 7. Duyệt và publish

```bash
# người tạo gửi đi duyệt
curl -s -X POST http://localhost:8000/api/v1/agents/14/versions/15/approval-request \
  -H "Authorization: Bearer $TOKEN"

# admin duyệt
ADMIN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"admin-dev-password"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s http://localhost:8000/api/v1/admin/approvals -H "Authorization: Bearer $ADMIN"
curl -s -X POST http://localhost:8000/api/v1/admin/approvals/15/approve \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"reason":"Spec rõ, eval đạt ngưỡng."}'

# publish
curl -s -X PUT http://localhost:8000/api/v1/agents/14/deployed-version \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"version_id":15}'
```

### 8. Người dùng cuối hỏi agent đã publish

Deployment mặc định là `private`. Cấp quyền cho đúng người trước:

```bash
curl -s -X PUT http://localhost:8000/api/v1/agents/14/deployment/access \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"access_mode":"selected_users","allowed_user_emails":["student@example.com"]}'
```

Rồi họ hỏi bằng token của chính họ:

```bash
curl -N -X POST http://localhost:8000/api/v1/agents/14/deployed-workflow-chat \
  -H "Authorization: Bearer $STUDENT_TOKEN" -H "Content-Type: application/json" \
  -d '{"message":"Em bị cảnh báo học tập mức 2 thì kỳ này được đăng ký tối đa bao nhiêu tín chỉ?"}'
```

Trả lời thật của hệ thống:

> Theo Điều 19, khoản 2, trang 15:
> - Nếu bạn học chương trình đào tạo chuẩn thì được đăng ký tối đa 14 tín chỉ cho một học kỳ chính.
> - Nếu bạn học chương trình ELITECH hoặc chương trình hợp tác quốc tế thì được đăng ký tối đa 18 tín chỉ cho một học kỳ chính.

Gọi khi **chưa** được cấp quyền thì nhận `404` — cố ý không phải `403`, để người
lạ dò không biết được agent này có tồn tại hay không.

---

## Cấu trúc dự án

```
├── src/
│   ├── api/                  # 96 route FastAPI, prefix /api/v1
│   ├── auth/                 # JWT, 2 vai trò, seed tài khoản
│   ├── interview/            # phỏng vấn V1 (AgentSpec)
│   ├── solution/             # AI Solution Designer (specification 7 trường)
│   ├── workflow/             # trái tim của V2
│   │   ├── runtime.py        #   compile WorkflowJSON → LangGraph, chạy 8 node
│   │   ├── templates.py      #   catalog template
│   │   ├── selection.py      #   AI chọn template nào
│   │   ├── fill.py           #   AI điền cấu hình từng node
│   │   ├── validation.py     #   kiểm tra đồ thị, trả problems[]
│   │   ├── knowledge.py      #   node knowledge_search — QdrantKnowledgeStore
│   │   ├── embeddings.py     #   OpenAI / Voyage
│   │   ├── approvals.py      #   pause/resume Human Approval
│   │   ├── sandbox.py        #   chạy thử + transcript
│   │   └── mcp.py            #   MCP tool
│   ├── services/             # hạ tầng dùng chung
│   │   ├── extraction.py     #   PDF/DOCX → khối, có VLM cho trang scan
│   │   ├── chunking.py       #   cắt đoạn theo cấu trúc tác giả viết
│   │   ├── ingestion.py      #   bóc → cắt → embed → ghi Qdrant
│   │   ├── qdrant.py         #   collection, dense + sparse
│   │   └── reranker.py       #   client cho bge-reranker-v2-m3
│   ├── evals/                # sinh case, rule check, LLM judge, gate
│   ├── agents/               # version, budget, cost, thread, memory
│   ├── deployments/          # publish, api key, rate limit, quyền
│   ├── admin/                # hàng đợi duyệt, cấu hình nền tảng
│   ├── db/models.py          # 25 bảng SQLAlchemy
│   ├── server_loop.py        # event loop uvicorn phải dùng trên Windows
│   └── main.py
├── frontend/                 # React 18 + Vite 5
├── alembic/                  # migration
├── tests/                    # 505 test
├── docs/
│   ├── architecture_diagram.md    # sơ đồ (mermaid + hướng dẫn draw.io)
│   ├── architecture.drawio        # file draw.io 4 trang
│   ├── adr/                       # 45+ architecture decision record
│   └── agents/                    # quy ước làm việc với AI agent
├── eval/results/evidence.md  # bằng chứng chạy thật
├── scripts/gen_architecture.py    # sinh lại file .drawio
├── data/workspace/           # thư viện tài liệu của người dùng
└── docker-compose.yml
```

---

## Kiểm thử

```bash
pytest tests/ -v          # hoặc: make test
ruff check src/ tests/    # hoặc: make lint
mypy src/                 # hoặc: make typecheck
make check                # lint + format + test
```

Kết quả lần chạy gần nhất: **1068 passed** trong 173 giây.

> Không cần Docker cho Qdrant hay reranker: `tests/conftest.py` dựng
> `QdrantClient(":memory:")` và thay embedding/reranker bằng bản giả tất định,
> nên `pytest` chạy offline.

> Bộ test chạy trên database **riêng** (`app_dev_test`), tự suy ra từ
> `DATABASE_URL` bằng cách thêm hậu tố `_test` — xem `tests/db_target.py`. Nó
> `DROP` toàn bộ schema ở đầu mỗi lần chạy, và từ chối chạy nếu tên database
> không kết thúc bằng `_test`. Đừng gỡ hàng rào đó: chạy test với `.env` thường
> sẽ **xoá sạch database phát triển** và báo màu xanh.

---

## Xử lý sự cố

| Triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| Lượt chat đầu treo 30s rồi `PoolTimeout` (Windows) | Chạy `uvicorn` trần, thiếu cờ `--loop` | Dùng `make run` |
| `[WinError 10013] socket access permissions` | Cổng 8000 đang bị chiếm | `netstat -ano \| findstr :8000` rồi `taskkill /PID <pid> /F` |
| `CREATE EXTENSION vector` thất bại khi migrate | Dùng ảnh `postgres:16` thay vì `pgvector/pgvector:pg16` | Sửa `docker-compose.yml`, tạo lại volume |
| Ingest chết `ConnectError [WinError 10061]`, tài liệu `failed` | Container `qdrant` chưa bật | `make up`, rồi `POST .../documents/{id}/reingest` |
| Log `Reranker unusable … falling back to unranked order` | Container `reranker` chưa bật | `make up`. Không sập, nhưng đo được: hit@8 tụt 19/22 → 16/22 |
| Đổi từ `postgres:16-alpine` sang `pgvector` xong thấy index lạ | alpine dùng musl, ảnh mới dùng glibc, hai bên sắp xếp text khác nhau | Chạy `REINDEX DATABASE app_dev;` một lần |
| `connection refused` cổng 5432 | Container database chưa bật | `docker compose up -d db` |
| Agent trả lời "tài liệu không đề cập" cho câu quy chế **có** nói | Recall của retrieval — xem [chẩn đoán RAG](#tài-liệu-khác) | Kiểm tra `EMBEDDING_MODEL` có key hợp lệ không |
| `POST /agents/{id}/invoke` trả 422 với agent V2 | `/invoke` là cửa của agent V1 (`AgentSpec`); agent workflow V2 không có `AgentSpec` | Dùng `POST /agents/{id}/deployed-workflow-chat` |
| Node `web_search` báo "chưa cấu hình" | Thiếu `SEARCH_API_KEY` | Lấy key miễn phí ở <https://app.tavily.com> |

---

## Tài liệu khác

| Tài liệu | Nội dung |
|---|---|
| [`docs/architecture_diagram.md`](docs/architecture_diagram.md) | 4 sơ đồ: components, luồng dựng agent, luồng runtime, luồng RAG |
| [`docs/architecture.drawio`](docs/architecture.drawio) | Cùng nội dung, mở thẳng bằng draw.io |
| [`eval/results/evidence.md`](eval/results/evidence.md) | 8 test case chạy thật kèm output nguyên văn |
| [`RUN_GUIDE.md`](RUN_GUIDE.md) | Hướng dẫn khởi chạy chi tiết từng bước |
| [`docs/adr/`](docs/adr/) | 43 quyết định kiến trúc kèm lý do |
| [`JOURNAL.md`](JOURNAL.md) · [`WORKLOG.md`](WORKLOG.md) | Nhật ký phát triển |

## Giấy phép

MIT — dùng tự do cho mục đích giáo dục.
