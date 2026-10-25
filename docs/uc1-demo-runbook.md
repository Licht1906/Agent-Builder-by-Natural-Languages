# UC-1 — Runbook dựng một agent chạy được từ đầu

Từ database trống đến `sales.rep` hỏi *"Khách nào quá hạn trên 30 ngày?"* và nhận đúng **7 dòng**
(beat 4:45 của `spec.md` §15).

Phần môi trường (venv, Docker, migrate, chạy backend/frontend) đã có ở [`RUN_GUIDE.md`](../RUN_GUIDE.md)
— file này không chép lại, chỉ nói phần vòng đời agent mà `RUN_GUIDE.md` chưa phủ, cộng những chỗ
hỏng âm thầm đã thực sự xảy ra.

---

## 0. Đọc trước, nếu không sẽ mất việc

**Đừng chạy `pytest` / `make test` trên database đang demo.** `tests/conftest.py::_fresh_postgres_schema`
gọi `Base.metadata.drop_all()` trên đúng database mà `DATABASE_URL` trỏ tới, một lần mỗi phiên test.
Mọi agent, version, deployment bạn dựng trên UI sẽ **biến mất**. `RUN_GUIDE.md` §"Chạy test sẽ XOÁ SẠCH"
đã cảnh báo điều này; nó có thật, và nó đã xảy ra trong lúc soạn runbook này.

```bash
# Postgres thứ hai, chỉ để test, cổng 5433
docker run -d --name p065-test-db \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=app_dev \
  -p 5433:5432 postgres:16-alpine

# Và LUÔN chạy test qua nó
DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5433/app_dev' pytest tests/ -q
```

Điều gì sống sót và điều gì không, nếu bạn đã chạy nhầm:

| Sống sót | Mất |
|---|---|
| Role Postgres `agt_*` (cluster-level, `drop_all` không chạm) | Toàn bộ `uc1_agent_spec_versions` |
| Secret trong Vault (nếu Vault không restart) | `uc1_connections`, `deployments`, transcript |
| Dữ liệu demo, *nếu* suite tình cờ seed lại | Accounts bạn tự tạo (2 account seed được tạo lại) |

Khôi phục: chạy lại từ bước 1 bên dưới. Không cần dựng lại role Postgres.

---

## 1. Thứ tự khởi động

Thứ tự này có lý do, không tuỳ ý: `seed-vault` cần Vault đã sống, `seed-demo` cần schema đã migrate.

```bash
docker compose up -d db qdrant policy vault reranker   # hạ tầng
make migrate                                            # alembic upgrade head
make seed-demo                                          # dữ liệu AR dạng Olist
make seed-vault                                         # credential vào Vault  ← xem ghi chú
make verify-uc1                                         # tuỳ chọn: chứng minh grant + RLS còn nguyên
make run                                                # backend, cổng 8000
make frontend-run                                        # frontend, cổng 5173
```

**`make seed-vault` phải chạy lại sau mỗi lần restart Vault.** Vault ở §16.2 chạy dev mode, giữ secret
trong RAM. Script seed `conn/erp_readonly` = `settings.database_url` — đúng cái DSN bạn sẽ nhập tay ở
bước 3, nên chạy target này là khỏi phải gõ.

Kiểm tra nhanh trước khi mở UI:

```bash
curl -s localhost:8000/health                    # {"status":"ok",...,"database":"ok"}
curl -s localhost:8200/v1/sys/seal-status        # "sealed":false
```

---

## 2. Accounts

Từ `src/config.py` (mật khẩu là default dev; đổi qua `.env`):

| Email | Role | employee_id | Dùng để |
|---|---|---|---|
| `agent-admin@example.com` | `AGENT_ADMIN` | E001 | Dựng agent, tạo kết nối, duyệt, deploy |
| `sales.manager@demo.local` | `SALES_MANAGER` | E201 | Chat production — **23** dòng quá hạn >30d (§15 beat 4:00) |
| `sales.rep@demo.local` | `SALES_REP` | E102 | Chat production — **7** dòng quá hạn >30d (§15 beat 4:45) |

Toàn bộ bề mặt *dựng* agent (`uc1_agent_routes`, `sandbox_routes`, `connection_routes`) là
`AdminUser`-only. Cửa *dùng* agent (`uc1_agent_chat_routes`) là `CurrentUser` + kiểm tra
`audience.roles`. Nên bạn sẽ phải đổi account giữa bước 3–6 và bước 7. Điều này không khớp §15
(§15 cho `sales_manager` tự dựng agent từ 0:00) — xem bẫy #1.

---

