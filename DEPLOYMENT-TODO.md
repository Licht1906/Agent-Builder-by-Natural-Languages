# Việc còn lại sau lần deploy production đầu tiên

Hạ tầng đã chạy thật (xem `DEPLOYMENT.md`). Ba việc dưới đây **không chặn** hệ
thống đang chạy, nhưng hai trong ba sẽ chặn `deploy.yml` ngay khi GitHub Actions
hoạt động trở lại.

Thứ tự dưới đây là thứ tự phụ thuộc, không phải thứ tự ưu tiên: #1 và #2 cùng
một nguyên nhân gốc, và #2 chặn #3.

---

## 1. `seed_uc1_demo_data` — RLS chặn `COPY FROM`

> **Đã xử lý (DS-23).** Dữ liệu demo không còn nằm trong Postgres của nền tảng: "ERP mẫu" là một database
> riêng (`erp_mau` trên cùng instance Cloud SQL), không có RLS — phạm vi do nền tảng áp ở tầng code — và
> được seed bằng `scripts/seed_erp_mau.py` (INSERT thường, idempotent) qua job `backend-seed-erp-mau` mà
> `deploy.yml` chạy mỗi lần deploy. `scripts/seed_uc1_demo_data.py` đã gỡ. Phần dưới giữ làm lịch sử.

**Triệu chứng.** Job seed chết, dữ liệu demo UC-1 không có trên production:

```
psycopg.errors.FeatureNotSupported: COPY FROM not supported with row-level security
HINT:  Use INSERT statements instead.
```

**Chỗ hỏng.** `scripts/seed_uc1_demo_data.py:299`, hàm `_copy_crm_sales_assignment`:

```python
with cur.copy(
    sql.SQL("COPY crm_sales_assignment (customer_id, sales_rep_id, department) FROM STDIN")
) as copy:
```

**Vì sao chỉ hỏng trên production.** `crm_sales_assignment` bật RLS ở mức
`FORCE`, nên chính sách áp cả với chủ sở hữu bảng. Ở máy dev script chạy bằng
`postgres` — superuser thật, bỏ qua RLS. Trên Cloud SQL không có superuser thật:
`appuser` chỉ là `cloudsqlsuperuser`, và vai trò đó **không** bỏ qua được
`FORCE ROW LEVEL SECURITY`.

Nói cách khác, đây không phải bằng chứng RLS hỏng — nó là bằng chứng RLS đang
làm đúng việc, ở một môi trường mà script chưa từng chạy.

**Ba hướng:**

| Hướng | Đánh đổi |
|---|---|
| Đổi `COPY` sang `INSERT` (khuyến nghị) | Đúng gợi ý của chính Postgres. Chậm hơn, nhưng đây là seed vài trăm dòng chạy một lần. Làm script chạy được ở **mọi** môi trường, không cần biết nó đang là ai. |
| `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` quanh lúc seed rồi bật lại | Nhanh nhất, nhưng đụng vào đúng ranh giới bảo mật mà `schema.sql` §5 cố tình dựng. Nếu script chết giữa chừng, bảng ở trạng thái tắt RLS mà không ai biết. |
| Tạo role riêng có `BYPASSRLS` để seed | Sạch về mặt ý niệm nhưng thêm một principal nữa vào mô hình quyền, và `ALTER ROLE ... BYPASSRLS` cần superuser — thứ Cloud SQL không cho. |

Hướng đầu là thứ duy nhất không đòi hỏi môi trường phải nhượng bộ.

**Chạy lại sau khi sửa:**

```bash
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
export CLOUDSDK_CORE_PROJECT=project-d53af5b9-f901-4d5c-80d
gcloud run jobs deploy backend-oneoff \
  --image=asia-southeast1-docker.pkg.dev/project-d53af5b9-f901-4d5c-80d/app/backend:<tag-moi> \
  --region=asia-southeast1 --command=python --args="scripts/seed_uc1_demo_data.py" \
  --quiet
gcloud run jobs execute backend-oneoff --region=asia-southeast1 --wait
```

---

## 2. `verify_uc1_data_surface` — AC-7 chưa kiểm chứng, và nó chặn pipeline

**Đây là việc gấp nhất trong ba việc**, vì lý do không hiển nhiên: nó không chỉ
là một lỗ hổng kiểm chứng.

`.github/workflows/deploy.yml:136` chạy nó như **cổng chặn** của job migrate:

```yaml
--args="-c,alembic upgrade head && python scripts/verify_uc1_data_surface.py"
```

`&&` nghĩa là script này fail thì job migrate fail, và job migrate fail thì
`deploy`, `deploy-frontend`, `reconcile` đều không chạy. Nên **ngay khi Actions
hoạt động lại, pipeline sẽ đỏ ở lần chạy đầu tiên** — không phải vì Actions,
mà vì cái này.

