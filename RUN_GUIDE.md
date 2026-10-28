# Hướng Dẫn Khởi Chạy Dự Án (Từ A đến Z)

File này hướng dẫn chi tiết các bước để khởi chạy dự án bao gồm Database (PostgreSQL trong Docker),
Backend (FastAPI) và Frontend (React + Vite) trên môi trường local (máy cá nhân).

## Yêu Cầu Cài Đặt (Prerequisites)

Trước khi bắt đầu, đảm bảo máy bạn đã cài đặt:
1. **Python 3.11+**
2. **Node.js 18+** (nếu bạn cần chạy frontend)
3. **Docker Desktop** (bắt buộc để chạy database PostgreSQL)
4. **Git** & **Make** (tùy chọn nhưng khuyến nghị)

---

## 1. Khởi Tạo Môi Trường Python & Cấu Hình

Nếu bạn chưa thiết lập môi trường ảo và file cấu hình, hãy làm theo các bước này:

```bash
# 1. Khởi tạo môi trường ảo (Virtual Environment)
python -m venv .venv

# 2. Kích hoạt môi trường ảo
# Trên Windows (PowerShell/CMD):
.\.venv\Scripts\activate
# Trên macOS/Linux (hoặc Git Bash):
source .venv/bin/activate

# 3. Cài đặt các thư viện phụ thuộc
pip install -e ".[dev]"

# 4. Copy file cấu hình
cp .env.example .env
```
*(Hãy mở file `.env` lên và điền các API KEY cần thiết như `OPENAI_API_KEY` nếu có).*

*Node `KnowledgeSearch` dùng `EMBEDDING_MODEL`, mặc định
`voyage-4,text-embedding-3-small`: Voyage được ưu tiên, OpenAI là fallback.
Điền `VOYAGE_API_KEY` để dùng Voyage; nếu key thiếu hoặc model hết allowance,
hệ thống tự chuyển sang OpenAI.*

> **Không có key embedding thì tài liệu không index được.** Từ
> [ADR-0065](docs/adr/0065-mot-stack-truy-xuat-qdrant-thang-pgvector-vi-source-ids-la-id-tu-tai-lieu.md)
> chỉ còn **một** stack truy xuất, và nó đọc vector trong Qdrant. Đường lùi
> "chỉ tìm bằng từ khoá" của ADR-0045 đã bị bỏ cùng stack pgvector, nên ingest
> **từ chối** thay vì ghi một tài liệu không có vector. Đây là chủ ý: một đường
> lùi chỉ đúng trong bản checkout không ai deploy là stack thứ hai đội lốt
> tính năng dự phòng.

Hai biến đáng chú ý trong `.env`:

| Biến | Ý nghĩa |
| --- | --- |
| `DATABASE_URL` | Trỏ tới Postgres của Docker: `postgresql://postgres:postgres@127.0.0.1:5432/app_dev` |

---

## 2. Khởi Chạy Các Dịch Vụ Nền (Docker)

Backend dial tới **sáu** dịch vụ, không phải một. `depends_on` trong
`docker-compose.yml` chỉ xếp thứ tự cho backend **chạy trong compose** — khi
bạn chạy backend từ máy mình bằng `make run`, nó không bật giúp bạn cái nào cả.
Nên bật hết bằng một lệnh:

```bash
make up
# tương đương:
# docker compose up -d --wait db qdrant reranker policy mailhog
```

`--wait` đợi tới khi mọi healthcheck xanh chứ không chỉ tới khi Docker đã tạo
container — `reranker` phải nạp xong cross-encoder 2,2 GB mới trả lời, nên
"started" và "dùng được" cách nhau khoảng hai phút ở lần chạy đầu (những lần
sau model đã nằm trong volume `hf_cache`, chỉ còn vài chục giây).

| Service | Cổng | Thiếu nó thì sao |
| --- | --- | --- |
| `db` | 5432 | Không có gì chạy. `/health` trả `"database": "error"` |
| `qdrant` | 6333 | **Upload vẫn 201, ingest chết ngầm.** Tài liệu ở trạng thái `failed`, và dấu hiệu đầu tiên là agent trả lời "tài liệu không đề cập" về đúng cái tài liệu bạn vừa tải lên |
| `reranker` | 8001 | Không sập, nhưng tìm kiếm **tệ đi thấy rõ**: `src/services/reranker.py` lùi về thứ tự RRF thô, bỏ luôn ngưỡng điểm, và chỉ để lại một dòng log |
| `policy` | 8181 | Fail **closed** ngay lời gọi tool đầu tiên — đúng thiết kế, không phải bug |
| `mailhog` | 1025/8025 | Không gửi/xem được mail thử |

Tắt cho nhẹ máy khi nghỉ: `make stop` (đỗ container lại; **không** dùng
`docker compose down` — lệnh đó xoá container).

### Sau khi container đã lên

