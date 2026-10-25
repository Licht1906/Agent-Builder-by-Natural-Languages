# Deploy lên Google Cloud

Production thật (không phải demo) — quyết định khi bàn kiến trúc này. Phạm vi:
`backend` (FastAPI) + `reranker` + `frontend/` (Firebase Hosting), cộng hạ
tầng chúng cần.

## Kiến trúc

| `docker-compose.yml` service | Trên GCP | Vì sao |
|---|---|---|
| `backend` | Cloud Run | Stateless HTTP, scale-to-zero phù hợp |
| `reranker` | Cloud Run (`min-instances=1` **trong giờ**, **8 vCPU / 4 GiB / concurrency 4**) | Model 2.2 GB tải lúc start — giữ ấm để khỏi tải lại mỗi cold start. Kích thước không tuỳ ý: xem §Sizing |
| `db` (Postgres+pgvector) | Cloud SQL for PostgreSQL, private IP, **zonal** | Managed, có backup; `CREATE EXTENSION vector` chạy một lần sau khi tạo |
| `qdrant` | Compute Engine VM (`internal-services`), không public IP | Cần persistent disk, Cloud Run không mount volume được |
| `policy` (OPA) | Cùng VM `internal-services` | Stateless nhưng cần policy file cố định — gộp chung VM cho đỡ phức tạp thay vì tách Cloud Run riêng |
| `vault` | Cùng VM `internal-services`, **vẫn dev-mode** | Xem "Known gaps" bên dưới |
| `mailhog` | **Bỏ** | Dev-only. Cần SMTP relay thật, cấu hình qua `SMTP_HOST`/`SMTP_USERNAME`/`SMTP_PASSWORD` |
| `frontend/` | Firebase Hosting | SPA tĩnh; Hosting đồng thời reverse-proxy `/api/**` sang Cloud Run `backend` |

Cloud Run (`backend`) nối vào `qdrant`/`policy`/`vault` trên VM qua **Direct
VPC egress** (`--network=app-vpc --subnet=app-subnet`), egress
`private-ranges-only` — traffic ra Internet thật (OpenAI/OpenRouter/Tavily/SMTP
relay) đi thẳng, không vòng qua VPC. Không dùng Serverless VPC Access
connector: connector chạy tối thiểu hai instance e2-micro, tính tiền 24/24 và
không scale-to-zero được, nên nó sẽ là khoản duy nhất mà lịch bật/tắt bên dưới
không chạm tới được.

Toàn bộ hạ tầng chỉ chạy **08:00–20:00 giờ Việt Nam**, mọi ngày trong tuần.
Xem phần "Chi phí".

```
GitHub Actions ──build/push──> Artifact Registry
                │
                ├─ Cloud Run Job: alembic upgrade head + verify_uc1_data_surface.py
                │        │
                └─ Cloud Run: backend  ──Direct VPC egress──>  VM internal-services
                   Cloud Run: reranker                            ├─ qdrant  (persistent disk)
                                  │                                ├─ policy (OPA)
                                  └── Cloud SQL (Postgres+pgvector, unix socket)

Trình duyệt ──> Firebase Hosting ──/api/** rewrite──> Cloud Run: backend
                 (SPA tĩnh từ frontend/dist)

Cloud Scheduler ──08:00──> Cloud Function `app-scheduler` ──start──┐
                └─20:00──>            (infra/gcp/scheduler/)  ──stop──┤
                                                                      ▼
                                    Cloud SQL · VM · min-instances của cả hai Cloud Run
```

## Chi phí

Hạ tầng chỉ chạy **08:00–20:00 giờ Việt Nam, mọi ngày** (12/24). Ngoài khung
giờ đó: Cloud SQL đặt `activationPolicy=NEVER`, VM `internal-services` tắt,
`min-instances` của cả hai Cloud Run service về 0.

Ước tính, `asia-southeast1`, **chưa gồm** phần tính theo lưu lượng (request
Cloud Run, egress, token OpenAI/OpenRouter). Đây là con số tra bảng giá, không
phải hoá đơn — đối chiếu Billing sau chu kỳ đầu tiên:

| | 24/24, cấu hình cũ | 12/24, cấu hình đã hạ |
|---|---:|---:|
| Cloud SQL | `db-custom-2-7680` REGIONAL ~$307 | `db-custom-1-3840` ZONAL ~$38 |
| VM `internal-services` | `e2-standard-2` ~$51 | `e2-medium` ~$12 |
| Persistent disk | 50GB pd-ssd ~$10 | 30GB pd-balanced ~$3 |
| Cloud Run `reranker` | `min-instances=1` ~$20 | ~$10 |
| Cloud Run `backend` | `min-instances=1` ~$9 | ~$5 |
| Serverless VPC Access connector | ~$12 | **$0** (bỏ, dùng Direct VPC egress) |
| Cloud Scheduler + Function | — | ~$0 (2 job đầu miễn phí, function trong free tier) |
| Cloud NAT | ~$1 | ~$1 (tính theo VM-giờ, VM tắt thì gần như bằng 0) |
| Firebase Hosting | — | ~$0 (free tier: 10GB lưu trữ, 360MB/ngày băng thông) |
| **Tổng** | **~$410/tháng** | **~$71/tháng** |

Hai đòn bẩy tách bạch, và chúng nhân với nhau chứ không cộng: lịch bật/tắt cắt
~50% *thời gian*, hạ cấu hình cắt ~60% *giá mỗi giờ*.

Disk và storage của Cloud SQL vẫn tính tiền khi máy đã tắt — đó là lý do phần
disk giảm bằng cách đổi loại/kích thước chứ không giảm theo lịch.

### Đánh đổi đã chấp nhận

- **Bỏ Cloud SQL regional HA.** Không còn failover tự động sang zone khác;
  backup hằng ngày vẫn còn, nên mất zone là mất *thời gian khôi phục*, không
  mất dữ liệu. Một hệ thống cố ý tắt 12 tiếng mỗi đêm thì đằng nào cũng không
  hứa uptime liên tục. Quay lại bằng `SQL_AVAILABILITY=REGIONAL bash
  infra/gcp/setup.sh`.
- **Request ngoài khung giờ sẽ fail, không phải chờ.** Cloud Run service vẫn
  tồn tại và vẫn nhận request; nó cold start rồi chết ở bước kết nối DB. Nếu
  cần một trang "ngoài giờ làm việc" tử tế hơn thì phải thêm, chưa có.
- **Vault mất secret mỗi đêm** — xem "Known gaps", đây là hệ quả nghiêm trọng
  nhất và đã có bước xử lý trong chuỗi `start`.

### Đổi khung giờ

Sửa env var rồi chạy lại `schedule.sh`; nó cập nhật cả function lẫn hai
scheduler job, nên khung giờ chỉ tồn tại ở một chỗ:

```bash
WINDOW_START_HOUR=9 WINDOW_END_HOUR=17 \
WINDOW_DAYS=0,1,2,3,4 \
PROJECT_ID=$PROJECT_ID bash infra/gcp/schedule.sh
```

`WINDOW_DAYS` theo `weekday()` của Python: `0`=Thứ Hai … `6`=Chủ Nhật. Ví dụ
trên là 8h/ngày chỉ ngày làm việc (~24% thời gian, rẻ hơn nữa khoảng một nửa
so với 12/24).

### Bật/tắt bằng tay

```bash
gcloud scheduler jobs run app-start --location=$REGION   # bật ngay
gcloud scheduler jobs run app-stop  --location=$REGION   # tắt ngay
```

Tắt hẳn lịch (quay về 24/24) thì `gcloud scheduler jobs pause app-stop
--location=$REGION` rồi chạy `app-start` một lần.

Việc còn tồn sau lần deploy đầu tiên — RLS chặn seed UC-1, AC-7 chưa kiểm
chứng được trên Cloud SQL, GitHub Actions hết quota — nằm ở
[`DEPLOYMENT-TODO.md`](DEPLOYMENT-TODO.md), kèm bằng chứng và các hướng sửa.

## Sizing: reranker là chỗ duy nhất không được đoán

Phần trên nói *bao nhiêu tiền*. Phần này nói **cấu hình nào thì thứ bạn trả tiền
mới thực sự làm việc** — và reranker là chỗ duy nhất mà đoán sai không báo lỗi.

Cross-encoder là việc CPU-bound thuần, thời gian chạy tỉ lệ nghịch với số core.
Đo trên `bge-reranker-v2-m3`, passage thật từ quy chế 34 trang:

| Số ứng viên | 2 vCPU | 4 vCPU | 8 vCPU |
|---|---|---|---|
| 10 | 14.68s | 7.01s | 2.83s |
| 20 | 25.58s | 12.26s | **5.52s** |

Một lần search gửi `max(KNOWLEDGE_DENSE_TOP_K, KNOWLEDGE_SPARSE_TOP_K)` = **20**
ứng viên — RRF gộp hai nhánh prefetch chứ không cộng chúng (đếm trên index thật).
Ở 2 vCPU, cấu hình trước đây của file này, mọi search đều vượt timeout.

**Và nó hỏng im lặng.** `src/services/reranker.py` cố ý không bao giờ raise: một
reranker chết hay chậm chỉ ghi một dòng warning rồi trả lại thứ tự chưa xếp,
`reranked=False`. Nên triệu chứng không phải 500, mà là một service được trả
tiền đầy đủ để cho ra đúng kết quả của việc không có nó. Đo được:
hit@8 **19/22 → 16/22**, MRR **0.742 → 0.498**.

Ba con số phải đi cùng nhau, đổi một cái là phải xem lại hai cái kia:

| | Ở đâu | Giá trị |
|---|---|---|
| `--cpu` | `.github/workflows/deploy.yml` | 8 |
| `KNOWLEDGE_*_TOP_K` | `src/config.py` | 20 |
| `RERANKER_TIMEOUT_SECONDS` | `src/config.py` | 30 (biên an toàn, không phải mục tiêu) |

`--concurrency=4` cũng bắt buộc: mặc định Cloud Run dồn tới 80 request vào một
instance, và 80 lần rerank CPU-bound trên 8 core sẽ xếp hàng tới lúc timeout —
lúc đó con số vCPU không còn nghĩa gì.

**8 vCPU không mâu thuẫn với phần Chi phí.** Ngoài khung giờ `min-instances`
về 0, và trong khung giờ Cloud Run tính CPU theo lúc *phục vụ request* chứ
không theo lúc rỗi — mà 8 vCPU × 5.52s và 4 vCPU × 12.26s là cùng số
CPU-second. Cái vCPU cao thật sự mua là ~7s wall clock trên mỗi lượt chat.

**Còn một cần gạt nữa, và nó là cần gạt cuối.** Hạ `KNOWLEDGE_*_TOP_K` về 10
làm rerank nhanh gấp đôi, nhưng đo được **17/22, MRR 0.683** thay vì 19/22 và
0.742. Đây là đánh đổi *chất lượng*, khác hẳn mọi dòng trong bảng Chi phí phía
trên — chúng đổi tiền lấy thời gian sẵn sàng, cái này đổi tiền lấy câu trả lời
đúng. Chỉ dùng khi đã hết cách khác.


## Known gaps (cố ý, không phải sơ suất)

- **Streaming qua Firebase Hosting chưa được kiểm chứng.** `frontend/src/lib/api.js`
  đọc câu trả lời bằng `response.body.getReader()` và tách frame theo `\n\n` —
  tức Server-Sent Events. Bản build đi qua rewrite của Hosting, mà Hosting có
  CDN đứng trước và đặt giới hạn thời gian cho mỗi request (khoảng 60 giây).
  Một lượt chat dài hơn ngưỡng đó, hoặc bị CDN đệm lại, sẽ biến trace đang chạy
  thành một cục dữ liệu tới muộn — đúng thứ `vite.config.js` đã đặt
  `cache-control: no-cache, no-transform` để tránh ở dev server. `firebase.json`
  đặt cùng header đó cho `/api/**`, nhưng **giới hạn thời gian thì header không
  gỡ được**.

  Chưa test thật nên chưa biết nó có thành vấn đề hay không. Kiểm tra bằng một
  lượt chat dài hơn 60 giây sau lần deploy đầu. Nếu hỏng, đường thoát là bỏ
  rewrite và cho SPA gọi thẳng URL Cloud Run — nhưng việc đó cần sửa code
  (`const BASE = "/api/v1"` là hằng số, bundle không có `VITE_*` nào để trỏ đi
  chỗ khác) và lúc ấy `CORS_ORIGINS` mới thật sự có việc để làm.