## 3. Tạo kết nối ERP — làm **trước** khi dựng agent

Đăng nhập `agent-admin` → tab **Kết nối** → Thêm kết nối:

| Field | Giá trị |
|---|---|
| Tên | `erp_readonly` |
| Loại | `postgres` |
| Credential | giá trị `DATABASE_URL` trong `.env` |
| Allowed objects | `v_ar_open`<br>`v_customer_safe` |
| Allowed hosts | *(để trống)* |
| Chỉ đọc | ✅ |

Rồi bấm **probe**. Mong đợi: `Kết nối thành công — 2 view, 567 dòng`. **Đủ cả hai view, và sửa danh sách
này về sau là phải probe lại** — compiler chỉ tin `last_probe_objects`, xem bẫy #6.

> **Tên phải đúng `erp_readonly`.** `vault_path` được suy ra là `conn/<tên>`, còn
> `src/compiler/tools.py` **hardcode** `resolve_credential("conn/erp_readonly")`. Đặt tên khác thì
> connection vẫn tạo được, probe vẫn xanh, và agent vẫn không đọc được gì — tool đi tìm ở một path
> không ai ghi vào.

> **Probe xanh không chứng minh agent đọc được.** Probe đếm dòng bằng user trong DSN (superuser),
> còn agent đọc sau `SET LOCAL ROLE <agent_id>`. Hai đường quyền khác nhau. Từ bây giờ
> `provision_agent_role` tự kiểm tra chỗ này lúc deploy (`assert_agent_role_can_read`), nên
> sai lệch sẽ chặn deploy chứ không chờ tới lúc demo.

---

## 4. Meta-agent soạn spec

`agent-admin` → **UC-1 meta-agent** → gõ mô tả, ví dụ:

```
Tôi cần trợ lý theo dõi công nợ quá hạn và nhắc nhân viên
```

→ **Soạn cấu hình agent** → **Xác nhận & lưu thành version 1**. Đặt `agent_id` dạng `agt_...`
(cũng là tên role Postgres và tên container, **không đổi được sau khi lưu**).

---

## 5. Vá spec — bước không thể bỏ

Meta-agent soạn thiếu hai thứ, và **cả hai đều hỏng âm thầm**. Vào tab **Sửa spec**, dán đúng patch
này (một lần, không cần hai lần):

```json
{
  "audience": {
    "roles": ["sales_rep", "sales_manager"],
    "departments": ["SALES"],
    "data_scope": "own_records"
  },
  "connections": [
    {
      "id": "conn_erp",
      "type": "postgres",
      "credential_ref": "vault://conn/erp_readonly",
      "allowed_objects": ["v_ar_open", "v_customer_safe"],
      "read_only": true
    },
    {
      "id": "conn_mail",
      "type": "smtp",
      "credential_ref": "vault://conn/smtp_internal",
      "allowed_hosts": ["smtp.internal.corp"],
      "read_only": false
    },
    {
      "id": "conn_task",
      "type": "rest",
      "credential_ref": "vault://conn/tasktracker",
      "allowed_hosts": ["tasks.internal.corp"],
      "read_only": false
    }
  ]
}
```

→ **Lưu thành version 2**.

Vì sao từng field, và hậu quả nếu thiếu:

| Field | Meta-agent soạn | Hậu quả nếu để nguyên |
|---|---|---|
| `audience.roles` | chỉ `["sales_manager"]` | `sales.rep` không thấy agent trong catalog, và PEP trả `denied: caller holds no role listed in audience.roles` |
| `audience.departments` | tên phòng tự chế (`"Kinh doanh"`, …) | Lệch với `department` của hai account seed (`SALES`, `src/auth/seed.py`) → `caller department "SALES" not in audience.departments`. Khai đúng `["SALES"]`, **đừng để `[]`**: rỗng nghĩa là *không giới hạn phòng ban* nào cả (`authz.rego` dòng `count(depts) > 0`), tức là bỏ luôn lớp chặn cross-department mà chính bài demo đem ra chứng minh |
| `connections` | `[]` | `provision_agent_role` lặp trên `spec.connections` → **không GRANT SELECT gì cả** → agent deploy xong chỉ trả `permission denied for view v_ar_open`. FR-24 cũng thành vô hiệu vì `verified_objects_for_spec` không có gì để xác thực |
| `conn_mail` / `conn_task` | thiếu, hoặc bị patch cũ xoá mất | Patch là **thay cả nhánh** `connections`, nên gửi mỗi `conn_erp` là xoá hai kết nối kia. Hai capability `mail.send_reminder` / `task.create` mất connection: `_connection_id_for` trả `""` (audit row ghi `connection_id: ""`), và vì `prohibitions.external_egress: false` nên `generate_network_policy` dựng allow-list **rỗng** — container deploy ra không đi tới `smtp.internal.corp` lẫn `tasks.internal.corp` được, còn `authz.rego` chặn mọi call có `args.host` với `egress to ... blocked; host not declared in connections` |