```bash
# 1. Migration (tạo bảng)
alembic upgrade head          # hoặc: make migrate

# 2. Tạo collection trong Qdrant — idempotent, chạy lại không sao
make init-qdrant

# 3. (UC-1) Nạp golden set vào suite của agent UC-1 — chỉ cần một lần mỗi DB
# (ADR-0054 §6, EVAL-7). Idempotent: chạy lại chỉ báo "skipped". Nếu bỏ qua
# bước này, lần chạy "golden-set/run" đầu tiên tự nạp từ file, nên đây chỉ
# là cách làm trước cho chủ động, không bắt buộc.
python scripts/import_golden_set.py --agent agt_ar_assistant --file golden/uc1_ar_v3.jsonl

# 4. (tuỳ chọn) Nạp sẵn hai tủ tài liệu demo từ seed_corpus/
make seed-kb
```

Ảnh database là `pgvector/pgvector:pg16` — Postgres 16 kèm extension `vector`.
Nếu bạn đã chạy `postgres:16-alpine` từ trước: volume dùng lại được, nhưng chạy
`REINDEX DATABASE app_dev;` một lần sau khi đổi, vì hai ảnh sắp xếp chuỗi text
theo hai cách khác nhau.

> Extension `vector` **giờ không còn ai dùng**: bảng `document_chunks` đã bị
> drop ([ADR-0065](docs/adr/0065-mot-stack-truy-xuat-qdrant-thang-pgvector-vi-source-ids-la-id-tu-tai-lieu.md)),
> embedding nằm hết trong Qdrant. Extension để lại vì nó là đối tượng cấp
> database, rẻ, và xoá đi có thể làm hỏng cột `vector` do bạn tự thêm.

### ⚠️ Nếu máy bạn có PostgreSQL cài sẵn

Postgres cài trực tiếp trên Windows cũng nghe cổng `5432` và sẽ **giành cổng** với container —
triệu chứng là `password authentication failed for user "postgres"` dù mật khẩu đúng, vì bạn đang
nói chuyện với Postgres của Windows chứ không phải của Docker.

Máy này đã tắt service đó và chuyển sang khởi động thủ công. Nếu cần làm lại (PowerShell
**Run as administrator**):

```powershell
Stop-Service  -Name postgresql-x64-18 -Force
Set-Service   -Name postgresql-x64-18 -StartupType Manual   # không tự bật khi khởi động lại máy
```

Kiểm tra đang nói chuyện với đúng cái nào — Docker chạy Postgres **16**, bản cài trên Windows là 18:

```bash
docker exec -it agent-builder-by-natural-languages-db-1 psql -U postgres -d app_dev -c "select version();"
```

Muốn bật lại Postgres của Windows sau này: `Start-Service -Name postgresql-x64-18` (cũng cần quyền
admin), nhưng lúc đó phải tắt container hoặc đổi cổng, vì hai cái không dùng chung `5432` được.

---

## 3. Khởi Chạy Backend (FastAPI)

Sau khi Database đã chạy ổn định ở cổng `5432`, chúng ta có thể bật Backend. Đảm bảo bạn vẫn đang ở trong môi trường ảo `(.venv)`:

```bash
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000 --loop src.server_loop:new_event_loop
```

`make run` chạy đúng lệnh trên, nhưng **Windows không có sẵn `make`** — nếu gõ `make run` mà báo
`'make' is not recognized` thì cứ dùng thẳng lệnh `uvicorn` ở trên. (Muốn có `make` thì cài qua
`winget install GnuWin32.Make` hoặc `choco install make`, nhưng không bắt buộc.)

`--loop src.server_loop:new_event_loop` là bắt buộc trên Windows, đừng bỏ: checkpointer dùng
async psycopg, thứ không chạy được trên proactor loop mặc định của Windows.