- **VM không có external IP, nên phải có Cloud NAT.** Không phải tuỳ chọn:
  `vm-startup.sh` cần `apt-get` và `download.docker.com` để cài Docker, còn
  `docker compose up` cần kéo image qdrant/opa/vault từ Docker Hub. Thiếu NAT
  thì startup script chết câm lúc boot, và triệu chứng chỉ lộ ra ở lần SSH kế
  tiếp dưới dạng `sudo: docker: command not found` — không liên quan gì tới
  Docker. Private Google Access không thay được: nó chỉ mở API của Google.
- **Vault vẫn dev-mode.** `src/services/vault_secret_store.py` gọi thẳng
  Vault KV v2 REST API, không có lớp trừu tượng để đổi sang Secret Manager mà
  không sửa code — và `src/config.py` (§16.1) đã ghi rõ production-mode +
  auto-unseal nằm ngoài MVP. VM sinh một `VAULT_TOKEN` ngẫu nhiên (không phải
  `dev-only-vault-token` mặc định trong repo) nhưng dữ liệu vẫn in-memory: mất
  khi VM restart, không TLS nội bộ. Ghi một ticket theo dõi việc dựng Vault
  production-mode (GCS storage backend + Cloud KMS auto-unseal) trước khi hệ
  thống giữ BYODB credential thật của khách hàng.

  **Lịch tắt máy ban đêm nâng gap này từ rủi ro lý thuyết lên chuyện hằng
  ngày.** Trước đây "mất khi VM restart" là hiếm; giờ VM tắt lúc 20:00 mỗi
  ngày nên Vault mất sạch secret *mỗi đêm*. Kiểu hỏng thì tệ hơn cả việc mất:
  `scripts/seed_vault_credentials.py` đã ghi rõ — `resolve_credential_via`
  (`src/services/vault_secret_store.py`) hạ một secret thiếu thành `""` có
  chủ đích, nên `erp.query_receivables` trả `{"rows": [], "note": "chưa có kết
  nối ERP nào được cấu hình"}`, và agent — được đưa một kết quả rỗng chứ không
  phải một lỗi — trả lời rằng nó không có dữ liệu. Mọi case `correct_lookup`
  trong golden set fail theo cách đó, và không case nào nói "Vault".

  Nên chuỗi `start` của `app-scheduler` chạy Cloud Run Job `backend-vault-seed`
  (do `deploy.yml` tạo) sau khi VM lên và *trước* khi Cloud Run nhận traffic.
  Nếu job đó thiếu hoặc lỗi, function ghi một dòng `CẢNH BÁO` vào log rồi vẫn
  chạy tiếp — kiểm tra dòng đó là cách duy nhất để biết, vì bản thân hệ thống
  sẽ không báo gì:

  ```bash
  gcloud functions logs read app-scheduler --region=$REGION --gen2 --limit=50 | grep -i vault
  ```
- **Reranker không yêu cầu xác thực** trên Cloud Run — giữ đúng mô hình tin
  cậy hiện có của `docker-compose.yml` (gọi được qua tên service, không auth).
  Muốn siết lại (Cloud Run IAM + ID token) cần sửa
  `src/services/reranker.py` để đính token vào request — chưa nằm trong đợt
  hạ tầng này.
- **`NETWORK_POLICY_HOST_ALIASES`** (UC1-35, per-agent network policy) map
  hostname → tên service docker-compose cục bộ (`smtp.internal.corp: mailhog`
  trong `.env.example`). Với `mailhog` bị bỏ và mô hình chạy trên Cloud Run,
  mapping này không còn ý nghĩa như cũ — để trống, không đoán giá trị thay
  thế. Bản thân module (`src/agentspec/network_policy.py`) đã tự ghi nó chỉ
  *sinh ra artifact* compose override, chưa có `runtime`-per-agent container
  thật sự áp dụng nó, nên để trống không phá tính năng nào đang chạy.

## Setup một lần

Yêu cầu: `gcloud auth login` với quyền Owner/Editor trên project GCP đích,
`openssl` (script tự sinh mật khẩu/token).

```bash
export PROJECT_ID=<gcp-project-id>
export GITHUB_REPO=Licht1906/Agent-Builder-by-Natural-Languages   # dùng cho Workload Identity Federation
export REGION=asia-southeast1   # đổi nếu muốn region khác

bash infra/gcp/setup.sh
```

