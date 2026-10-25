# UC-1 — Phân rã ticket

> Nguồn: [`spec.md`](spec.md). Copy từng block `UC1-XX` bên dưới thành 1 GitHub Issue riêng, theo quy ước ở `docs/agents/issue-tracker.md` (`gh issue create --title "..." --body "..."`). Số hiệu `UC1-XX` là thứ tự trong tài liệu này, không phải số issue GitHub — số issue thật do GitHub gán lúc tạo.
>
> **Lịch sử gộp (2026-08-13):** bản đầu có 55 ticket, gộp lại còn 39 — nguyên tắc gộp: cùng một PR tự nhiên sẽ làm chung và không có AC nào cần hoàn thành độc lập; **không gộp** nếu khác priority (P0 vs P2 — gộp sẽ kéo việc tuỳ chọn theo độ ưu tiên của việc bắt buộc), khác kỹ năng (backend vs frontend), hoặc là thứ vừa cố tình tách ra để không bị chìm (provenance tracker — rủi ro R2 "Cao"; DeepEval LLM-judge vs rule-based — quyết định 2026-08-13).

## Quy ước

- **Label epic**: `uc1:<epic>` (mười lăm epic bên dưới), theo đúng khuôn mẫu label `rag:*` / `v3:*` đã dùng trong repo (`gh label list`). Tạo các label này trước khi mở ticket đầu tiên của epic, ví dụ: `gh label create "uc1:policy" --description "UC-1 — OPA policy engine" --color 1d76db`.
- **Priority**: `P0` (blocker — chặn đường của epic khác hoặc của demo), `P1` (Must theo FR), `P2` (Should — cắt được nếu cháy thời gian).
- **Refs**: trỏ lại đúng ID trong `spec.md` (`FR-xx`, `NFR-xx`, `T-x`, `C-x`, `AC-x`, hoặc số mục `§n`) — không lặp lại nội dung, chỉ trỏ nguồn.
- Ticket không đánh dấu "Depends on" nghĩa là làm được ngay, song song với ticket khác.

## Thứ tự triển khai (milestone)

| Milestone | Epic | Vì sao đứng đây |
|---|---|---|
| M0 — Nền | E2, E6, E7 | `AgentSpec` schema, policy engine, data surface — ba cái còn lại đều biên dịch/thực thi dựa vào chúng |
| M1 — Vào hệ thống | E1 | Auth chặn đường mọi luồng có người dùng thật |
| M2 — Sinh agent | E3, E4 | Agent mẹ soạn spec + kết nối hệ thống nguồn |
| M3 — Biên dịch & vận hành | E5, E9, E15 | Compiler ghép spec đã xác nhận thành runtime thật; E15 (sandbox) cần graph đã compile (E5) nhưng phải xong *trước* E8 |
| M4 — Duyệt & vòng đời | E8, E10 | HITL #2 (hồ sơ giờ kèm transcript sandbox từ E15), deploy, rollback |
| M5 — Chất lượng | E11, E12 | Guardrail, eval, audit — điều kiện để tin được demo |
| M6 — Đóng gói & trình diễn | E13, E14 | Gói phần mềm + diễn tập kịch bản 8 phút |

---

## E1 — Auth & RBAC nền tảng (`uc1:auth`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-01 | Auth nền tảng: đăng ký + Argon2id + seed admin + JWT | P0 | — | FR-01, FR-02, FR-03, FR-04 |
| UC1-02 | Bind LDAP/Active Directory thay local password | P2 | UC1-01 | FR-05 (Should) |

**UC1-01 — Auth nền tảng: đăng ký + Argon2id + seed admin + JWT**
Mô tả: Toàn bộ luồng vào hệ thống trước khi có bất kỳ agent nào tồn tại — đăng ký, băm mật khẩu, seed admin đầu tiên, phát hành JWT. Gộp vì cả ba cùng một module `auth`, cùng bảng `users`, thường xong trong một PR.
Việc cần làm: `POST /auth/register` (email, mật khẩu ≥ 12 ký tự, Argon2id, `role`/`job_title`/`department`); tài khoản #1 trên một cài đặt mới tự động `agent_admin`, tài khoản sau `pending`; `POST /admin/users/{id}/activate` chỉ `agent_admin` gọi được; `POST /auth/login` trả JWT với claims `sub`/`employee_id`/`roles`/`department`; middleware xác thực trên mọi route cần đăng nhập.
Acceptance Criteria: đăng ký thiếu trường hoặc mật khẩu yếu bị từ chối; cài mới → tài khoản #1 là `agent_admin` không cần thao tác thủ công; tài khoản #2 `pending` tới khi được kích hoạt; decode JWT thấy đủ bốn claims; token hết hạn bị từ chối.

**UC1-02 — Bind LDAP/Active Directory thay local password**
Mô tả: Should-have — xác thực qua LDAP/AD của doanh nghiệp thay vì mật khẩu local.
Việc cần làm: cấu hình LDAP server trong `settings`; fallback về local password nếu LDAP không cấu hình.
Acceptance Criteria: bật LDAP thì đăng nhập bằng tài khoản AD thật thành công; tắt LDAP thì luồng local không đổi hành vi.