**Thành công:** Mở trình duyệt và truy cập [http://localhost:8000/docs](http://localhost:8000/docs) để xem giao diện API Swagger.

### Nếu gặp `[WinError 10013] ... socket ... access permissions`

Lỗi này gần như luôn là **cổng 8000 đã có người chiếm** (thường là một `uvicorn` cũ bạn quên tắt).
Tìm và tắt nó:

```powershell
# Xem tiến trình nào đang giữ cổng 8000
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object -ExpandProperty OwningProcess -Unique |
  ForEach-Object { Get-CimInstance Win32_Process -Filter "ProcessId=$_" | Select-Object ProcessId, CommandLine }

# Tắt nó (thay <PID> bằng số ở trên)
Stop-Process -Id <PID> -Force
```

Hai khả năng ít gặp hơn: cổng nằm trong dải Windows tự giữ chỗ
(`netsh interface ipv4 show excludedportrange protocol=tcp`), hoặc Hyper-V/WSL đã đặt trước.
Cả hai đều xử lý bằng cách chạy ở cổng khác, ví dụ `--port 8001` — nhớ đặt luôn
`BACKEND_URL=http://localhost:8001` trong `frontend/.env` để proxy trỏ đúng chỗ.

---

## 4. Khởi Chạy Frontend (React + Vite)

Hãy mở một **tab Terminal mới** (để giữ Backend vẫn đang chạy bên kia) và làm theo:

```bash
# 1. Di chuyển vào thư mục frontend
cd frontend

# 2. Cài đặt các thư viện Node.js
npm install

# 3. Chạy server phát triển (Dev server)
npm run dev
```

**Thành công:** Mở trình duyệt và truy cập [http://localhost:5173](http://localhost:5173).

Trình duyệt chỉ gọi `/api/...` trên chính origin của nó — Vite proxy sang backend ở
`http://localhost:8000`, nên **không cần cấu hình CORS** và không có URL backend nào bị nhúng
vào bundle. Nếu backend chạy ở chỗ khác, tạo file `frontend/.env`:

```
BACKEND_URL=http://localhost:8000
```

### Đăng nhập hoặc đăng ký

Ba tài khoản có sẵn sau khi migrate: `creator@example.com` / `creator-dev-password` và
`admin@example.com` / `admin-dev-password` (V2), cộng `agent-admin@example.com` /
`agent-admin-dev-password` (UC-1, #117 — vai trò `agent_admin`, dùng để kích hoạt các tài khoản
`pending` đăng ký sau).

Nút **Get started** mở màn hình đăng ký. Kể từ UC1-01 (#117), `POST /auth/register` không còn
dùng mã mời `ADMIN_SIGNUP_CODE` (đã bỏ). Tài khoản `agent_admin` **đầu tiên** không đến từ đăng ký
— nó được migration (`d13a1d1e5b78`) seed sẵn, `agent-admin@example.com` / `agent-admin-dev-password`
theo mặc định, y hệt cách `1135e617defc` đã seed `creator`/`admin` của V2. **Mọi tài khoản đăng ký
qua form** — kể cả yêu cầu vai trò `agent_admin` — luôn ở trạng thái `pending` cho tới khi một
`agent_admin` đang active kích hoạt. Đăng ký không còn trả token — phải
`POST /auth/login` riêng sau khi tài khoản được kích hoạt.

Việc kích hoạt làm trên giao diện: đăng nhập bằng `admin` hoặc `agent_admin`, bấm nút **Tài khoản**
trên thanh trên cùng của Dashboard (con số cam bên cạnh là số người đang chờ). Tab **Chờ kích hoạt**
liệt kê cũ nhất lên trước, mỗi dòng một nút *Kích hoạt*; tab **Tất cả tài khoản** là danh bạ đầy đủ.
Hai endpoint đứng sau màn hình này là `GET /admin/users?status=pending` và
`POST /admin/users/{id}/activate` — gọi thẳng bằng curl vẫn được, màn hình chỉ là chỗ đọc `id`.

`frontend/src/pages/Register.jsx` đã cập nhật theo hợp đồng này: form giữ hai vai trò V2
(Creator/Admin — ba vai trò UC-1 chưa có màn hình riêng), thêm hai trường bắt buộc job_title/department,
bỏ hẳn ô mã mời admin, và sau khi đăng ký thành công hiển thị thông báo "tài khoản đang chờ kích
hoạt" thay vì tự đăng nhập — vì giờ không có response nào trả `access_token` để đăng nhập bằng cả.

Vai trò gắn với tài khoản và được backend kiểm ở mọi request — giao diện không có nút đổi vai trò,
vì một nút như vậy chỉ đổi được thứ frontend vẽ ra, không đổi được thứ API cho phép.

### Đi hết một vòng trên giao diện

Đăng nhập bằng tài khoản **creator**, rồi bấm **New agent**:

1. **AI Solution Designer** — mô tả bài toán bằng lời của bạn. Hệ thống hỏi lại cho tới khi đủ
   7 trường, xong thì nút *Build the specification* sáng lên. *(Bước này gọi OpenAI, nên cần
   `OPENAI_API_KEY` trong `.env`.)*
2. **Specification** — soát lại cả 7 trường, sửa được thoải mái. *Confirm* làm nó **bất biến**;
   muốn đổi ý sau đó thì phải tạo version mới.
3. **Generate workflow** — sinh graph nháp rồi mở thẳng ra canvas. Nút này ở màn *Specification*,
   và **giờ có thêm một nút y hệt ngay giữa canvas** cho ai mở Trình dựng workflow từ thanh bên mà
   chưa đi qua bước này: trước đây màn đó chỉ ghi một dòng đỏ *"Version này chưa có workflow nào"*
   — đúng nhưng bí lối, vì thứ chữa nó nằm ở trang khác. Trần của graph là **24 node**
   (`SYSTEM_LIMITS.max_steps`); trước là 12, mà 12 vốn viết cho *bước* của V1 chứ không cho *node*
   của V2 — một graph có nhánh tiêu node vào hình dạng (`end` mỗi nhánh, `if_else`, `collect`,
   `human_approval`) chứ không phải vào việc.
   > **Nếu bấm mà báo `Tài khoản model của nền tảng đã chạm trần chi tiêu…`** thì đó là dự án
   > OpenAI đã kịch trần, không phải đặc tả của bạn sai: vào
   > `platform.openai.com/settings/…/limits` nâng trần rồi bấm lại. Bản đặc tả vẫn còn nguyên.
   > Mọi lỗi từ nhà cung cấp model giờ trả **503 kèm một câu tiếng Việt** thay vì
   > `Yêu cầu thất bại (HTTP 500)` — xem `src/services/llm.py::provider_failure_of`.
4. **Canvas** — kéo thả node, kéo từ chấm màu bên phải sang chấm trái của node khác để nối, bấm
   vào đường nối để xoá. **Kéo nền canvas để di chuyển khung nhìn** (con trỏ thành bàn tay):
   bảng *Thêm bước*, Inspector và ô chat đều **nổi đè lên graph** để workflow được cả màn hình,
   nên node nằm dưới chúng thì kéo nền ra là thấy — không phải đi tìm thanh cuộn ở mép màn hình.
   Kéo nền **không** làm mất node đang chọn; bấm (không kéo) mới bỏ chọn. Danh sách lỗi đỏ ở dưới chính là thứ nút *Confirm workflow* sẽ từ chối,
   nên hết đỏ là confirm được.
5. **Sandbox** — chạy thật, xem từng node/tool/chi phí hiện dần. Tool có tác dụng phụ chỉ chạy
   bản mô phỏng. Gặp node *Human Approval* thì run dừng lại chờ bạn bấm Approve/Reject.
6. **Evaluation** — *Generate cases* viết test case từ success criteria, *Run evaluation* chấm.
7. **Approval & Deploy** — *Submit for approval* (cần đã có sandbox run + eval đạt ngưỡng). Đăng
   nhập bằng tài khoản **admin** để duyệt trong **Review queue**. Duyệt xong, creator quay lại
   *Publish*, chỉnh ai được dùng, rate limit, và lấy API key (**key chỉ hiện đúng một lần**).
   Nút **So sánh version** ngay cạnh danh sách version cho biết hai bản khác nhau chỗ nào trước khi
   bạn đổi con trỏ — rollback chính là publish một bản cũ hơn.
8. **Operations** — agent đã publish phục vụ ra sao: số lượt, số lượt chết giữa chừng, token, tiền,
   và 20 lượt gần nhất. **Chỉ tính lưu lượng thật** — lần chạy thử và chat `trial` của chính bạn bị
   loại, nếu không agent sẽ trông bận rộn đúng vào những ngày bạn test nó nhiều nhất. Dừng vì hết
   ngân sách không tính là lỗi.
9. **Chat** — agent đã publish xuất hiện ở mục *Agents you can use* dưới dashboard.

### Hai nửa của hoá đơn

Nền tảng gọi model cho **hai việc khác nhau** và giờ đo cả hai:

| | Bảng | Câu hỏi nó trả lời |
|---|---|---|
| Agent **chạy** | `run_costs` | agent tốn bao nhiêu mỗi lượt phục vụ |
| Nền tảng **dựng** agent | `platform_model_calls` | đọc phỏng vấn, soạn đặc tả, vẽ graph, viết + chấm bộ đề tốn bao nhiêu |

Nửa thứ hai trước đây **không được đo gì cả**, và nó là nửa đắt. Xem ở tab
*Chi phí* trong phòng PM, panel **"Nền tảng tiêu cho chính nó"** — xếp theo
**việc**, kèm số lần thử trung bình mỗi tài liệu (trên ~1,3 nghĩa là composer
đang trả tiền hai lần cho cùng một graph) và tỉ lệ token vào được cache.

Muốn xem nhanh bằng SQL:

```sql
SELECT purpose, count(*) AS luot, round(sum(cost_usd)::numeric, 5) AS usd,
       round(avg(attempt)::numeric, 2) AS lan_thu_tb
FROM platform_model_calls GROUP BY 1 ORDER BY 3 DESC;
```

### Phòng điều hành PM (PM control room)

Đăng nhập bằng tài khoản **PM** riêng — `pm@example.com` / `pm-dev-password` (seed bởi migration
`b3c8e47a9d15`, vai `pm`, **không phải admin**: không vào được Accounts/Platform settings/Review
queue). Vai này đăng nhập là vào thẳng control room. Admin và `agent_admin` cũng thấy nó — một mục
**PM control room** riêng trên sidebar, dưới mục *Admin*.

Control room là **sáu tab trên thanh điều hướng bên trái** (đó là chỗ duy nhất chuyển tab —
trang không lặp lại danh sách đó lần nữa), mỗi tab một câu hỏi: **Tổng quan** (cảnh báo Q1–Q4, 6 ô headline, success theo trigger, outcome,
activation) · **Run & trace** (lỗi bắt đầu ở đâu + bảng run, nút *đọc*, *→ case*) · **Chất lượng
theo version** (offline vs online, *báo cáo*, *Chấm mẫu judge*, gate meta agent) · **Chấm tay**
(màn đọc run đi qua hàng đợi, G/B, + hiệu chỉnh judge bên cạnh) · **Chi phí & độ trễ** (P50/P90,
Pause/Resume) · **Duyệt & phản hồi** (phễu duyệt Approve/Reject, thumbs theo tuần). Cửa sổ
ngày và nút **giả lập** dùng chung cho mọi tab; bấm một thanh/cảnh báo ở Tổng quan là
nhảy sang Run & trace với đúng bộ lọc đó.

Cửa sổ ngày **gõ được số bất kỳ** (1–365, Enter hoặc rời ô là áp dụng), ba nút 7/28/90 chỉ là
lối tắt. Bảng **Phễu duyệt** và **Những lần chạy đằng sau con số** cho **kéo mép cột như Excel**
— kéo để đổi rộng, bấm đúp mép để trả về mặc định; trình duyệt nhớ độ rộng cho lần sau.
Board đọc **sổ run dùng chung** `agent_runs` ([ADR-0053](docs/adr/0053-so-run-dung-chung-va-dashboard-pm-theo-chuan-ai-evaluation.md)):
từ commit đó, **mọi** run production — V2 lẫn UC-1 — để lại câu hỏi, câu trả lời và transcript
trong DB, không cần Langfuse. Các widget theo bài giảng *AI Evaluation* (`docs/PM-PLAYBOOK.md` §5):
cảnh báo Q1–Q4 với ngưỡng chốt trước (`src/pm/thresholds.py`), success theo trigger, outcome theo
taxonomy (code quyết định lúc ghi), chi phí + latency P50/P90 (kèm **Pause/Resume**), **offline vs
online theo phiên bản** (eval pass · online · judge · thumbs · expert; nút **báo cáo** mở bảng
baseline/candidate/delta/regression), phễu duyệt (**Approve/Reject**), thumbs theo tuần, **lỗi bắt
đầu ở đâu** (theo loại node / action / trace code), **hiệu chỉnh judge** (ma trận nhầm lẫn judge ×
expert, TPR/TNR/κ), gate meta agent.

Ba việc PM làm từ board:

- **Đọc run & gán nhãn** — nút **đọc** trên mỗi run, hoặc ô **cần đọc** ở hàng đầu để đi qua hàng
  đợi. Màn đọc hiện câu hỏi → trace (từng tool gọi gì, trả về gì) → câu trả lời; phím **G** = Đạt,
  **B** = Không đạt, chọn trace code, ghi một câu. Ý kiến của judge **ẩn cho đến khi bạn đã quyết**.
- **Chấm mẫu judge** — nút trên tab *Chất lượng theo version* (10 run chưa chấm, tốn token), hoặc từ terminal:

  ```bash
  python scripts/judge_production_sample.py --dry-run   # liệt kê run sẽ chấm, không tốn token
  python scripts/judge_production_sample.py --limit 10
  ```

- **→ case** — input đã điền sẵn từ trace; chỉ cần viết "đáp án đúng phải làm gì".

Người dùng cuối cũng chấm được: dưới mỗi câu trả lời trong cửa sổ chat có 👍/👎 (ghi theo
`trace_id` của run, vào `quality_scores`). Tham số `trigger` của URL không đổi; board ẩn dữ liệu
giả lập mặc định — nút **giả lập** ở góc phải bật lên.

Chưa có run thật thì sinh dữ liệu giả lập để tập dượt (có cả transcript và judge giả):

```bash
python scripts/simulate_pm_runs.py            # 4 agent "[SIM] …", ~1.4k run có trace, approval, gate run
python scripts/simulate_pm_runs.py --reset    # xoá sạch những gì script đã viết, không đụng gì khác
```

Quyết định/thumbs/nhãn/case bạn tạo trên dữ liệu giả lập vẫn là dòng thật trong DB, nhưng run giả
lập **không bao giờ được resume hay tính tiền**. Nền: [ADR-0051](docs/adr/0051-pm-control-room-trong-platform.md),
[ADR-0053](docs/adr/0053-so-run-dung-chung-va-dashboard-pm-theo-chuan-ai-evaluation.md).

### Bộ đề của agent (tab "Chấm điểm" trong Trình dựng workflow)

Từ ADR-0054, bộ test thuộc **agent** và giữ nguyên qua mọi version mới — hết cảnh
"refine xong là bộ đề rỗng". Trên tab Chấm điểm của builder:

- **Sinh test case** — như cũ, nhưng nối thêm và không sinh lại tiêu chí đã có case.
- **Nạp case** — CSV / JSONL / XLSX một schema (`input`, `expected` bắt buộc; tải file
  mẫu trong dialog). Luôn thấy **báo cáo từng dòng trước**, bấm xác nhận mới ghi.
- **Dựng lưới** — suy lưới tình huống từ spec + graph (ADR-0042 qua ADR-0054 §3):
  máy quyết ô nào tồn tại, không tốn token; mỗi ô một case `ô lưới` với lời seed
  tiếng Việt sửa được. **Chạy lưới** đo mỗi ô n lần theo zone (nguội 3 · ấm 5 · nóng 8)
  và nói thật ba trạng thái *đủ tin / chưa chắc / đang hỏng* kèm k/n; "thử thêm" tăng n.
  Sửa spec/đổi môi trường là bằng chứng cũ tự hết hạn (ô xám lại) — không có nút nào
  giữ được điểm cũ. Ô lưới **không** tính vào điểm eval thường (hai tập, hai ngưỡng).
- **Chạy chấm điểm ▾** — tất cả / chỉ case đang trượt / case đã chọn.
- Mỗi case đã chạy hiện **agent đã làm gì và trả lời gì**: hàng chip `Đã làm:` là từng lời gọi
  tool theo thứ tự (✓/✗, `~` là mock, di chuột xem tham số và kết quả), rồi `Trả lời:` gấp gọn ba
  dòng — bấm để mở hết. Bằng chứng thô vẫn nằm trong nếp gấp *Evidence* như cũ.
- Case có tiêu chí đã bị sửa khỏi spec mang chip **"tiêu chí không còn trong bản này"**
  (vẫn chạy, judge chấm theo đáp án của case); case rule-based nêu action graph không
  còn thì **không chạy** và được liệt kê là bỏ qua — mẫu số không đếm nó.

Lưu ý vòng đời: các cửa eval (kể cả lưới) chạy trên version ở trạng thái
**đã chốt workflow**; version đã duyệt/deploy là bất biến, muốn đo lại thì sửa thành
phiên bản mới. Tab PM "Chất lượng theo version" có cột **phủ lưới** đọc đúng các ô
còn bằng chứng theo khoá hiện hành.

### Trace trong Langfuse (tuỳ chọn, tự host)

Board không cần Langfuse — sổ `agent_runs` đã giữ transcript. Langfuse là **kính lúp cho một run**:
span theo từng node (kèm `node_type`, nhánh đã đi), từng tool call lồng trong node của nó, thời
gian thật, `session_id` = hội thoại, `user_id` = người gọi. Cả V2 lẫn UC-1 đều xuất (V2 qua
`src/services/tracing.py`, UC-1 qua `src/services/uc1_tracing.py` — có lọc PII), và **cùng
`trace_id`** với dòng trong `agent_runs`, nên màn đọc run có nút **mở trong Langfuse**.

```bash
docker compose -f docker-compose.langfuse.yml up -d      # 6 container: web, worker, postgres, clickhouse, redis, minio
curl http://localhost:3000/api/public/health             # {"status":"OK", ...} sau ~1 phút
```

Rồi thêm vào `.env` (key/project được file compose tạo sẵn lúc khởi động, không phải bí mật):

```env
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-agentlab-local
LANGFUSE_SECRET_KEY=sk-lf-agentlab-local
LANGFUSE_PROJECT_ID=agentlab-local
```

Khởi động lại backend. Giao diện Langfuse: `http://localhost:3000`, đăng nhập
`pm@example.com` / `pm-dev-password`. Để trống `LANGFUSE_*` là tắt — chạy y hệt, chỉ không có
trace. Trace không bao giờ chặn một lần chạy: backend Langfuse sập thì chat vẫn trả lời.

Dùng **Langfuse Cloud** cũng được: đặt `LANGFUSE_HOST` đúng region của tài khoản
(`https://us.cloud.langfuse.com` hoặc `https://cloud.langfuse.com` — key tạo ở region nào
chỉ xác thực ở region đó), key `pk-lf-…`/`sk-lf-…` của project bạn, và `LANGFUSE_PROJECT_ID`
là **id** project (chuỗi `cm…` trong URL project) để nút "mở trong Langfuse" trỏ đúng.
Nhớ: trace V2 **không** lọc PII trước khi gửi — nội dung chat sẽ rời máy.

### Xoá agent không dùng nữa

Trên dashboard, mỗi thẻ agent có nút thùng rác ở góc dưới bên phải. Hộp thoại xác nhận nói rõ những
gì mất theo: **toàn bộ version, transcript sandbox, kết quả eval và lịch sử chi phí** của agent đó.
Không hoàn tác được.

Hai điều không mất: **tài liệu bạn đã tải lên** (thuộc thư viện của bạn, không thuộc agent) và
**phiên phỏng vấn** đã sinh ra agent đó (chỉ quên mất nó từng sinh ra agent nào).

Agent **đang phục vụ** thì bị từ chối kèm câu chỉ việc phải làm: vào tab *Approval & Deploy*, gạt
công tắc tạm dừng, rồi mới xoá. Đây là chủ ý — tạm dừng thì hoàn tác được, còn xoá thì không, nên
việc xoá thứ người khác đang dùng đáng phải quyết định hai lần.

---

## 5. Tài Liệu Cho Node Knowledge Search

Một workflow dạng hỏi-đáp tài liệu chỉ trả lời được khi có tài liệu thật để đọc.

**Tải tài liệu lên ngay trên giao diện:** mở node `Knowledge Search` trên canvas,
panel bên phải có nút **Upload a document**. Tủ tài liệu vừa tải lên tự động
được tick vào `source_ids` của node đó.

Định dạng đọc được: **`.md`, `.txt`, `.pdf`, `.docx`** (tối đa 20 MB). Định dạng
khác bị từ chối ngay lúc upload chứ không phải lúc chạy.

### `source_ids` là id tủ tài liệu, không phải tên tệp

Node trỏ vào một **Knowledge Base** (tủ), không trỏ vào một tệp
([ADR-0043](docs/adr/0043-knowledge-base-as-the-addressed-unit.md) §1) — nên
thêm hay bớt tài liệu trên tủ **không** phải là một lần sửa workflow. Giá trị
trong `source_ids` là id của tủ (`"3"`), và một ref kiểu `so-tay.md` giờ bị từ
chối kèm tên nó trong thông báo.

> Nếu bạn đọc hướng dẫn cũ nói "ref chỉ là tên file" và chép file tay vào
> `data/workspace/creators/<id>/`: cách đó đã hết tác dụng. Store đọc thư mục
> trên đĩa đã bị xoá ở
> [ADR-0065](docs/adr/0065-mot-stack-truy-xuat-qdrant-thang-pgvector-vi-source-ids-la-id-tu-tai-lieu.md);
> một tệp chỉ tồn tại với hệ thống sau khi đi qua đường ingest.

### Chuyện gì xảy ra khi bạn bấm Upload

Upload trả `201` **ngay**, rồi một job nền chạy tiếp (`src/services/ingestion.py`):
bóc chữ → cắt đoạn → embed → ghi điểm vào Qdrant. Trạng thái của tài liệu đi
qua `pending → extracting → chunking → embedding → indexed`, xem được ở
`GET /knowledge-bases/{kb_id}/documents/{id}`.

Nghĩa là **upload thành công không có nghĩa là index thành công**. Tài liệu ở
`failed` có `status_detail` nói vì sao; hai nguyên nhân hay gặp nhất là Qdrant
chưa bật và thiếu key embedding. Sửa xong thì chạy lại chính tài liệu đó, không
cần tải lên lần nữa:

```bash
curl -X POST http://localhost:8000/api/v1/knowledge-bases/{kb_id}/documents/{id}/reingest   -H "Authorization: Bearer $TOKEN"
```

Xem nhanh tình trạng cả kho:

```bash
python -c "
from src.db.base import SessionLocal
from src.db.models import Document
from sqlalchemy import select
s = SessionLocal()
for d in s.scalars(select(Document).order_by(Document.id)):
    print(d.id, d.kb_id, d.file_name, d.status.value, d.chunk_count, (d.status_detail or '')[:60])
"
```

### PDF bản scan

PDF không có lớp văn bản **không** còn bị từ chối. Nó đi qua một mô hình thị
giác (RAG-06) để đọc từng trang, và tài liệu được đánh dấu `low_confidence` khi
trên 30% khối trích xuất ở mức thấp — màn hình upload cảnh báo "độ chính xác
trích dẫn có thể giảm" chứ không tự chặn index. Đường này gọi OpenRouter, nên
cần `OPENROUTER_API_KEY`.

### Trích dẫn sâu tới đâu

Đoạn trả về mang **Section Path** theo đúng cấu trúc tác giả viết, chứ không
phải tên tệp: `CHƯƠNG I > Điều 3. Thời gian và kế hoạch học tập > 3. > d)`,
kèm số trang. Đó cũng là nhãn model được yêu cầu nhắc lại trong `[...]`, nên
một câu trả lời trích dẫn được kiểm tới cấp điều khoản.

### Tài liệu của ai người nấy đọc

Store được gắn cứng `owner_id` của **chủ agent** lúc khởi tạo
(`get_knowledge_store`), và mọi truy vấn Qdrant lọc theo `owner_id` + `kb_id`.
Một `source_ids` trỏ vào tủ của người khác bị **từ chối kèm tên id đó**, không
phải im lặng bỏ qua — một nguồn bị bỏ qua âm thầm là câu trả lời trông như có
căn cứ trên mọi tủ người tạo đã gắn trong khi thực ra chỉ dựa vào một phần.

Người dùng cuối chat với agent đã publish vẫn đọc được tài liệu của agent đó —
đọc theo **chủ agent**, không phải theo người đang chat.

Sau khi workflow đã **xác nhận**, tab **Sandbox** gửi câu hỏi vào chính graph đó;
sơ đồ trên canvas sẽ sáng theo từng node đang chạy.

## 6. Gỡ Rối Nhanh

| Triệu chứng | Nguyên nhân thường gặp |
| --- | --- |
| `password authentication failed for user "postgres"` | Postgres cài trên Windows đang giành cổng 5432 — xem mục ⚠️ ở phần 2 |
| `/health` trả `"database": "error"` | Container db chưa chạy: `make up` |
| Ingest báo `ConnectError [WinError 10061]`, tài liệu ở `failed` | **Qdrant chưa bật.** `make up`, rồi `reingest` chính tài liệu đó — xem phần 5 |
| Log `Reranker unusable … falling back to unranked order` | Container `reranker` chưa bật. Không sập, nhưng kết quả tìm kiếm tệ đi thấy rõ: `make up` |
| Ingest báo thiếu embedder | Chưa có `VOYAGE_API_KEY`/`OPENAI_API_KEY`, hoặc `EMBEDDING_MODEL` để trống — từ ADR-0065 không còn đường lùi từ khoá |
| Agent trả lời "tài liệu không đề cập" về tài liệu vừa tải lên | Gần như luôn là ingest hỏng, không phải retrieval kém. Kiểm tra `status` của tài liệu trước |
| `source_ids` bị từ chối kèm tên một tên tệp | Node phải trỏ vào **id tủ tài liệu**, không phải tên tệp (ADR-0043 §1) |
| Giao diện báo **"Cần đăng nhập"** | Token hết hạn (mặc định 60 phút) — đăng nhập lại |
| Đăng nhập trả **403** ngay sau khi đăng ký | Tài khoản đang `pending` — một `admin`/`agent_admin` mở **Tài khoản → Chờ kích hoạt** trên Dashboard và bấm *Kích hoạt* |
| Đăng ký trả **409** | Email hoặc `employee_id` đó đã có tài khoản |
| Gọi API trả **429** | Chạm rate limit của Deployment — chỉnh trong tab *Approval & Deploy* |
| Frontend gọi API lỗi mạng | Backend chưa bật, hoặc không ở `http://localhost:8000` — xem `BACKEND_URL` |

Đổi bất cứ biến nào trong `.env` cũng phải khởi động lại backend: cấu hình được đọc một lần và
nhớ luôn (`@lru_cache`).

---

## 🚀 Tóm Tắt Nhanh (Cheatsheet)

Mỗi ngày khi bật máy lên code tiếp, ba lệnh ở ba tab terminal:

- **Tab 1 (dịch vụ nền):** `make up`
  → bật `db` `qdrant` `reranker` `policy` `mailhog` và đợi tới khi tất
  cả xanh. Không có `make` trên Windows thì:
  `docker compose up -d --wait db qdrant reranker policy mailhog`
- **Tab 2 (Backend):** `.\.venv\Scriptsctivate` rồi
  `uvicorn src.main:app --reload --host 0.0.0.0 --port 8000 --loop src.server_loop:new_event_loop`
  → http://localhost:8000/docs
- **Tab 3 (Frontend):** `cd frontend` rồi `npm run dev` → http://localhost:5173

Kiểm tra nhanh mọi thứ đã lên:

```bash
docker compose ps                      # sáu dòng, tất cả (healthy)
curl -s http://localhost:8000/health   # backend + database
curl -s http://localhost:8001/health   # {"model_loaded":true,"device":"cpu"}
curl -s http://localhost:6333/healthz  # qdrant
```

Nghỉ tay: `make stop`. (Đừng `docker compose down` — lệnh đó **xoá** container.)

### Đo chất lượng truy xuất

Cần `qdrant` + `reranker` đang chạy và một key embedding:

```bash
python eval/rag_benchmark.py --rerank
```

Nó chạy đúng đường production (bóc chữ → cắt đoạn → embed → RRF → rerank) trên
`data/workspace/creators/3/quy_che_dao_tao_hust.pdf` với 22 câu hỏi vàng, in
hit@8 + MRR và ghi vào `eval/results/rag_benchmark.json`. Bỏ `--rerank` để đo
riêng dense / sparse / hybrid-chưa-rerank — hữu ích khi muốn biết cross-encoder
đóng góp bao nhiêu.

### Chạy test

```bash
pytest -q          # hoặc: make test
```

Không cần chuẩn bị gì thêm. `tests/db_target.py` tự đổi `DATABASE_URL` sang một
database tên `..._test` (mặc định `app_dev_test`, tạo giúp nếu chưa có) **trước
khi** bất cứ module `src` nào kịp mở kết nối, và `require_disposable` từ chối
drop bất kỳ database nào không kết thúc bằng `_test`. Dữ liệu bạn tạo trên giao
diện trong `app_dev` không bị đụng tới.

Muốn chỉ định database khác thì đặt `TEST_DATABASE_URL` — nó thắng mọi suy diễn.

Bộ test cũng **không** cần Docker cho Qdrant hay reranker: `tests/conftest.py`
dựng `QdrantClient(":memory:")` và thay embedding/reranker bằng bản giả tất
định, nên `pytest` chạy được offline.