Script này (idempotent, chạy lại an toàn nếu lỗi giữa chừng):

1. Bật API, tạo Artifact Registry, VPC/subnet, Cloud Router + Cloud NAT,
   firewall. Không tạo Serverless VPC Access connector (Cloud Run dùng Direct
   VPC egress) — nếu phát hiện một connector cũ còn sót, script in ra lệnh xoá
   thay vì tự xoá.
2. Tạo Cloud SQL for PostgreSQL (private IP, zonal, `db-custom-1-3840`), tạo
   DB + user. Ghi đè bằng env var nếu cần khác: `SQL_TIER`,
   `SQL_AVAILABILITY=REGIONAL`, `VM_MACHINE_TYPE`, `VM_DISK_SIZE`.
3. Tạo VM `internal-services` (không public IP) + persistent disk, chờ
   `vm-startup.sh` cài xong Docker, push
   `infra/gcp/docker-compose.internal.yml` và
   `docs/usecase_spec/platform/authz.rego` vào VM qua `gcloud compute scp`,
   khởi động `qdrant`/`policy`/`vault`.
4. Tạo service account cho Cloud Run runtime và cho GitHub Actions deployer,
   gán IAM roles tối thiểu cần thiết.
5. Dựng Workload Identity Federation — GitHub Actions xác thực bằng OIDC
   token, không cần lưu service-account key JSON nào trong repo/secrets.
6. Đẩy `DATABASE_URL` và `VAULT_TOKEN` (vừa sinh ra) vào Secret Manager.

Cuối script sẽ in ra một danh sách biến — thêm chúng vào **Settings → Secrets
and variables → Actions** của repo trên GitHub (mục *Variables*, không phải
*Secrets*, vì đều không nhạy cảm — chỉ là ID/URL):

```
GCP_PROJECT_ID
GCP_REGION
GCP_SQL_CONNECTION_NAME
GCP_INTERNAL_VM_IP
GCP_WORKLOAD_IDENTITY_PROVIDER
GCP_DEPLOY_SA
GCP_RUN_SA
```

Cộng `GCP_SCHEDULER_URL` — `schedule.sh` in ra ở bước cuối (xem dưới). Để
trống thì `deploy.yml` bỏ qua mọi bước liên quan tới lịch và deploy 24/24 như
cũ; đó là trạng thái đúng cho tới khi bạn chạy `schedule.sh`.

Hai biến tuỳ chọn, cả hai đều có mặc định hợp lý nên bỏ qua được:

- `CORS_ORIGINS` — với Firebase Hosting rewrite thì trang và API dùng chung
  origin, nên CORS không tham gia vào đâu cả. Mặc định `deploy.yml` dùng
  `https://<project-id>.web.app`. Chỉ cần đặt tay nếu có origin khác gọi API
  từ trình duyệt (vd chạy `frontend/` ở localhost trỏ vào backend thật).
  **Đừng đặt thành chuỗi rỗng**: `"".split(",")` ra `[""]`, chặn sạch mọi
  origin, và triệu chứng là lỗi CORS khó hiểu chứ không phải "chưa cấu hình".
- `SMTP_HOST` — bỏ trống được. `src/agents/tools/builtin.py:277` raise
  `ToolUnavailableError` có kiểm soát khi thiếu, không crash lúc khởi động;
  hậu quả duy nhất là agent không dùng được tool `send_email`. Muốn gửi thật
  thì trỏ vào một relay (SendGrid/Mailgun/SES) và điền
  `SMTP_USERNAME`/`SMTP_PASSWORD` trong `.env.production`.

`FRONTEND_URL` không cần đặt tay: `deploy.yml` set nó thành URL Firebase.
Biến này quyết định nơi Google OAuth callback đá trình duyệt về
(`src/api/user_connection_routes.py:155`) — để mặc định của `src/config.py`
thì người dùng bị đưa về `http://localhost:5173` sau khi authorize.

**Bước tay bắt buộc, một lần**, in ra ở cuối `setup.sh`: bật extension
`vector` trên Cloud SQL trước khi migrate lần đầu —

```bash
PROJECT_ID=<gcp-project-id> bash infra/gcp/enable-pgvector.sh
```