Ba chỗ phải khớp chính xác, lệch là hỏng âm thầm:

- `credential_ref` = `vault://conn/erp_readonly` → `verified_objects_for_spec` tách tên `erp_readonly` từ đây để tìm dòng đã probe
- `id` phải **chứa** `erp` → `_connection_id_for` (`graph.py`) khớp domain của tool `erp.query_receivables` bằng `"erp" in connection.id`
- `allowed_objects` phải có `v_ar_open` → chính danh sách `provision_agent_role` lặp qua để GRANT

Ba `credential_ref` ở trên đều là path mà `make seed-vault` đã ghi sẵn (`scripts/seed_vault_credentials.py`:
`conn/erp_readonly`, `conn/smtp_internal`, `conn/tasktracker`), nên không phải tạo thêm gì trong tab **Kết nối**:
chỉ `conn_erp` cần một dòng `uc1_connections` đã probe xanh (FR-24 chỉ xác thực `allowed_objects`, mà hai
connection kia không khai object nào). `granted_views` lọc `type == postgres and read_only`, nên thêm
`conn_mail`/`conn_task` không đụng gì tới phần GRANT ở bước 6.

**Lưu ý về merge:** patch là **shallow merge ở top-level**. Gửi `audience` là thay *cả nhánh*, nên
phải khai lại `data_scope` — nó `required`, không có default, thiếu là 422 (may là fail loud, không
âm thầm nới quyền).

---

## 6. Đi qua các cổng rồi deploy

Vẫn ở `agent-admin`, trên version 2:

1. **Chat thử (sandbox)** — cần ≥1 lượt (FR-38). Xem bẫy #1: lượt này sẽ bị `denied`, vẫn ghi `COMPLETED`, cổng vẫn qua.
2. **Golden set** — cần ≥ `min_pass_rate`. Spec mặc định `0.5`, không phải 0.9 của FR-33 — xem bẫy #4.
3. **Xin duyệt** → `submit-for-approval`
4. **Duyệt** — `approval_quorum: 1` với `autonomy: propose_then_act`, nên `agent-admin` tự duyệt được
5. **Deploy** → đây mới là lúc `provision_agent_role` chạy và GRANT SELECT

Sau deploy, xác nhận grant đã thật sự có:

```sql
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'agt_overdue' ORDER BY table_name;
```

Mong đợi 3 dòng: `v_ar_open`, `v_customer_safe`, `crm_sales_assignment` — cái thứ ba là base table
cho RLS join, đến từ `_VIEW_BASE_TABLE_GRANTS`. Thiếu nó thì view `security_invoker` không đọc nổi
một dòng dù grant trên view vẫn hiện ra.

---

## 7. Test như người dùng thật

Đăng xuất, đăng nhập `sales.rep@demo.local` → **Agent của tôi** → chọn agent → hỏi:

```
Khách nào quá hạn trên 30 ngày?
```

Transcript đúng phải có:

```
agent
tools  erp.query_receivables  phạm vi: {"principal_id":"E102","scope":"own_records"}
       {'rows': [...7 dòng...], 'source': 'v_ar_open'}
agent  "... (nguồn: v_ar_open)"
```

Đối chiếu bằng SQL — đây là con số của sự thật:

```sql
SELECT sales_rep_id,
       count(*)                                      AS total_open,
       count(*) FILTER (WHERE days_overdue > 30)     AS over_30d
FROM v_ar_open WHERE sales_rep_id IN ('E102', 'E201') GROUP BY sales_rep_id;
-- E102 (sales.rep)     → 17 open,  7 quá hạn >30d
-- E201 (sales.manager) → 32 open, 23 quá hạn >30d
SELECT count(*) FROM v_ar_open;  -- 231 toàn bộ
```

Đổi sang `sales.manager@demo.local` hỏi đúng câu đó → phải ra **23**, không phải 7. Đó là beat 4:45, và là
bằng chứng delegated authority (FR-41): `RowScope` lấy từ JWT của người đang hỏi, không phải của
người tạo agent.

---

## 8. Bẫy đã biết

### #1 — Sandbox rehearsal không bao giờ chạy được tool call *(chưa sửa)*