**Triệu chứng.** Ba kiểm tra đầu pass, cái thứ tư chết:

```
OK    AC-6: agt_ar_assistant_ro cannot SELECT fin_invoice
OK    crm_sales_assignment has RLS enabled and forced
OK    agt_ar_assistant_ro has only its expected grants
psycopg.errors.InsufficientPrivilege: new row violates row-level security policy
                                      for table "crm_sales_assignment"
```

**Chỗ hỏng.** `scripts/verify_uc1_data_surface.py:160`, trong
`_insert_two_reps_with_one_customer_each` — hàm chèn dữ liệu thử cho kiểm tra
"hai principal thấy hai tập dòng khác nhau" (AC-7).

**Nguyên nhân gốc: giống hệt #1.** Script chèn dữ liệu bằng `appuser`, và policy
RLS chặn `INSERT` vì hàng được chèn không thuộc về principal nào đang có hiệu
lực. Sửa #1 sang `INSERT` **không** tự động sửa #2 — #2 đã dùng `INSERT` rồi và
vẫn bị chặn.

**Cái cần quyết.** Script này kiểm chứng rằng RLS phân tách dữ liệu đúng. Muốn
kiểm chứng điều đó thì nó phải dựng được dữ liệu ở cả hai phía — tức là phải có
một cách hợp lệ để ghi vào bảng có RLS. Hai lựa chọn thật:

- Cho script `SET LOCAL role`/`SET LOCAL` biến session mà policy dùng, rồi chèn
  từng phía dưới đúng principal của phía đó. Đây là cách kiểm chứng *trung thực
  nhất* — nó chứng minh policy hoạt động bằng cách đi qua policy, không phải
  bằng cách vòng qua nó.
- Bỏ AC-7 khỏi cổng chặn của `deploy.yml` và chạy nó riêng ở môi trường có
  superuser. Nhanh hơn, nhưng nghĩa là production không còn được kiểm chứng
  điều mà AC-7 hứa.

**Cho tới khi sửa**, nếu cần pipeline xanh gấp thì tách lệnh migrate:

```yaml
--args="-c,alembic upgrade head"
```