Không dùng `gcloud sql connect` được: lệnh đó hoạt động bằng cách whitelist IP
client lên **public IP** của instance, mà instance này tạo với `--no-assign-ip`
nên không có public IP nào. Đường vào duy nhất là từ trong VPC, và
`enable-pgvector.sh` đi qua VM `internal-services` (đã nằm sẵn trong VPC) bằng
IAP tunnel. Nó cũng dùng `appuser` chứ không phải `postgres` — user tạo qua
Cloud SQL API được cấp `cloudsqlsuperuser`, đủ quyền `CREATE EXTENSION`, còn
`postgres` thì `setup.sh` chưa từng đặt mật khẩu.

Sau đó khai báo secret thật (API key, mật khẩu):

```bash
cp .env.example infra/gcp/.env.production
# điền OPENAI_API_KEY, OPENROUTER_API_KEY, JWT_SECRET, SECRET_STORE_KEY,
# SEED_*_PASSWORD, SMTP_USERNAME/PASSWORD, v.v.
# ĐỂ TRỐNG: DATABASE_URL, QDRANT_URL, OPA_URL, VAULT_URL, VAULT_TOKEN,
# SMTP_HOST, APP_ENV, CORS_ORIGINS, FRONTEND_URL — deploy.yml tự set những
# cái này từ hạ tầng vừa dựng.

bash infra/gcp/sync-secrets.sh
```

`sync-secrets.sh` đẩy mỗi dòng `KEY=value` còn lại thành một Secret Manager
secret tên `app-key-viet-thuong-gach-noi` (vd `OPENAI_API_KEY` →
`app-openai-api-key`). `.github/workflows/deploy.yml` tự liệt kê mọi secret
tiền tố `app-` và map ngược lại thành `--set-secrets`, nên không cần sửa
workflow mỗi khi thêm một biến môi trường mới — chỉ cần thêm dòng vào
`infra/gcp/.env.production` rồi chạy lại `sync-secrets.sh`.

## Từ đây về sau: CI/CD tự động

`git push` (hoặc merge PR) vào `main` → `.github/workflows/ci.yml` chạy
lint/test/`opa test`/frontend build như hiện tại → nếu xanh,
`.github/workflows/deploy.yml` tự chạy tiếp:

1. Build + push image `backend` và `reranker` lên Artifact Registry (tag =
   12 ký tự đầu của git SHA).
2. Chạy Cloud Run Job một lần: `alembic upgrade head` rồi
   `scripts/verify_uc1_data_surface.py` (đúng script CI đã chạy, lần này
   nhắm vào Cloud SQL thật) — dừng lại nếu bước này fail, không deploy tiếp.
3. Deploy `backend` (nối Cloud SQL qua unix socket, nối VM qua Direct VPC egress)
   và `reranker`, trỏ `RERANKER_URL` của `backend` vào URL Cloud Run thật của
   `reranker`.
4. Cập nhật Cloud Run Job `backend-vault-seed` (không chạy ở đây —
   `app-scheduler` gọi nó mỗi sáng sau khi VM lên).
5. Smoke test `GET /health`.
6. Gọi `app-scheduler?action=reconcile` — trả hạ tầng về đúng trạng thái theo
   giờ. Deploy trong giờ làm việc thì để nguyên; deploy lúc nửa đêm thì tắt
   lại thứ mà bước 2 vừa đánh thức.

## Bật Firebase Hosting (một lần, chạy được ngay sau setup.sh)

```bash
PROJECT_ID=<gcp-project-id> bash infra/gcp/setup-firebase.sh
```

Script gắn Firebase vào project GCP, tạo site mặc định
(`https://<project-id>.web.app`) và cấp `firebasehosting.admin` + `run.viewer`
cho service account deploy. Từ đó `deploy.yml` tự build và đẩy SPA mỗi lần
push.

Script gọi thẳng REST API bằng access token của `gcloud` thay vì dùng Firebase
CLI. Lý do: Firebase CLI giữ bộ token OAuth riêng, độc lập với `gcloud`, và nó
hết hạn âm thầm — `firebase login:list` vẫn in ra tên tài khoản trong khi
refresh token đã chết, nên lỗi hiện ra là `401 UNAUTHENTICATED` chứ không phải
"hãy đăng nhập lại". Một hệ xác thực ít hơn là một thứ ít hỏng hơn. (Trong CI
thì khác: `firebase-tools` ở đó dùng Application Default Credentials do
`google-github-actions/auth` ghi ra, tức vẫn là danh tính WIF.)