`sandbox_routes.py` gate `AdminUser`, mà `audience.roles` của agent UC-1 luôn là role nghiệp vụ.
Không account nào vừa vào được cửa sandbox vừa nằm trong audience → mọi lượt chạy thử đều
`denied: caller holds no role listed in audience.roles`.

Trớ trêu: lượt đó vẫn ghi `COMPLETED`, nên cổng FR-38 vẫn cho qua — bạn deploy được với "bằng chứng
rehearsal" là một transcript toàn deny, đúng thứ §4.5 nói sandbox tồn tại để tránh.

`compile_spec` đã giải nửa vấn đề (ép `metadata.status = deployed` để spec draft rehearse được);
nửa danh tính thì còn trống. Hai hướng: thêm `rehearse_as` để admin chạy thử dưới danh tính một
user thuộc audience, hoặc mở builder surface cho role nghiệp vụ theo đúng §15. **Chưa chốt.**

### #2 — SSE chết im lặng khi provider lỗi *(chưa sửa)*

`_sandbox_chat_stream` đã `yield` event `thread` rồi mới gọi `run_sandbox_turn`. Exception bay ra
giữa stream nên FastAPI không đổi được thành 500, frontend không nhận `done` lẫn `error` → **spinner
quay vĩnh viễn, không có row nào vào `uc1_sandbox_runs`**.

Hay gặp nhất là 429: `AGENT_MODEL` = `qwen/qwen-2.5-7b-instruct` chỉ có **đúng một** provider trên
OpenRouter hỗ trợ tool call (Phala; Together thì không), trên free shared pool. Lỗi thật gặp phải:

```
openai.RateLimitError: 429 ... 'is_byok': False,
'limit_source': 'upstream_provider_shared_pool'
```

Xử lý tạm: chờ ~1 phút rồi thử lại (nó tự hết). Xử lý thật trước ngày demo: thêm provider key riêng
ở <https://openrouter.ai/settings/integrations> để khỏi dùng pool chung.

### #3 — Vault báo `unhealthy` nhưng vẫn tốt

`docker-compose.yml` healthcheck gọi `vault status`, mà `VAULT_ADDR` default là `https://` trong khi
dev mode chạy `http://`:

```
Error checking seal status: ... http: server gave HTTP response to HTTPS client
```

Vault hoàn toàn bình thường. Xác nhận bằng `curl -s localhost:8200/v1/sys/seal-status` → `"sealed":false`.
Sửa hẳn thì thêm `VAULT_ADDR=http://127.0.0.1:8200` vào env của healthcheck.

### #4 — `min_pass_rate` là 0.5, không phải 0.9

Có chủ ý, không phải meta-agent soạn hỏng. `src/agentspec/spec.py` ghi rõ: 5 trong 8 case
`correct_lookup` gọi tên khách hàng (`Công ty Bình Minh`, `Thành Đạt`) mà
`scripts/seed_uc1_demo_data.py` không tạo, nên chúng **không thể pass dù agent làm gì**. Hạ tạm từ
0.9 của §11.2, kèm ghi chú "Restore 0.9 once the fixture and the seed agree".

Hệ quả cần biết khi trình bày: ở mức 0.5, một version có thể qua cổng với **một nửa case an toàn
đang fail**, và `refusal`/`leak`/`injection`/`cross_tenant` nằm cùng một rổ với `correct_lookup` —
nên riêng pass rate không nói được cái fail là case độ chính xác hay case chứa lỗi. Đọc verdict
từng case trong hồ sơ duyệt, đừng đọc con số.

### #5 — Đọc `denied:` trong transcript, đừng đọc câu trả lời

Khi PEP chặn, model chỉ diễn đạt lại chữ `denied` thành lời xin lỗi. Câu *"Xin lỗi, tôi không có
quyền truy cập vào thông tin đó"* **không hardcode ở đâu trong repo** — nó là LLM paraphrase. Nhìn
badge `completed` màu xanh và câu xin lỗi to ở dưới sẽ tưởng agent hỏng; lý do thật nằm ở dòng
`denied: ...` trong bước `tools`, cùng tông xám với mọi bước khác.

### #6 — `allowed_objects chứa object không phải view` **không** có nghĩa là bạn khai bảng gốc

`v_ar_open` và `v_customer_safe` là view thật (migration `a4c2c2363959`, `security_invoker = true`).
Thấy dòng này:

```
allowed_objects chứa object không phải view: v_ar_open, v_customer_safe —
compiler chỉ chấp nhận view, không chấp nhận bảng gốc (FR-24)
```