---

## E2 — `AgentSpec` schema & validate (`uc1:spec`)

Bản lề của toàn bộ platform. Mọi epic khác biên dịch hoặc thực thi dựa trên đầu ra của epic này.

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-03 | Validate `AgentSpec`: Pydantic model + secret detector + guardrail baseline | P0 | — | §7, FR-13, FR-22, `../platform/agentspec.schema.json` |
| UC1-04 | State machine vòng đời version (`draft → … → retired`) | P0 | UC1-03 | §13 |

**UC1-03 — Validate `AgentSpec`: Pydantic model + secret detector + guardrail baseline**
Mô tả: Lớp validate duy nhất mà agent mẹ, compiler, và API đều gọi qua — ba mối lo (hình dạng đúng schema, không lọt secret, guardrail baseline không gỡ được) cùng chạy trên một lời gọi validate, gộp vì tách ra sẽ phải validate spec ba lần ở ba chỗ khác nhau.
Việc cần làm: Pydantic model khớp `agentspec.schema.json`, `additionalProperties: false` cưỡng chế; heuristic/regex phát hiện chuỗi giống credential chạy trên spec đã serialize trước khi ghi DB; server force-merge baseline guardrail (union, không phải overwrite) sau khi agent mẹ trả về, trước khi hiển thị cho người dùng sửa.
Acceptance Criteria: `agentspec.yaml` parse thành công; spec thêm trường lạ hoặc thiếu trường bắt buộc bị từ chối rõ lý do; spec có DSN/API key giả bị từ chối; `PATCH` cố xoá một baseline guardrail entry bị từ chối, không âm thầm bỏ qua.