## Bật lịch bật/tắt (sau lần deploy đầu tiên)

Phải xếp sau vì `schedule.sh` cần Cloud Run Job `backend-vault-seed` đã tồn
tại, mà job đó do `deploy.yml` tạo.

```bash
PROJECT_ID=<gcp-project-id> REGION=asia-southeast1 bash infra/gcp/schedule.sh
```

Script deploy Cloud Function `app-scheduler` cùng hai Cloud Scheduler job
(`app-start` 08:00, `app-stop` 20:00), rồi in ra `GCP_SCHEDULER_URL` — thêm nó
vào GitHub Actions repo variables. Kiểm tra ngay, đừng đợi 20:00 mới biết:

```bash
gcloud scheduler jobs run app-stop  --location=$REGION
gcloud functions logs read app-scheduler --region=$REGION --gen2 --limit=50
gcloud scheduler jobs run app-start --location=$REGION
```

Trong log của lần `app-start`, tìm dòng re-seed Vault. Nếu là `CẢNH BÁO` thì
đọc "Known gaps" — bỏ qua nó nghĩa là UC-1 trả rỗng mỗi sáng mà không báo lỗi.

## Seed lần đầu (thủ công, không nằm trong pipeline)

Chạy một lần sau lần deploy đầu tiên — đây là seed, không phải migration nên
cố tình không tự động chạy lại mỗi lần push (tránh reset dữ liệu demo):

```bash
gcloud run jobs deploy backend-oneoff --image=<backend-image-vừa-deploy> \
  --region=$REGION --service-account=$GCP_RUN_SA \
  --add-cloudsql-instances=$GCP_SQL_CONNECTION_NAME \
  --set-secrets=<danh-sách-app-secrets-như-deploy.yml> \
  --command=python --args="scripts/init_qdrant.py"
gcloud run jobs execute backend-oneoff --region=$REGION --wait
# tương tự cho scripts/seed_kb.py, scripts/seed_uc1_demo_data.py,
# scripts/seed_vault_credentials.py — đổi --args mỗi lần
```

## Cập nhật `authz.rego`

Không nằm trong CI/CD tự động (đúng tinh thần comment sẵn có trong
`docker-compose.yml`: đổi version OPA "là một quyết định, không phải một bản
cập nhật thường quy" — thay đổi Rego semantics ở đây thay đổi quyền của mọi
tool call). Cập nhật thủ công:

```bash
gcloud compute scp docs/usecase_spec/platform/authz.rego internal-services:/opt/app/policy/authz.rego \
  --zone=$ZONE --tunnel-through-iap
gcloud compute ssh internal-services --zone=$ZONE --tunnel-through-iap \
  --command="cd /opt/app && sudo docker compose restart policy"
```

## File liên quan

- `infra/gcp/setup.sh` — provision hạ tầng một lần.
- `infra/gcp/vm-startup.sh` — startup script của VM `internal-services`.
- `infra/gcp/docker-compose.internal.yml` — subset `qdrant`/`policy`/`vault`
  chạy trên VM đó, cùng image/version pin với `docker-compose.yml` gốc.
- `infra/gcp/sync-secrets.sh` — đồng bộ `infra/gcp/.env.production` vào
  Secret Manager.
- `infra/gcp/enable-pgvector.sh` — bước tay một lần, bật extension `vector`
  qua VM `internal-services` (Cloud SQL không có public IP).
- `infra/gcp/schedule.sh` — dựng lịch bật/tắt: deploy function + hai
  Cloud Scheduler job. Chạy **sau** lần deploy đầu tiên.
- `infra/gcp/scheduler/main.py` — Cloud Function `app-scheduler`, chứa chuỗi
  bật/tắt có thứ tự và định nghĩa khung giờ.
- `infra/gcp/setup-firebase.sh` — bật Firebase Hosting, cấp quyền deploy.
- `firebase.json` / `.firebaserc` — cấu hình Hosting: SPA fallback cộng
  rewrite `/api/**` sang Cloud Run.
- `.github/workflows/deploy.yml` — pipeline CI/CD build → migrate → deploy →
  deploy-frontend → reconcile.