thì đừng đi tìm bảng gốc. Cùng một câu chữ, hai nguồn hoàn toàn khác nhau — nhìn *chỗ* nó hiện ra:

| Hiện ở | `list_views` lúc đó là gì | Nghĩa thật |
|---|---|---|
| Kết quả **probe** đỏ ở tab **Kết nối** | `information_schema.views` thật trên DSN vừa nhập | View **không có** trên database đó: chưa `make migrate`, DSN trỏ nhầm database, hoặc view nằm ngoài schema `public` |
| **422** khi Chat thử / Chat production | *danh sách probe gần nhất đã xác nhận* (`verified_objects_for_spec`), **không** phải introspection | Kết nối chưa được xác thực. Compiler không hề đọc `information_schema` |

Trường hợp thứ hai là cái hay gặp, và cả ba nguyên nhân của nó đều không liên quan gì tới view:

- Dòng `uc1_connections` **tên không đúng `erp_readonly`** → `verified_objects_for_spec` tách tên từ
  `credential_ref` (`vault://conn/erp_readonly`) và không tìm thấy dòng nào
- `last_probe_status` chưa phải `connected` (chưa bấm probe, hoặc probe fail)
- Probe chạy **trước** khi sửa danh sách Allowed objects → `last_probe_objects` cũ, thiếu
  `v_customer_safe`. Sửa allowed objects là phải **probe lại**

Kiểm tra bằng SQL, đây là ba cột quyết định:

```sql
SELECT name, last_probe_status, last_probe_objects, last_probe_detail
FROM uc1_connections ORDER BY id DESC;
-- mong đợi: name = 'erp_readonly', status = 'connected',
--           objects chứa cả v_ar_open lẫn v_customer_safe

SELECT table_name FROM information_schema.views
WHERE table_schema = 'public' AND table_name LIKE 'v\_%';
```

Từ nay compiler nói thẳng chuyện này thay vì đổ cho bảng gốc — `UnverifiedConnectionObjectsError`
(`src/compiler/graph.py`) là subclass của `DisallowedBaseTableError` nên route bắt lỗi không đổi,
chỉ câu chữ đổi:

```
connection 'conn_erp' khai allowed_objects chưa được xác thực: v_ar_open, v_customer_safe —
probe gần nhất chưa xác nhận view nào (chưa probe, probe fail, hoặc chưa có kết nối nào tên
'erp_readonly'). Compiler không tự đọc information_schema (FR-24): probe lại kết nối
vault://conn/erp_readonly với đúng danh sách object rồi compile lại.
```

---

## 9. Thất bại hạ tầng giờ không còn giả dạng dữ liệu rỗng

Trước đây, credential không resolve được → tool trả `{"rows": [], "note": "chưa có kết nối ERP..."}`.
Model đọc đúng như thiết kế của cái dict đó: một tập kết quả rỗng. Rồi nó nói với người dùng
*"Hiện tại không có khách hàng nào quá hạn hơn 30 ngày. (nguồn: v_ar_open)"* — **kèm trích dẫn** —
trong khi `v_ar_open` đang có 231 dòng. Guardrail `citation_required` cho qua, vì nó kiểm tra có
trích dẫn hay không, chưa bao giờ kiểm tra đã đọc được gì hay chưa.

Sau khi sửa (`_unreadable` trong `src/compiler/tools.py`), mọi nhánh thất bại **bỏ hẳn key `rows`**,
nên không còn gì để model đếm:

```python
{"source": "v_ar_open", "error": "chưa có kết nối ERP nào được cấu hình"}
```

Ba trạng thái giờ phân biệt được, và đây là bảng để đọc transcript:

| Thấy trong transcript | Nghĩa là |
|---|---|
| `{'rows': [...], 'source': ...}` | Đọc được thật |
| `{'rows': [], 'source': ...}` | Đọc được, RLS scope ra 0 dòng — **là một sự thật về dữ liệu** |
| `{'source': ..., 'error': ...}` | Không đọc được. Không kết luận gì về công nợ từ dòng này |

`resolve_credential_via` cũng thôi gộp `VaultUnavailableError` vào `""`: Vault sập giờ báo là sự cố,
không còn giả dạng "chưa cấu hình kết nối". Đúng nguyên tắc mà header của chính module đó đã tuyên
bố từ đầu — *"an incident is not a missing reference"*.

---

## 10. Lệnh kiểm tra chất lượng

```bash
make lint        # ruff check src/ tests/ reranker/
make format      # ruff format
make typecheck   # mypy src/ reranker/
make test        # ⚠️ ĐỌC MỤC 0 TRƯỚC — dùng DATABASE_URL cổng 5433
```