Nhưng phải ghi lại rằng AC-6/AC-7 không còn được kiểm tra sau mỗi lần migrate —
đó chính là thứ UC1-14 (#115) dựng ra để bảo vệ.

---

## 3. GitHub Actions hết quota — `deploy.yml` chưa từng chạy

**Triệu chứng.** Cả ba job fail trong 2 giây với **zero step**:

```
frontend:      failure, steps=0, 09:07:14 -> 09:07:16
policy:        failure, steps=0, 09:07:14 -> 09:07:16
lint-and-test: failure, steps=0, 09:07:14 -> 09:07:16
```

Không phải test fail, không phải lint fail — runner chưa từng được cấp. Điều
này đã xảy ra ở các commit **trước** đợt deploy này, nên nó không do thay đổi
nào ở đây gây ra.

**Chỗ kiểm tra:** `github.com/Licht1906/settings/billing`

Token `gh` hiện tại thiếu scope `admin:org` nên không đọc được trang billing từ
CLI. Thêm bằng `gh auth refresh -h github.com -s admin:org` nếu muốn kiểm tra
bằng lệnh.

**Hệ quả.** `deploy.yml` trigger bằng `workflow_run` với điều kiện
`conclusion == 'success'`, nên nó bị `skipped` chứ không phải fail. Không có
CI thì không có deploy tự động.

### Thứ chưa được chứng minh

Đây là phần đáng lo hơn cả quota, và nó nghịch lý: `deploy.yml` đã được sửa
**tám chỗ** trong đợt này, nhưng tất cả đều được kiểm chứng bằng cách chạy tay
các lệnh tương đương — không phải bằng cách chạy workflow. Các **lệnh** đã
đúng; **file YAML** thì chưa.

Cụ thể những phần chỉ tồn tại trong workflow và chưa chạy lần nào:

- trao đổi WIF token (`google-github-actions/auth`)
- `token_format: id_token` + `id_token_audience` để gọi `app-scheduler`
- biểu thức `${{ vars.GCP_SCHEDULER_URL != '' && '0' || '1' }}`
- vòng lặp shell phân giải secret `app-*` thành `--set-secrets`
- chuỗi trigger `workflow_run` giữa CI và Deploy
- job `reconcile` với `if: always()`

**Lần deploy đầu tiên qua pipeline nên được theo dõi sát**, và nên làm sau khi
#2 đã xong — nếu không nó sẽ dừng ở job migrate và không nói gì về sáu điểm trên.

### Trong lúc chờ: chạy tay những gì CI chạy

```bash
.venv/bin/ruff check src/ tests/          # da xanh
.venv/bin/pytest tests/ -q                # can Postgres + pgvector o local
cd frontend && npm test && npm run build && cd ..
opa test docs/usecase_spec/platform/      # opa chua cai tren may nay
```

Cài `opa` nếu cần:

```bash
curl -L -o ~/.local/bin/opa https://openpolicyagent.org/downloads/v1.19.0/opa_linux_amd64_static
chmod +x ~/.local/bin/opa
```

---

## Việc nhỏ: `qdrant-client` lệch version với server

```
UserWarning: Qdrant client version 1.19.0 is incompatible with server version 1.12.4.
Major versions should match and minor version difference must not exceed 1.
```

`requirements.txt:63` để `qdrant-client>=1.12.0` không có trần, còn
`infra/gcp/docker-compose.internal.yml` pin server `qdrant/qdrant:v1.12.4` —
cùng version mà `docker-compose.yml` gốc dùng, có chủ đích.

Hiện chỉ là cảnh báo và mọi thứ hoạt động (34 vector đã index và đọc lại được).
Nhưng khoảng cách bảy minor version là thứ sẽ thành lỗi thật vào một lúc không
báo trước. Hai cách: đặt trần cho client (`qdrant-client>=1.12,<1.13`), hoặc
nâng server lên — nhưng nâng server là quyết định giống như nâng OPA, không phải
cập nhật thường quy, vì nó đụng dữ liệu đã có trên đĩa.

---

## Deploy tay, khi cần lặp lại

Toàn bộ hạ tầng hiện tại được dựng bằng đường này, nên nó đã được kiểm chứng
đầy đủ. Dùng khi Actions còn hỏng.

```bash
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
export PROJECT_ID=project-d53af5b9-f901-4d5c-80d
export CLOUDSDK_CORE_PROJECT=$PROJECT_ID
export REGION=asia-southeast1
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
AR=asia-southeast1-docker.pkg.dev/$PROJECT_ID/app
TAG=$(git rev-parse HEAD | cut -c1-12)
SA="projects/$PROJECT_ID/serviceAccounts/gh-actions-deployer@$PROJECT_ID.iam.gserviceaccount.com"

# 1. Build (Cloud Build, khong can Docker o may — xem .gcloudignore)
gcloud builds submit --region=$REGION --service-account="$SA" \
  --default-buckets-behavior=regional-user-owned-bucket --tag "$AR/backend:$TAG" .

# 2. Secret list — sinh lai moi lan, dung ghi cung
SECRETS=$(gcloud secrets list --filter="name:app-" --format="value(name)" \
  | while read -r s; do
      printf '%s=%s:latest,' "$(echo "$s" | sed 's/^app-//' | tr '[:lower:]-' '[:upper:]_')" "$s"
    done); SECRETS=${SECRETS%,}

# 3. Deploy
gcloud run deploy backend --image="$AR/backend:$TAG" --region=$REGION \
  --service-account="app-run-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --add-cloudsql-instances="$(gcloud sql instances describe app-db --format='value(connectionName)')" \
  --network=app-vpc --subnet=app-subnet --vpc-egress=private-ranges-only \
  --port=8000 --min-instances=1 --memory=1Gi --cpu=1 --timeout=300 \
  --set-secrets="$SECRETS" \
  --set-env-vars="^@^APP_ENV=production@QDRANT_URL=http://10.10.0.2:6333@OPA_URL=http://10.10.0.2:8181@CORS_ORIGINS=https://$PROJECT_ID.web.app@FRONTEND_URL=https://$PROJECT_ID.web.app@RERANKER_URL=$(gcloud run services describe reranker --region=$REGION --format='value(status.url)')" \
  --allow-unauthenticated

# 4. Frontend
cd frontend && npm ci && npm run build && cd ..
GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/application_default_credentials.json" \
  firebase deploy --only hosting --project "$PROJECT_ID" --non-interactive
```

Bốn cái bẫy đã trả giá để biết, đừng bỏ:

- `--port=8000` — Dockerfile nghe cứng ở 8000, Cloud Run mặc định gửi vào 8080
- `--network/--subnet` — Cloud SQL chỉ có private IP, thiếu cái này là timeout
- `^@^` — `CORS_ORIGINS` chứa dấu phẩy, để mặc định thì gcloud cắt thành biến rác
- `--service-account` cho `builds submit` — project mới không còn SA mặc định

Truy vấn dữ liệu production (Cloud SQL không có public IP, phải đi qua VM):

```bash
DB_PASS=$(gcloud secrets versions access latest --secret=app-database-url \
  | sed -E 's|^postgresql://appuser:([^@]+)@.*$|\1|')
printf '%s\n' "$DB_PASS" | gcloud compute ssh internal-services \
  --zone=asia-southeast1-a --tunnel-through-iap --command="
  set -euo pipefail
  IFS= read -r pw
  sudo docker run --rm -i -e PGPASSWORD=\"\$pw\" postgres:15 \
    psql -h 10.80.0.3 -U appuser -d app_prod -c \"<cau-lenh>\"
"
```