**UC1-04 — State machine vòng đời version (`draft → … → retired`)**
Mô tả: Cưỡng chế đúng các chuyển trạng thái ở §13 — bao gồm điều kiện gộp mới cho `draft → pending_approval` (HITL #1 + sandbox + DeepEval, xem E15/E11).
Việc cần làm: enum trạng thái + bảng chuyển hợp lệ; mỗi transition kiểm tra điều kiện tiên quyết; transition không hợp lệ trả lỗi có cấu trúc.
Acceptance Criteria: cố `deploy` một spec chưa `approved` bị chặn; sửa spec đã `deployed` luôn tạo version mới, không sửa tại chỗ.

---

## E3 — Agent mẹ soạn spec (`uc1:metaagent`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-05 | LangGraph node `draft_spec` + retry loop khi output không hợp lệ | P0 | UC1-03 | FR-10, FR-11, §11 (rủi ro R1) |
| UC1-06 | Hiển thị `clarifications` nổi bật trên form | P1 | UC1-05 | FR-14 |
| UC1-07 | Tool catalog allow-list — từ chối tool không tồn tại | P0 | UC1-03 | FR-15 |

**UC1-05 — LangGraph node `draft_spec` + retry loop khi output không hợp lệ**
Mô tả: Nhận mô tả tự nhiên + `<tool_catalog>`/`<connection_catalog>`/`<role_catalog>`/`<principal>`, trả về `AgentSpec` draft đúng schema; nếu không qua validate (UC1-03) thì tự retry kèm lỗi validate. Gộp vì retry loop là một phần của cùng lời gọi, không phải một tính năng đứng riêng.
Việc cần làm: render Jinja template `../platform/meta-agent-system-prompt.md`; gọi LLM structured output; ép `metadata.status = "draft"` bất kể model trả gì (Hard rule #6 trong prompt); bắt `ValidationError`, đưa message lỗi vào lượt gọi tiếp theo, giới hạn cứng số lần retry.
Acceptance Criteria: input là ví dụ "Worked example" trong prompt → output khớp cấu trúc `agentspec.yaml`; `metadata.status` luôn `"draft"`; ép model trả spec thiếu trường → hệ thống tự sửa trong ngưỡng retry, vượt ngưỡng thì báo lỗi dễ hiểu, không crash.

**UC1-06 — Hiển thị `clarifications` nổi bật trên form**
Mô tả: `clarifications` (field/question/why) hiển thị ngay trên form, không phải ẩn trong log.
Việc cần làm: API trả `clarifications` tách khỏi spec chính; UI render từng clarification cạnh trường liên quan.
Acceptance Criteria: mô tả một câu → thấy 3-6 clarification hiển thị đúng vị trí trường tương ứng.

**UC1-07 — Tool catalog allow-list — từ chối tool không tồn tại**
Mô tả: Mọi `capabilities[].tool` phải nằm trong catalog thật; agent mẹ không được phát minh tool.
Việc cần làm: catalog cố định (code); validator đối chiếu từng `tool` trong spec với catalog trước khi cho lưu.
Acceptance Criteria: spec chứa `erp.query_all` (không có thật) bị từ chối ở tầng API, không chỉ ở tầng UI.

---

## E4 — Kết nối & Vault (`uc1:connections`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-08 | Kết nối: credential → Vault + dry-run probe | P0 | — | FR-20, FR-21, FR-23 |
| UC1-09 | Compiler từ chối `allowed_objects` không phải view | P0 | UC1-03 | FR-24 |

**UC1-08 — Kết nối: credential → Vault + dry-run probe**
Mô tả: Toàn bộ luồng nhập credential — từ Vault tới xác nhận nó chạm đúng chỗ. Gộp vì cùng một hành trình người dùng (nhập credential → thấy ngay kết quả probe), thường xây trong một PR. Chốt secret store là **HashiCorp Vault (KV v2)** — xem §6.2 `spec.md`; SOPS+age bị loại vì hợp cho secret tĩnh biết trước lúc deploy, không hợp luồng người dùng nhập credential qua UI lúc runtime.
Việc cần làm: container Vault trong `docker-compose.yml` (dev-mode cho demo, production-mode+auto-unseal là nâng cấp ngoài MVP); `install.sh` chạy `vault operator init`/unseal một lần; `POST /connections` tách khỏi mọi endpoint có LLM trong đường đi, ghi secret qua Vault KV v2, trả về `vault://conn/<name>`, không echo secret; `POST /connections/{id}/probe` — với Postgres: liệt kê view trong `allowed_objects`, đếm dòng từng view, timeout ngắn.
Acceptance Criteria: bắt request/response toàn bộ luồng tạo spec bằng agent mẹ — secret không xuất hiện ở payload nào đi qua LLM; probe với kết nối UC-1 thật trả đúng `2 view, 1.284 dòng` (khớp §15); probe credential sai trả lỗi rõ ràng, không treo.

**UC1-09 — Compiler từ chối `allowed_objects` không phải view**
Mô tả: Least privilege thực thi ở đây, không phải bằng lời hứa của model.
Việc cần làm: query `information_schema.views` tại thời điểm probe/compile; so khớp `allowed_objects`; base table trong danh sách → compile fail.
Acceptance Criteria: spec khai `allowed_objects: [orders]` (bảng gốc) bị từ chối; `allowed_objects: [v_ar_open]` được chấp nhận.

---

## E5 — Compiler: spec → runtime (`uc1:compiler`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-10 | Spec → LangGraph graph + OPA policy bundle | P0 | UC1-03, UC1-12 | §7 |
| UC1-11 | Provenance tracker trong LangGraph state | P0 | UC1-10 | §10.3, rủi ro R2 (**Cao** — không gộp với ticket khác) |

**UC1-10 — Spec → LangGraph graph + OPA policy bundle**
Mô tả: Dịch `AgentSpec` đã duyệt thành một `StateGraph` thi hành được, đồng thời phát ra policy bundle nạp cho `authz.rego`. Gộp vì cả hai đầu ra tới từ cùng một pass compile trên cùng một spec.
Việc cần làm: mỗi capability → tool binding; connection → client khởi tạo từ `credential_ref` (resolve qua Vault tại runtime, không tại compile time); đóng gói `spec` (`status: deployed`) làm input cố định cho PEP, versioned theo `agent_id` + version.
Acceptance Criteria: `agentspec.yaml` compile thành công thành graph chạy được, không chứa tool nào ngoài `capabilities[]`; PEP nhận đúng spec đã compile, không phải spec draft; version mới không ảnh hưởng policy bundle của version đang chạy.

**UC1-11 — Provenance tracker trong LangGraph state**
Mô tả: Đánh dấu `provenance.from_untrusted_data = true` khi agent đọc dữ liệu từ DB/tài liệu/API, lan sang mọi tool call phát sinh sau đó cùng lượt — nếu thiếu, T2 (phòng thủ prompt injection) không bao giờ bắn dù `authz.rego` viết đúng. **Không gộp với ticket khác** — rủi ro R2 đánh dấu Cao, cần một ticket không thể vô tình bị bỏ dở giữa chừng của việc khác.
Việc cần làm: wrap kết quả trả về từ mọi tool `scope: read` bằng cờ provenance; propagate cờ xuyên suốt state của lượt hội thoại; đính `provenance` vào mọi request gửi PEP.
Acceptance Criteria: test bắt buộc — câu hỏi chứa chỉ thị ẩn đọc từ một dòng dữ liệu → `mail.send_reminder` bị PEP deny với lý do `"write action derived from untrusted content"`.

---

## E6 — Policy engine / OPA (`uc1:policy`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-12 | Land `authz.rego` làm PEP sidecar | P0 | — | §6.2, `../platform/authz.rego` |
| UC1-13 | Viết `authz_test.rego` (15 case) + CI gate | P0 | UC1-12 | §11.3, §20 |

**UC1-12 — Land `authz.rego` làm PEP sidecar**
Mô tả: `authz.rego` đã viết sẵn — ticket này là đưa nó vào chạy thật như một service sidecar mà runtime gọi trước mọi tool call.
Việc cần làm: đóng gói OPA làm sidecar container; endpoint nhận đúng input shape ở đầu file `authz.rego`; runtime gọi PEP và tôn trọng `effect` trả về — "runtime là dumb executor".
Acceptance Criteria: gọi thử với input mẫu từ header `authz.rego` trả đúng `decision.effect`; runtime không có code path nào bỏ qua PEP.

**UC1-13 — Viết `authz_test.rego` (15 case) + CI gate**
Mô tả: Bộ test phủ các nhánh ở §11.3 (role hợp lệ/không hợp lệ, `data_scope` ba mức, tự duyệt, hash mismatch, budget cạn, glob `deny_tools`, cờ untrusted), cộng CI job chạy `opa test` trước merge — gộp vì viết case vô nghĩa nếu không gắn CI ngay, và ngược lại.
Việc cần làm: viết case theo từng nhánh `deny contains` trong `authz.rego`, đặt tên rõ nhánh đang test; thêm step CI, fail build nếu case đỏ.
Acceptance Criteria: `opa test` xanh cục bộ và trong CI; mỗi `deny contains` rule có ít nhất một case dương tính và một âm tính; PR làm hỏng một case → CI đỏ, không merge được.

---

## E7 — Data surface / Postgres (`uc1:data`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-14 | Áp dụng `schema.sql` + script verify trong CI | P0 | — | §9, `schema.sql`, AC-6, AC-7 |
| UC1-15 | Seed dữ liệu demo: Olist + `fin_invoice` tổng hợp | P1 | UC1-14 | §9, §16.2 |

**UC1-14 — Áp dụng `schema.sql` + script verify trong CI**
Mô tả: DDL đã viết sẵn — chạy migration thật, cộng script biến ba dòng kiểm chứng thủ công ở §10.4 thành test tự động chạy mỗi lần schema đổi. Gộp vì verify vô nghĩa nếu tách khỏi lần áp dụng đầu tiên.
Việc cần làm: migration chạy `schema.sql`; runtime kết nối bằng service account low-privilege, `SET LOCAL ROLE agt_ar_assistant_ro` mỗi transaction; runtime phát `SET LOCAL app.principal_id/app.department/app.scope` từ JWT + quyết định OPA trước mỗi query; script SQL/psycopg chạy case AC-6/AC-7 trong CI sau mỗi migration.
Acceptance Criteria: `SET LOCAL ROLE agt_ar_assistant_ro; SELECT * FROM fin_invoice;` → permission denied; hai principal khác nhau query `v_ar_open` nhận đúng hai tập dòng khác nhau; script fail nếu một PR vô tình GRANT thêm quyền hoặc tắt RLS.

**UC1-15 — Seed dữ liệu demo: Olist + `fin_invoice` tổng hợp**
Mô tả: Olist không có khái niệm công nợ — `fin_invoice` sinh tổng hợp, có chủ đích giữ cột nhạy cảm để chứng minh chúng không lọt ra.
Việc cần làm: script sinh `fin_invoice` hợp lý từ `orders`; sinh `crm_sales_assignment` gán khách hàng cho sales rep; đủ dữ liệu để demo trả đúng số liệu ở §15 (23 dòng manager, 7 dòng rep).
Acceptance Criteria: sau seed, câu hỏi "khách nào quá hạn trên 30 ngày" trả đúng số dòng dùng trong kịch bản demo.

---

## E8 — Duyệt & triển khai (`uc1:approval`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-16 | Approval queue UI + diff view + transcript sandbox | P0 | UC1-04, UC1-39 | FR-30, FR-31, FR-38 |
| UC1-17 | Approval gates: quorum + DeepEval + sandbox bắt buộc | P0 | UC1-04, UC1-29, UC1-30, UC1-38 | FR-32, FR-33, FR-38 |
| UC1-18 | Vòng đời role Postgres: deploy sinh, suspend thu hồi | P0 | UC1-14, UC1-17 | FR-34, FR-35 |

**UC1-16 — Approval queue UI + diff view + transcript sandbox**
Mô tả: HITL #2 — `agent_admin` xem hàng chờ, diff, và transcript của lượt chat thử gần nhất (E15) trước khi ký duyệt; không còn phải suy đoán hành vi agent từ spec tĩnh.
Việc cần làm: `GET /approvals/queue`; `GET /agents/{id}/versions/{v}/diff?against={v-1}`; UI diff dạng field-level; nhúng transcript sandbox gần nhất vào cùng màn hình hồ sơ.
Acceptance Criteria: sửa một trường trong spec → diff hiển thị đúng đường dẫn trường đó; hồ sơ duyệt hiển thị đúng transcript của lượt chat thử gần nhất (AC-12).

**UC1-17 — Approval gates: quorum + DeepEval + sandbox bắt buộc**
Mô tả: Ba điều kiện chặn cùng nằm trên transition `draft → pending_approval` / `pending_approval → approved` của cùng một state machine (UC1-04) — gộp vì tách ra sẽ phải sửa cùng một hàm transition ba lần ở ba PR khác nhau. Two-person rule cho `autonomy: autonomous` đã ép ở tầng JSON Schema (`allOf` cuối `agentspec.schema.json`); DeepEval và sandbox không đạt thì chặn `submit-for-approval` (không phải chặn `deploy` — xem sửa FR-33, §8.1 bước 7).
Việc cần làm: đếm approval distinct approver trước khi transition sang `approved`; chặn version `autonomous` với `approval_quorum < 2` ngay từ lúc lưu spec; `POST .../submit-for-approval` kiểm tra: golden set (UC1-29+UC1-30) đạt `min_pass_rate`, và tồn tại ≥ 1 lượt sandbox (UC1-38/39); log kết quả từng case vào hồ sơ duyệt.
Acceptance Criteria: spec `autonomy: autonomous, approval_quorum: 1` bị từ chối lưu; cùng một người duyệt hai lần không đủ quorum; golden set dưới 0.9 hoặc chưa sandbox lần nào → `submit-for-approval` trả lỗi có cấu trúc, liệt kê rõ lý do; `deploy` không tự chạy lại eval.

**UC1-18 — Vòng đời role Postgres: deploy sinh, suspend thu hồi**
Mô tả: Tạo và thu hồi service account/PG role là hai đầu của cùng một vòng đời quyền — gộp vì cùng chạm một bảng role, thường cùng module.
Việc cần làm: tự động tạo role Postgres theo `metadata.id` lúc deploy, GRANT đúng theo `connections[].allowed_objects`; endpoint `POST /agents/{id}/suspend` revoke ở tầng nhanh nhất có thể (PEP cache invalidation hoặc REVOKE trực tiếp).
Acceptance Criteria: hai agent khác nhau deploy ra hai role Postgres khác nhau, không thừa quyền lẫn nhau; sau suspend, request tiếp theo bị từ chối trong < 1s đo bằng đồng hồ thực.

---

## E9 — Vận hành agent con (`uc1:runtime`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-19 | Query công nợ quá hạn có trích dẫn nguồn | P0 | UC1-10, UC1-14 | FR-40 |
| UC1-20 | `data_scope` enforcement qua `SET LOCAL` từ danh tính người hỏi | P0 | UC1-14 | FR-41 (trọng tâm — không gộp) |
| UC1-21 | Tool ghi: `mail.send_reminder` + `task.create`, đều qua hàng chờ duyệt | P0 | UC1-12, UC1-16 | FR-42, FR-43 |
| UC1-22 | Mọi tool call qua Policy Enforcement Point trước khi thực thi | P0 | UC1-12 | FR-44 |
| UC1-23 | Toàn vẹn approval: `action_hash` binding + approver ≠ requester | P0 | UC1-12 | FR-45, FR-46 |

**UC1-19 — Query công nợ quá hạn có trích dẫn nguồn**
Mô tả: Câu hỏi nghiệp vụ cơ bản của UC-1, dùng `erp.query_receivables` + `guardrails.output.citation_required`.
Việc cần làm: tool binding cho `erp.query_receivables`/`erp.query_customer`; prompt yêu cầu trích dẫn khi trả lời số liệu.
Acceptance Criteria: câu trả lời về công nợ luôn kèm trích dẫn; không có câu trả lời số liệu "trần" không nguồn.

**UC1-20 — `data_scope` enforcement qua `SET LOCAL` từ danh tính người hỏi**
Mô tả: Trọng tâm của cả bài toán — hai người dùng chung agent thấy hai tập dữ liệu khác nhau (§10.1). **Không gộp** — đây là điều duy nhất đáng nói nhất của thiết kế này, không nên chìm trong một ticket lớn hơn.
Việc cần làm: runtime lấy `employee_id`/`department` từ JWT của người đang hỏi (không phải người tạo agent) mỗi request; set GUC trong cùng transaction với query.
Acceptance Criteria: AC-2 — `sales_rep` và `sales_manager` hỏi cùng câu, hai phiên song song, nhận kết quả khác nhau đúng phạm vi phụ trách.

**UC1-21 — Tool ghi: `mail.send_reminder` + `task.create`, đều qua hàng chờ duyệt**
Mô tả: Hai tool `scope: write` duy nhất của UC-1, cùng một pattern PEP-gated — gộp vì implementation gần như giống hệt nhau, khác mỗi điều kiện phụ.
Việc cần làm: `mail.send_reminder` chỉ ghi vào hàng chờ, không gọi SMTP trực tiếp — SMTP thật chỉ chạy sau khi có quyết định `approve`; `task.create` gọi PEP với `action.args.assignee_id`, tôn trọng `effect: require_approval` khi `assignee_id != $principal.employee_id` (điều kiện đã có sẵn trong `agentspec.yaml`).
Acceptance Criteria: yêu cầu gửi email nhắc nợ → nằm ở hàng chờ, MailHog không nhận gì tới khi approve; agent tự tạo task giao cho chính người hỏi → tạo ngay; giao cho đồng nghiệp → vào hàng chờ duyệt.

**UC1-22 — Mọi tool call qua Policy Enforcement Point trước khi thực thi**
Mô tả: Không có tool nào chạy tắt qua PEP, kể cả tool `scope: read`.
Việc cần làm: wrapper bắt buộc bọc quanh mọi lời gọi tool trong graph compiler (UC1-10); test kiểm tra "mọi tool trong catalog đều đi qua PEP" bằng cách introspect graph.
Acceptance Criteria: thêm một tool mới vào catalog mà quên bọc PEP → test tích hợp bắt được.

**UC1-23 — Toàn vẹn approval: `action_hash` binding + approver ≠ requester**
Mô tả: Hai luật toàn vẹn của cùng một quyết định approval, cùng nằm trong PEP (`authz.rego` dòng 250-256) — gộp vì cùng code path ghi/kiểm approval.
Việc cần làm: tính `action_hash` từ toàn bộ `action.args` trước khi trình duyệt, PEP so khớp hash tại thời điểm thực thi; API tạo approval ghi `approver_sub` từ JWT của người bấm duyệt (không nhận từ client), PEP kiểm tra lại như lớp phòng thủ kép.
Acceptance Criteria: duyệt một action, sửa `amount` trước khi thực thi → PEP deny vì hash không khớp; người tạo action tự bấm duyệt hành động của chính mình → bị từ chối cả ở API lẫn PEP.

---

## E10 — Vòng đời & rollback (`uc1:lifecycle`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-24 | Version mới mỗi lần sửa spec, giữ bản cũ | P0 | UC1-04 | FR-50 |
| UC1-25 | Rollback về version `approved` cũ | P2 | UC1-24 | FR-51 (Should) |
| UC1-26 | Kênh pull-only tới control plane cho template/patch | P1 | — | FR-52 |

**UC1-24 — Version mới mỗi lần sửa spec, giữ bản cũ**
Mô tả: UI/API cho việc tạo version thấy được, so sánh được.
Việc cần làm: `GET /agents/{id}/versions`; mỗi sửa spec luôn insert version mới, không update tại chỗ.
Acceptance Criteria: sửa spec 3 lần → 3 version độc lập truy vấn lại được, version cũ không đổi nội dung.

**UC1-25 — Rollback về version `approved` cũ**
Mô tả: Should-have — dời con trỏ `deployed_version` về version cũ khi bản mới có lỗi. Giữ riêng khỏi UC1-24 vì khác priority (P2 so với P0) — gộp sẽ kéo việc tuỳ chọn theo độ ưu tiên bắt buộc.
Việc cần làm: `POST /agents/{id}/rollback`; chỉ chấp nhận target version có status `approved`/từng `deployed`.
Acceptance Criteria: rollback đổi đúng con trỏ, không tạo version mới, không cần duyệt lại.

**UC1-26 — Kênh pull-only tới control plane cho template/patch**
Mô tả: Lần duy nhất hệ thống khách hàng liên hệ control plane nhà cung cấp (§4.7, §12.4) — chỉ pull, không gửi dữ liệu nghiệp vụ.
Việc cần làm: client pull template/patch có ký số; xác minh chữ ký trước khi áp dụng; log mỗi lần gọi (chỉ định danh template/build, không kèm payload nghiệp vụ).
Acceptance Criteria: bảng §12.4 khớp thực tế — bắt traffic ra ngoài lúc tạo agent/agent trả lời câu hỏi → 0 request tới control plane.

---

## E11 — Guardrails & eval (`uc1:eval`)

Bốn ticket dưới đây **giữ nguyên tách rời**, không gộp — `UC1-29`/`UC1-30` vừa được tách có chủ đích (quyết định 2026-08-13) để đường LLM-judge và đường rule-based không lẫn vào nhau.

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-27 | Guardrail pipeline: 3 input + 3 output guardrail | P0 | UC1-10 | §11.1 |
| UC1-28 | Golden set `golden/uc1_ar_v3.jsonl` — 23 case | P0 | — | §11.2 |
| UC1-29 | DeepEval runner cho nhóm LLM-judge (`faithfulness`/`answer_relevancy`) | P0 | UC1-28 | §11.2, FR-33 |
| UC1-30 | Rule-based checker cho 3 nhóm còn lại (đọc transcript, không qua LLM) | P0 | UC1-28, UC1-38 | §11.2, FR-33 |

**UC1-27 — Guardrail pipeline: 3 input + 3 output guardrail**
Mô tả: `prompt_injection_detector`, `pii_redactor`, `topic_classifier` ở input; `pii_masker`, `citation_required`, `confidence_router` ở output — bảng §11.1.
Việc cần làm: mỗi guardrail là một node/hook riêng trong graph; `confidence_router` dưới ngưỡng 0.65 → `escalate_to_human`.
Acceptance Criteria: câu hỏi chứa PII giả → bị redact trước khi tới LLM; câu trả lời có PII giả → bị mask trước khi tới người dùng.

**UC1-28 — Golden set `golden/uc1_ar_v3.jsonl` — 23 case**
Mô tả: File chưa tồn tại — theo đúng phân bổ ở bảng §11.2 (8 tra cứu đúng, 5 từ chối đúng, 4 rò rỉ cột nhạy cảm, 3 prompt injection, 3 phân quyền chéo). Mỗi case gắn cờ `judge: llm | rule` để router (UC1-29/30) biết đi đường nào.
Việc cần làm: 8 case "tra cứu đúng" theo format DeepEval chuẩn (`input`/`expected_output`/`retrieval_context`); 15 case còn lại mang trường máy-kiểm-được thay vì văn xuôi kỳ vọng — ví dụ `forbidden_columns` cho leak_check, `expected_refusal_signals` cho refusal, hai `principal` khác nhau + kỳ vọng `row_filter.scope` cho phân quyền chéo.
Acceptance Criteria: 23 case tồn tại, đúng nhóm, đúng metric, đúng cờ `judge`; 15 case rule-based không chứa `expected_output` dạng văn xuôi tự do.

**UC1-29 — DeepEval runner cho nhóm LLM-judge (`faithfulness`/`answer_relevancy`)**
Mô tả: Chỉ chạy trên 8 case "tra cứu đúng" — nhóm duy nhất thực sự cần một LLM giám khảo.
Việc cần làm: DeepEval runner chuẩn, cấu hình LLM judge; đọc `golden_set_ref` từ spec, lọc case có `judge: llm`.
Acceptance Criteria: chạy 8 case, mỗi case có điểm `faithfulness`/`answer_relevancy` kèm lý do DeepEval sinh ra.

**UC1-30 — Rule-based checker cho 3 nhóm còn lại (đọc transcript, không qua LLM)**
Mô tả: `refusal_correctness`, `leak_check`, prompt-injection-không-write, phân quyền chéo — cả bốn có đáp án đúng/sai máy kiểm được, không đưa qua LLM judge (cùng nguyên tắc với phòng thủ prompt injection ở §10.3).
Việc cần làm: đọc transcript sandbox (UC1-38) của mỗi case; `leak_check` = regex/string-match `forbidden_columns`; `refusal_correctness` = match `expected_refusal_signals`; prompt-injection case = assert không tool `scope:write` nào có `effect:allow`; phân quyền chéo = so `row_filter.scope` thực tế với kỳ vọng.
Acceptance Criteria: chạy 15 case, kết quả xác định 100% giống nhau giữa hai lần chạy liên tiếp.

Gate tổng hợp trên cả 23 case (`min_pass_rate = 0.9`, chặn `submit-for-approval`) thuộc UC1-17 (E8) — không lặp lại ở đây.

---

## E12 — Observability & audit (`uc1:observability`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-31 | Observability: Langfuse trace + structured log | P1 | UC1-10 | §14 |
| UC1-32 | `audit_log` + hash chain wiring từ PEP | P0 | UC1-12, UC1-14 | §14, T8 |

**UC1-31 — Observability: Langfuse trace + structured log**
Mô tả: Trace từng bước LangGraph (token, latency, chi phí) và log JSON theo request — gộp vì cùng là wiring quan sát vận hành, không phải bằng chứng bắt buộc như audit log (UC1-32, giữ riêng vì nặng ký hơn — an toàn/kiểm toán, không chỉ observability).
Việc cần làm: instrument graph bằng Langfuse callback, self-hosted, bật `pii_scrub_traces`; middleware log JSON lines với `request_id`/`agent_id`/`principal_id`/`tool`/`effect`, đủ để join với `audit_log` bằng `request_id`.
Acceptance Criteria: một lần chạy sinh đủ trace trong Langfuse dashboard, không chứa PII thô (kiểm bằng câu hỏi chứa PII giả rồi soi trace); một request sinh log line có đủ 5 trường tối thiểu.

**UC1-32 — `audit_log` + hash chain wiring từ PEP**
Mô tả: DDL đã có sẵn trong `schema.sql` — runtime thực sự ghi vào `audit_log` mỗi quyết định PEP.
Việc cần làm: sau mỗi lời gọi PEP, insert một dòng `audit_log` với `effect`/`reasons`/`action_hash` từ `decision` object; không có code path nào bỏ qua ghi log.
Acceptance Criteria: AC-7 — `v_audit_integrity` trả rỗng ở trạng thái bình thường; sửa tay một dòng `audit_log` rồi query lại → trả ra đúng dòng gãy.

---

## E13 — Đóng gói & triển khai (`uc1:packaging`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-33 | `docker-compose.yml` single-node + `install.sh` | P1 | tất cả epic vận hành | NFR-04 |
| UC1-34 | `docker-compose.airgap.yml` + import artifact ký số | P2 | UC1-33 | NFR-05 |
| UC1-35 | NetworkPolicy deny-all + allowlist từ `connections[]` | P0 | UC1-10 | C1, T5 |

**UC1-33 — `docker-compose.yml` single-node + `install.sh`**
Mô tả: Cài xong từ máy trắng dưới 15 phút (NFR-04), theo cấu trúc gói ở §12.1.
Việc cần làm: `install.sh` sinh `.env`/master key/seed admin/migrate DB/init Vault; `docker-compose up -d` khởi động đủ service ở bảng §6.2.
Acceptance Criteria: AC-10 — bấm giờ từ `git clone` tới màn hình đăng ký dưới 15 phút trên máy sạch.

**UC1-34 — `docker-compose.airgap.yml` + import artifact ký số**
Mô tả: Chế độ ngoại tuyến hoàn toàn (§12.3). Giữ riêng khỏi UC1-33 vì P2, không nên chặn đường P1.
Việc cần làm: đóng gói artifact `.tar` ký bằng cosign/minisign; import script không cần mạng ngoài.
Acceptance Criteria: cài đặt thành công trên máy cắt mạng hoàn toàn sau khi có artifact.

**UC1-35 — NetworkPolicy deny-all + allowlist từ `connections[]`**
Mô tả: `external_egress: false` phải là cơ chế có hiệu lực, không phải cờ trên UI. Giữ riêng khỏi UC1-33 vì P0 an toàn, không nên chìm trong ticket đóng gói chung.
Việc cần làm: sinh NetworkPolicy per-agent từ `connections[].allowed_hosts` lúc deploy; deny-all mặc định.
Acceptance Criteria: AC-8 — `docker exec` vào container agent, `nslookup` domain ngoài allowlist → không resolve được.

---

## E14 — Demo & nghiệm thu (`uc1:demo`)

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-36 | Diễn tập kịch bản demo 8 phút | P1 | mọi epic vận hành, E15 | §15 |
| UC1-37 | Checklist AC-1..AC-12 chạy full trước hội đồng | P0 | UC1-36 | §18 |

**UC1-36 — Diễn tập kịch bản demo 8 phút**
Mô tả: Chạy thử toàn bộ §15 ít nhất một lần trước ngày trình bày thật, đúng timing từng phút — bao gồm beat sandbox ở phút 2:45.
Việc cần làm: chuẩn bị hai tài khoản demo (`sales_manager`, `sales_rep`) sẵn dữ liệu đúng; chuẩn bị sẵn câu prompt injection ở phút 6:30; bấm giờ toàn bộ.
Acceptance Criteria: chạy hết 10 bước trong §15 không lỗi, đúng số liệu kỳ vọng (23 dòng / 7 dòng), trong khung 8 phút.

**UC1-37 — Checklist AC-1..AC-12 chạy full trước hội đồng**
Mô tả: Chốt cuối — mọi tiêu chí nghiệm thu ở §18 phải xanh, không chỉ những cái lộ diện trong demo.
Việc cần làm: chạy tuần tự AC-1 → AC-12, ghi lại bằng chứng (log/lệnh SQL/thời gian bấm) cho từng cái.
Acceptance Criteria: 12/12 AC xanh, có bằng chứng lưu lại.

---

## E15 — Chạy thử / sandbox (`uc1:sandbox`)

Mới thêm 2026-08-13, chèn giữa E5 (compiler) và E8 (duyệt) — xem §4.5 trong `spec.md`. Tồn tại để tạo **bằng chứng** cho admin, không phải để cách ly side-effect (write đã luôn qua `approval_matrix` bất kể sandbox hay production).

| # | Title | Priority | Depends on | Refs |
|---|---|---|---|---|
| UC1-38 | Sandbox chat: thread riêng + transcript + ngân sách | P0 | UC1-10 | FR-36, FR-37, FR-39 |
| UC1-39 | Gate xin duyệt: chặn `submit-for-approval` nếu chưa có lượt sandbox nào | P0 | UC1-38 | FR-38, AC-11 |

**UC1-38 — Sandbox chat: thread riêng + transcript + ngân sách**
Mô tả: Người tạo chat với agent đã compile nhưng chưa `deployed`, bằng chính danh tính của họ, trong thread tách biệt production, ghi transcript đầy đủ, dùng chung ngân sách runtime thật. Gộp ba mối lo vì cả ba đều là một phần của việc đứng lên cùng một endpoint sandbox chat.
Việc cần làm: `POST /agents/{id}/versions/{v}/sandbox/chat` (SSE); checkpointer LangGraph dùng thread id namespace `sandbox:` khác `prod:`, route qua đúng compiler output (UC1-10); `GET /agents/{id}/versions/{v}/sandbox/runs` ghi từng bước graph đã chạy (node, tool call, args, kết quả, token, latency, chi phí ước tính); áp cùng `authority.budget` cho graph dù chạy ở thread nào, vượt trần → lỗi có cấu trúc.
Acceptance Criteria: chat thử và chat production không chia sẻ lịch sử; một lượt "khách nào quá hạn trên 30 ngày" tạo transcript liệt kê đúng tool đã gọi và chi phí khác 0; ép vượt `max_tool_calls_per_session` trong sandbox → dừng đúng lúc, lỗi có cấu trúc giống hệt runtime thật.

**UC1-39 — Gate xin duyệt: chặn `submit-for-approval` nếu chưa có lượt sandbox nào**
Mô tả: Không được xin duyệt nếu chưa từng chat thử — cùng nguyên tắc "không sandbox thì không xin duyệt" mà V1 đã áp dụng, giờ áp cho UC-1 (FR-38). Giữ riêng khỏi UC1-38 vì đây là thay đổi trên state machine (UC1-04/UC1-17), không phải trên endpoint sandbox.
Việc cần làm: `POST /agents/{id}/versions/{v}/submit-for-approval` kiểm tra tồn tại ≥ 1 row trong sandbox runs (UC1-38) trước khi cho chuyển `pending_approval`; phối hợp với UC1-17 cho điều kiện DeepEval.
Acceptance Criteria: AC-11 — gọi `submit-for-approval` trên version chưa từng sandbox → lỗi có cấu trúc, không chuyển trạng thái.
