# Prompt dựng Slide thuyết trình & Thumbnail — "Agent Builder by Natural Languages"

File này chứa **prompt sẵn để copy-paste** cho một AI (Claude, GPT, Gamma,
Midjourney/DALL·E…) nhằm sinh ra:

1. **Bộ slide thuyết trình** (Phần 2) — đầy đủ, đúng trọng tâm, có pain point,
   user, scope, kiến trúc, feature.
2. **Thumbnail** (Phần 3) — đúng mô tả dự án, thân thiện, gây hấp dẫn.

> **Cách dùng:** luôn dán **Phần 1 (Fact sheet)** trước, rồi dán tiếp prompt ở
> Phần 2 hoặc Phần 3. Fact sheet là nguồn dữ kiện duy nhất — nó chặn model bịa
> số liệu, và mọi con số trong slide phải truy được về đây.

---

## Phần 1 — FACT SHEET (dán kèm mọi prompt)

```text
=== DỮ KIỆN DỰ ÁN — NGUỒN DUY NHẤT, KHÔNG ĐƯỢC BỊA THÊM ===

TÊN & ĐỀ BÀI
- Sản phẩm: AI Agent Builder .
- Một câu: nhân viên văn phòng mô tả công việc bằng tiếng Việt, hệ thống phỏng vấn
  lại cho đủ ý, dựng thành một workflow LangGraph chạy được, chạy thử trong sandbox,
  tự sinh test case rồi tự chấm, và chỉ cho publish sau khi có người duyệt.
- Bản chất: một META-AGENT — agent dùng để dựng ra agent khác. Đóng gói cài đặt
  tại chỗ (on-premise).

PAIN POINT (tầng đề bài — vì sao có dự án)
- Mỗi đội sản phẩm muốn có agent riêng nhưng thiếu kỹ sư AI.
- Dựng một agent (chọn tool, viết prompt, nối luồng) mất nhiều tuần.
- Hệ quả: nhu cầu tự động hoá dồn ứ ở một nhóm rất nhỏ người biết làm.

PAIN POINT (tầng khách hàng — use case UC-1 "Trợ lý công nợ phòng Kinh doanh",
doanh nghiệp thương mại ~300 nhân sự, phòng Kinh doanh 18 sales_rep + 3
sales_manager, tổ CNTT 4 người, ERP chạy PostgreSQL nội bộ)
- P1: Trưởng phòng gộp báo cáo công nợ thủ công từ ERP + Excel + email —
  3–4 giờ mỗi sáng thứ Hai.
- P2: Nhân viên kinh doanh không có quyền query database, phải nhờ tổ CNTT —
  20–30 request ad-hoc/tuần, mỗi request ngốn 30–60 phút của IT.
- P3: Nhắc nợ trễ vì phát hiện muộn — trung bình chậm 5–8 ngày sau ngày đáo hạn.
- P4: Rủi ro lộ dữ liệu khi IT export Excel gửi cho sales — mất kiểm soát file
  sau khi nó rời hệ thống.
- Vì sao cách thường không giải được: cấp quyền ERP thẳng cho sales thì vi phạm
  least privilege; dựng dashboard BI thì chết ở câu hỏi ad-hoc và không có hành
  động; dùng chatbot SaaS thì dữ liệu công nợ rời khỏi doanh nghiệp — không được
  phê duyệt.

USER / ACTOR
- Người tạo agent (creator / agent_admin — tổ CNTT): mô tả nhu cầu, sửa spec,
  gắn tài liệu & credential, chạy thử, xin duyệt.
- Admin duyệt (approver): duyệt spec trước khi deploy, xem diff giữa hai version,
  bật/tắt (suspend) agent.
- Người dùng cuối agent con (sales_manager / sales_rep / sinh viên…): chỉ hỏi và
  nhận trả lời, trong đúng phạm vi dữ liệu của mình.
- PM / người quản lý chất lượng: đọc phòng điều hành PM, gán nhãn run, biến run
  sai thành golden case.
- Agent mẹ (dịch vụ nội bộ): sinh spec nháp. KHÔNG có quyền deploy.
- Agent con: thực thi trong đúng giới hạn spec đã ký, KHÔNG có quyền riêng —
  mỗi request nó mượn danh tính của người đang hỏi (delegated authority, đối lập
  với ambient authority).

RÀNG BUỘC KHÁCH HÀNG (C1–C6) — mỗi tính năng phải neo vào một ràng buộc
- C1: Dữ liệu nghiệp vụ không rời khỏi hạ tầng doanh nghiệp trong mọi trường hợp.
- C2: Mỗi nhân viên chỉ xem công nợ của khách hàng mình phụ trách.
- C3: Mọi email gửi ra ngoài phải có người có thẩm quyền duyệt trước.
- C4: Agent tuyệt đối không được ghi/sửa/xoá dữ liệu trong ERP.
- C5: Mọi truy cập dữ liệu để lại vết kiểm toán không sửa được.
- C6: Tổ CNTT xem và sửa được cấu hình agent trước khi nó chạy.

KIẾN TRÚC — BA MẶT PHẲNG
- CONTROL PLANE (phía nhà cung cấp): license, template registry, bản cập nhật đã
  ký số. Mũi tên đi lên là MỘT CHIỀU và chỉ mang artifact đã ký — không mang dữ
  liệu nghiệp vụ.
- BUILD PLANE (trong hạ tầng khách hàng, chỉ agent_admin): Auth & RBAC, agent mẹ,
  duyệt + compile.
- RUNTIME PLANE (trong hạ tầng khách hàng): agent con, policy engine, Vault + audit.
- Điểm nhấn: AGENT MẸ CŨNG NẰM TRÊN MÁY KHÁCH HÀNG. Nhà cung cấp không giữ bản
  sao nào của dữ liệu, cấu hình agent, hay lịch sử hội thoại.

KIẾN TRÚC — THÀNH PHẦN
gateway: Traefik + TLS · web: React/Vite (bản chạy) — Next.js (bản đóng gói) ·
api: FastAPI (96 route, prefix /api/v1) · metaagent: LangGraph (sinh AgentSpec) ·
compiler: Python (spec → LangGraph graph + policy bundle + tool binding) ·
runtime: LangGraph worker pool · policy: OPA sidecar (authz.rego, ~15 case
authz_test.rego) · vault: HashiCorp Vault KV v2 · llm: vLLM hoặc Ollama (cục bộ)
· data: PostgreSQL + pgvector, Redis, Qdrant · obs: Langfuse (trace, cost,
latency — tắt thì không mất gì, "degrade, never break").

MÔ HÌNH LLM (cấu hình mặc định UC-1, local hoàn toàn)
- Agent mẹ & agent con: Qwen2.5-7B-Instruct AWQ (chạy vừa 6GB VRAM).
- Embedding: bge-m3 (đa ngữ, hợp tài liệu tiếng Việt). Rerank: bge-reranker-v2-m3.
- Bản V2 đang chạy dùng OpenAI gpt-4o-mini + embedding voyage-4 / text-embedding-3-small.
- Chuyển sang cloud thì UI cảnh báo rõ rằng ràng buộc "dữ liệu không rời hạ tầng"
  không còn được đảm bảo.

ARTIFACT TRUNG TÂM — AgentSpec
- Agent mẹ KHÔNG SINH CODE. Nó sinh một bản đặc tả khai báo mà con người đọc,
  sửa, ký và version được. Compiler mới biến spec thành runtime.
- Hệ quả: không LLM nào tự ý viết code chạy trong hệ thống khách hàng; mọi năng
  lực của agent con nằm gọn trong một file `git diff` được giữa hai version.
- Bảy nhóm trường trên form UI map 1-1 vào spec: đối tượng sử dụng (audience) ·
  phân quyền sử dụng (data_scope) · agent được làm gì (capabilities) · agent
  không được làm gì (prohibitions) · quyền hạn (authority.autonomy +
  approval_matrix) · guardrails bắt buộc (baseline không gỡ được) · kết nối hệ
  thống (connections).
- credential_ref chỉ là CON TRỎ `vault://conn/erp_readonly`, không phải secret;
  API có bộ phát hiện hình dạng secret để từ chối spec chứa secret thật.
- connections[].allowed_objects chỉ liệt kê VIEW, không phải bảng gốc — least
  privilege áp bằng schema, không bằng lời hứa của model.

LUỒNG END-TO-END — 10 BƯỚC
1 Đăng ký/đăng nhập (Argon2id; role + chức vụ vào JWT claims)
2 Nhập yêu cầu bằng ngôn ngữ tự nhiên (rate limit theo user)
3 Agent mẹ sinh spec nháp (structured output; guardrail baseline inject cứng phía server)
4 Sửa + xác nhận config — **HITL #1**, có diff view
5 Xin quyền kết nối — credential → Vault; **dry-run probe** trả về đúng những
  object agent sẽ chạm (ví dụ: 2 view, 1.284 dòng)
6 Compile — spec → graph + policy bundle; chưa deploy, chưa nhận traffic thật
7 Chạy thử trong sandbox — chat thử thật + transcript (node, tool, input/output,
  token, latency, chi phí) + DeepEval smoke test; không đạt thì chặn bước kế
8 Duyệt triển khai — **HITL #2**, quorum theo approval_quorum (autonomous thì
  bắt buộc ≥2, two-person rule); hồ sơ duyệt kèm transcript sandbox
9 Deploy — container riêng, service account scoped, NetworkPolicy deny-all
10 Vận hành & vòng đời — PEP cho mọi tool call; bump version thì quay về bước 4

TÍNH NĂNG ĐÃ CHẠY (8 khối, đều ✅)
1 AI Solution Designer — mô tả tự nhiên → specification 7 trường, tự hỏi lại chỗ thiếu
2 Workflow Generator — chọn 1 trong 5 template rồi để AI điền cấu hình từng node
3 Workflow Canvas — 8 node type, nối/sửa/xoá, validate trả problems[], **sửa luồng bằng lời**
4 Workflow Runtime — LangGraph, state, condition, pause/resume, execution trace
5 Tool cốt lõi — Knowledge Search (PDF/DOCX), MCP, Web search
6 Sandbox — chạy thử, xem từng node/tool/input/output, token + chi phí thật
7 Automatic Evaluation — sinh case từ spec, rule-based check + LLM judge, gate chặn deploy
8 Publish & quản lý — version, so sánh, HITL duyệt, deployment, rate limit, quyền truy cập
- 8 node type: agent_llm · knowledge_search · web_search · mcp · if_else · while ·
  human_approval · end.
- Node `collect`: đọc tham số bắt buộc của action rồi hỏi người dùng đúng những
  cái còn thiếu — không phải prompt do model viết (ADR-0052).
- Trong sandbox: node không có side effect (knowledge_search, web_search) chạy
  thật; node có side effect (mcp) dùng mock — chạy thử trên bản giả lập của chính
  tài liệu mình thì không chứng minh được gì.

RAG (ADR-0045, hybrid)
- Cắt đoạn theo cấu trúc + ngữ nghĩa (semantic breakpoint percentile 85).
- Hybrid: BM25 + cosine, hợp nhất bằng RRF, bỏ trùng. Vector nằm trong Postgres
  qua pgvector — KHÔNG dùng ChromaDB.
- Không có key embedding thì vẫn chạy, lùi về xếp hạng bằng từ khoá (BM25); cái
  mất là recall, không phải là node ngừng hoạt động.
- Đổi model embedding thì tự index lại toàn bộ — vector hai model khác nhau
  không so sánh được, kể cả gần đúng.

BẢO MẬT & GUARDRAILS
- Baseline bắt buộc (union với đề xuất của agent mẹ; người dùng thêm được, gỡ
  KHÔNG được): input — prompt_injection_detector, pii_redactor, topic_classifier;
  output — pii_masker, citation_required, confidence_router (dưới 0.65 →
  escalate_to_human).
- Row-level security + view least-privilege + audit hash-chain (v_audit_integrity).
- Điều kiện trong approval_matrix có dạng {field, op, value} cố định, KHÔNG phải
  chuỗi biểu thức tự do — vì để policy engine evaluate thứ do LLM sinh ra chính
  là lỗ hổng mà cả thiết kế này đang tránh.
- Gọi một agent chưa được cấp quyền trả về 404, cố ý không phải 403 — để người lạ
  dò không biết agent đó có tồn tại hay không.

ĐÁNH GIÁ (Evaluation)
- Golden set UC-1: 23 case, yêu cầu min_pass_rate = 0.9.
  · Tra cứu đúng (8 case) — faithfulness, answer_relevancy — LLM judge **theo lối
    DeepEval** (một lần gọi judge trên seam model của nền tảng, không phải gói
    `deepeval` — đừng nói "điểm DeepEval", số này không so được với số công bố
    dưới tên đó)
  · Từ chối đúng (5) — refusal_correctness — rule-based
  · Rò rỉ cột nhạy cảm (4) — leak_check — rule-based
  · Prompt injection (3) — không tool write nào được PEP allow — rule-based
  · Phân quyền chéo (3) — đọc row_filter đã áp trong transcript — rule-based
- Nguyên tắc: an toàn không nên phụ thuộc phán đoán của một LLM khi có thể kiểm
  tra cứng bằng code.
- Gate của V2: pass_rate phải vượt threshold mới cho deploy (ví dụ chạy thật:
  8/10 = 0.8 ≥ 0.6 → meets_threshold).

PHÒNG ĐIỀU HÀNH PM (ADR-0051, ADR-0053) — 6 tab
overview (có gì đang cháy) → runs & traces (run nào) → quality by version (bản mới
có tốt hơn không) → review (mình nợ hội đồng cái gì) → cost & latency (đang tốn
bao nhiêu) → approvals & feedback (ai đang chờ mình, người dùng nói gì).
- Một bảng `agent_runs` dùng chung cho cả hai pipeline; `run_costs` là hoá đơn.
- Ba loại phán xét tách bạch, cố ý không gộp: **label** (chuyên gia: Đạt/Không
  đạt + trace code + ghi chú) · **thumbs** (người nhận câu trả lời) · **judge**
  (mô hình chấm trên mẫu). Calibration = judge đối chiếu label trên cùng tập run.
- Trace code là từ của chuyên gia cho lý do hỏng: missing_lookup,
  fabricated_content, tool_failure_hidden…

BẰNG CHỨNG ĐÃ CHẠY THẬT (dùng làm slide demo/kết quả)
- Vòng đầy đủ: cán bộ phòng đào tạo mô tả "sinh viên hay hỏi em về quy chế đào
  tạo, em muốn một trợ lý đọc file PDF quy chế rồi trả lời, trích rõ điều khoản,
  câu nào quy chế không nói thì phải nói là không có" → hệ thống dựng workflow
  6 node → hỏi "em trượt 10 tín chỉ thì có bị cảnh báo không?" → agent trả lời
  "Theo Điều 19, khoản 1, mục a (trang 15)…".
- Câu trả lời thật khác: "Theo Điều 19, khoản 2, trang 15: chương trình chuẩn
  tối đa 14 tín chỉ; ELITECH hoặc hợp tác quốc tế tối đa 18 tín chỉ."
- UC-1: cùng một câu "Khách nào quá hạn trên 30 ngày?" → sales_manager thấy 23
  dòng, sales_rep thấy 7 dòng. Đây là khoảnh khắc quan trọng nhất của demo.
- Quy mô mã nguồn: 96 route FastAPI · 25 bảng SQLAlchemy · 8 node type ·
  5 template · 505 test pass trong 175 giây · 54 ADR có ghi lý do.
- Dữ liệu demo: Olist e-commerce dataset + bảng fin_invoice sinh tổng hợp (nói
  rõ là dữ liệu demo trung thực, không khẳng định dataset công khai có công nợ).
- Mock có kiểm soát: ERP = PostgreSQL + Olist · SMTP = MailHog · task tracker =
  REST stub · control plane = static file server có ký số.

PHẠM VI — KHÔNG LÀM TRONG BẢN NÀY (phải nói thẳng trên slide)
Helm chart cho Kubernetes · đa ngôn ngữ ngoài Việt/Anh · rollback tự động khi
eval suy giảm sau deploy · tích hợp SSO/SAML · marketplace template.

TIÊU CHÍ NGHIỆM THU ĐÁNG KHOE (AC)
AC-2 sales_rep và sales_manager hỏi cùng câu nhận kết quả khác nhau ·
AC-4 prompt injection không kích hoạt được tool write ·
AC-6 role của agent không SELECT được bảng gốc fin_invoice ·
AC-7 v_audit_integrity trả rỗng, sửa log thì lòi ra dòng gãy ·
AC-9 DeepEval ≥ 90% trên golden set ·
AC-10 cài từ máy trắng dưới 15 phút ·
AC-11 chưa chat thử lần nào thì không xin duyệt được.

GIỌNG & TỪ VỰNG BẮT BUỘC
- Tiếng Việt, xưng hô trung tính, câu ngắn, không sáo rỗng.
- Dùng: tủ tài liệu (Knowledge Base) · đoạn/Passage · workflow · node · spec ·
  chạy thử (sandbox) · duyệt (HITL) · run · nhãn (label).
- Tránh: "cách mạng hoá", "đột phá", "AI thông minh nhất", mọi con số không có
  trong fact sheet này.
```

---

## Phần 2 — PROMPT DỰNG SLIDE

### 2.1 Prompt chính (dán ngay sau Fact sheet)

```text
Bạn là một người dựng slide cho buổi bảo vệ đồ án kỹ thuật trước hội đồng gồm
giảng viên AI, kỹ sư phần mềm và một người phía nghiệp vụ. Dựa DUY NHẤT vào
fact sheet ở trên, hãy viết nội dung đầy đủ cho một bộ slide thuyết trình.

BỐI CẢNH TRÌNH BÀY
- Thời lượng: 12–15 phút nói + 8 phút demo trực tiếp + 5 phút hỏi đáp.
- Khán giả không đọc trước tài liệu nào. Họ chưa từng nghe từ "meta-agent".
- Hội đồng sẽ hỏi xoáy vào: bảo mật, cách chứng minh agent chạy đúng, và ranh
  giới giữa "AI làm" với "người quyết định".

NGUYÊN TẮC BẮT BUỘC
1. Mỗi slide phục vụ ĐÚNG MỘT ý. Tiêu đề slide là một câu khẳng định có nội
   dung (ví dụ "Agent mượn danh tính người hỏi, không có quyền riêng"), tuyệt
   đối không phải nhãn rỗng kiểu "Kiến trúc" hay "Tính năng".
2. Tối đa 6 bullet/slide, mỗi bullet tối đa 12 từ. Câu đầy đủ để trong phần
   ghi chú của người nói, không nhét lên slide.
3. Mọi con số phải lấy nguyên văn từ fact sheet. Không có trong fact sheet thì
   không được xuất hiện. Không ước lượng, không làm tròn lên.
4. Mỗi tính năng nêu ra phải neo vào một pain point (P1–P4) hoặc một ràng buộc
   (C1–C6). Tính năng không neo được vào cái gì thì bỏ khỏi slide.
5. Nói thẳng phần KHÔNG làm. Một slide riêng cho phạm vi và giới hạn — hội đồng
   tin bài trình bày dám tự vạch giới hạn hơn bài trình bày hoàn hảo.
6. Ưu tiên bằng chứng đã chạy thật hơn lời hứa. Ở đâu có transcript thật, trích
   nguyên văn.

ĐỊNH DẠNG ĐẦU RA — với MỖI slide, xuất đúng khối sau:

---
**Slide N — <Tiêu đề là một câu khẳng định>**
- *Vai trò:* (slide này trả lời câu hỏi nào trong đầu người nghe)
- *Nội dung trên slide:* (3–6 bullet, đúng luật ở trên)
- *Hình/biểu đồ:* (mô tả cụ thể thứ cần vẽ: loại sơ đồ, các khối, mũi tên, nhãn
  — đủ để một người khác vẽ lại mà không cần hỏi thêm. Nếu là ảnh chụp màn hình
  thì ghi rõ chụp màn hình nào, đang ở trạng thái gì.)
- *Lời người nói (~40–70 từ):* (viết ra thành câu, đúng giọng nói trước hội đồng)
- *Chuyển tiếp sang slide sau:* (một câu nối)
---

DÀN Ý BẮT BUỘC — 16 SLIDE, ĐÚNG THỨ TỰ NÀY

1. Mở đầu — tên sản phẩm, một câu định nghĩa, tên nhóm/mã đề bài. Kèm một câu
   hook lấy từ pain point, không phải lời chào.
2. Pain point tầng đề bài — mọi đội đều muốn có agent riêng, nhưng dựng agent
   mất nhiều tuần và cần kỹ sư AI. Nêu hệ quả tắc nghẽn.
3. Pain point tầng khách hàng — bảng P1–P4 kèm số đo hiện trạng (3–4 giờ,
   20–30 request/tuần, chậm 5–8 ngày, mất kiểm soát file Excel).
4. Vì sao cách thông thường không giải được — ba đường cụt: cấp quyền ERP thẳng,
   dashboard BI, chatbot SaaS. Mỗi đường một dòng lý do.
5. Ai dùng hệ thống này — 4 vai trò người (creator/agent_admin, admin duyệt,
   người dùng cuối, PM) + 2 vai trò máy (agent mẹ, agent con). Nhấn: agent con
   KHÔNG có quyền riêng.
6. Giải pháp trong một hình — luồng "mô tả bằng tiếng Việt → spec → workflow →
   chạy thử → duyệt → publish → dùng". Đây là slide xương sống, cả bài sau đó
   chỉ đi sâu vào từng ô của hình này.
7. AgentSpec — agent mẹ sinh ĐẶC TẢ chứ không sinh code; 7 nhóm trường; spec
   `git diff` được giữa hai version; credential chỉ là con trỏ vault://.
8. Kiến trúc ba mặt phẳng — control plane / build plane / runtime plane, mũi tên
   một chiều, và điểm nhấn "agent mẹ cũng nằm trên máy khách hàng".
9. Thành phần kỹ thuật — bảng dịch vụ ↔ công nghệ ↔ trách nhiệm. Kèm lựa chọn
   mô hình local (Qwen2.5-7B AWQ, bge-m3) và lý do 6GB VRAM.
10. Vòng đời 10 bước và hai chốt HITL — chỉ rõ HITL #1 (xác nhận spec) và
    HITL #2 (duyệt deploy) nằm ở bước nào, mỗi bước có điểm kiểm soát gì.
11. Tính năng đã chạy — 8 khối tính năng, đánh dấu trạng thái, mỗi khối một
    dòng nói nó giải pain point/ràng buộc nào.
12. Workflow Canvas & Runtime — 8 node type, sửa luồng bằng lời, pause/resume
    ở node human_approval, và luật side-effect trong sandbox.
13. Bảo mật — delegated authority, RLS + view least-privilege, guardrail
    baseline gỡ không được, audit hash-chain, 404-thay-vì-403. Neo từng cái vào
    C1–C6.
14. Đánh giá & phòng điều hành PM — golden set 23 case và cách chấm tách đôi
    (LLM judge cho ý nghĩa, rule-based cho an toàn); 6 tab PM; ba loại phán xét
    label/thumbs/judge và ý nghĩa của calibration.
15. Bằng chứng đã chạy — trích nguyên văn câu trả lời "Theo Điều 19, khoản 2,
    trang 15…" và khoảnh khắc 23 dòng vs 7 dòng. Kèm quy mô: 505 test, 96 route,
    25 bảng, 54 ADR.
16. Phạm vi, giới hạn và hướng đi tiếp — cái đã làm đầy đủ, cái mock có kiểm
    soát (và vì sao mock là đủ), cái không làm trong bản này, hướng mở rộng.

SAU DÀN Ý, XUẤT THÊM 3 PHỤ LỤC
A. **Kịch bản demo 8 phút** — bảng: mốc phút | thao tác | điều muốn chứng minh |
   câu nói khi màn hình đang tải. Bám kịch bản trong fact sheet. Ghi rõ đâu là
   khoảnh khắc cao trào (cùng câu hỏi, hai vai trò, hai kết quả).
B. **Slide dự phòng cho Q&A** — 6 slide backup, mỗi slide trả lời một câu hỏi
   khó có thật: "Sao tin agent trả lời đúng?", "LLM sinh sai spec thì sao?",
   "Prompt injection thì sao?", "Chi phí vận hành bao nhiêu?", "Khác gì n8n/
   Dify/Zapier?", "Dữ liệu có ra khỏi công ty không?".
C. **Ghi chú thiết kế slide** — bảng màu (nêu mã hex), cặp font, cách thể hiện
   sơ đồ kiến trúc sao cho đọc được từ cuối phòng, và ba lỗi trình bày cần
   tránh với bộ slide cụ thể này.

Nếu có chỗ nào fact sheet không đủ dữ kiện, hãy ghi rõ "[THIẾU DỮ KIỆN: …]" tại
đúng vị trí đó thay vì tự bịa.
```

### 2.2 Prompt phụ — rút gọn còn 5 phút

```text
Từ bộ 16 slide ở trên, hãy nén xuống còn 7 slide cho một lượt trình bày 5 phút
(vòng loại, không có demo). Giữ nguyên: một pain point có số đo, ai dùng, hình
xương sống luồng end-to-end, hai chốt HITL, một bằng chứng chạy thật, một giới
hạn tự nêu. Bỏ mọi thứ khác. Nói rõ với mỗi slide bị gộp: nó nuốt slide nào.
```

### 2.3 Prompt phụ — sinh thẳng file slide

```text
Chuyển bộ slide ở trên thành <Marp Markdown | reveal.js HTML một file duy nhất |
PPTX qua python-pptx>. Yêu cầu:
- Tỉ lệ 16:9, cỡ chữ tiêu đề ≥ 40pt, body ≥ 24pt, đọc được từ cuối phòng họp.
- Sơ đồ kiến trúc và luồng 10 bước vẽ bằng Mermaid (hoặc SVG inline), không dùng
  ảnh bitmap, không gọi tài nguyên từ mạng ngoài.
- Lời người nói đặt vào phần speaker notes, không hiện trên màn chiếu.
- Bảng màu tối giản 1 màu nhấn + 2 màu trung tính; nền sáng.
- Xuất một file duy nhất, chạy được ngay, không phụ thuộc CDN.
```

---

## Phần 3 — PROMPT THUMBNAIL

### 3.1 Bản tóm tắt để model hình dung đúng dự án

> Dán khối này trước prompt ảnh nếu model nhận được text dài (Claude, GPT-4o,
> Gemini). Với Midjourney/DALL·E thì bỏ qua, dùng thẳng 3.2.

```text
Thumbnail cho một sản phẩm phần mềm tên "AI Agent Builder": nhân viên văn phòng
gõ một câu tiếng Việt mô tả công việc, hệ thống biến câu đó thành một trợ lý AI
chạy được — có sơ đồ luồng, có bước con người bấm duyệt trước khi cho chạy.
Ba ý phải nhìn ra ngay trong 2 giây: (1) từ LỜI NÓI ĐỜI THƯỜNG, (2) thành MỘT
SƠ ĐỒ AGENT chạy được, (3) CON NGƯỜI vẫn là người gật đầu cuối cùng.
Cảm giác cần có: thân thiện, sáng sủa, đáng tin — KHÔNG phải hacker, KHÔNG phải
robot doạ người, KHÔNG phải ma trận số xanh lá.
```

### 3.2 Prompt ảnh — 3 phương án, chọn 1 hoặc chạy cả 3 rồi so

**Phương án A — "Câu nói hoá thành sơ đồ" (khuyến nghị)**

```text
Modern flat vector illustration, 16:9, wide banner composition.
Left third: a friendly Vietnamese office worker at a laptop, warm and relaxed
posture, a speech bubble containing a simple handwritten-style sentence.
Center: the speech bubble visually morphs into a clean node-and-edge workflow
diagram — 5 rounded rectangular nodes connected by smooth arrows, each node
carrying a tiny simple icon (document, magnifier, branch, checkmark, paper plane).
Right third: a large friendly checkmark badge held or approved by a second human
figure, signalling human approval before launch.
Style: soft geometric shapes, generous whitespace, subtle long shadows, thin
consistent 2px line weight, rounded corners everywhere.
Color palette: warm off-white background (#FAF9F6), deep indigo primary (#2D3E7E),
one warm coral accent (#E8734A), soft teal secondary (#3AA6A0); high contrast,
no gradients heavier than a soft two-stop.
Mood: approachable, optimistic, trustworthy, professional-but-not-corporate.
No text, no lettering, no logos, no watermark.
```

**Phương án B — "Trước / Sau"**

```text
Split-screen flat vector illustration, 16:9.
Left half (before): a tired office worker buried under stacks of paper, scattered
spreadsheet windows and email icons, muted desaturated palette, cluttered layout.
Right half (after): the same worker relaxed, one clean glowing workflow diagram
floating on a tidy screen, an AI assistant character rendered as a soft friendly
rounded shape (not a humanoid robot, not a face), bright saturated palette,
lots of whitespace.
A clear vertical seam with a simple forward arrow crossing it at center.
Style: modern editorial tech illustration, rounded geometry, flat shading with
soft shadows, consistent thin outlines.
Palette: muted grey-blue on the left, warm off-white + indigo + coral on the right.
Mood: relief, clarity, human-centered.
No text, no lettering, no logos.
```

**Phương án C — "Bàn tay ký duyệt"**

```text
Isometric flat vector illustration, 16:9, centered composition.
A translucent layered stack of three floating planes seen in isometric view:
the top plane holds a chat bubble, the middle plane holds an editable
specification form with rows and toggles, the bottom plane holds a running
workflow graph of connected nodes.
A human hand reaches in from the right and places a friendly approval stamp
on the middle plane — the human decision sits between the idea and the machine.
Style: clean isometric tech illustration, soft ambient occlusion, rounded edges,
thin consistent linework, generous negative space.
Palette: off-white background (#FAF9F6), indigo (#2D3E7E), coral accent (#E8734A),
soft teal (#3AA6A0).
Mood: precise, calm, trustworthy, friendly.
No text, no lettering, no logos, no watermark.
```

### 3.3 Negative prompt (dùng chung)

```text
text, letters, words, captions, watermark, logo, signature, gibberish typography,
menacing humanoid robot, glowing red eyes, terminator, skull, hacker in hoodie,
dark server room, green matrix code rain, binary digits, circuit-board brain,
lens flare, neon cyberpunk, chrome, glossy 3D render, stock-photo realism,
cluttered composition, tiny unreadable details, distorted hands, extra fingers,
low contrast, muddy colors
```

### 3.4 Tiêu chí nghiệm thu thumbnail — soi từng cái trước khi chốt

| # | Tiêu chí | Cách kiểm |
|---|---|---|
| 1 | **Đúng mô tả dự án** | Người chưa biết dự án nhìn 3 giây, nói được "phần mềm này biến lời nói thành một luồng tự động, và có người duyệt" |
| 2 | **Thân thiện** | Có ít nhất một hình người ở tư thế thoải mái; không robot doạ người, không tông tối |
| 3 | **Gây hấp dẫn** | Có một điểm nhìn duy nhất (mũi tên biến hình, hoặc dấu duyệt) hút mắt trước tiên |
| 4 | **Đọc được ở kích thước nhỏ** | Thu về 320px ngang vẫn nhận ra bố cục; không có chi tiết nào nhỏ hơn 1/20 chiều rộng |
| 5 | **Không chữ trong ảnh** | Chữ (nếu cần) đè lên sau bằng công cụ layout, không để model vẽ — model vẽ chữ luôn sai |
| 6 | **Đúng bảng màu slide** | Cùng mã hex với bộ slide, để thumbnail và slide nhìn ra là một bộ |
| 7 | **Trung thực** | Không vẽ tính năng hệ thống không có (không cảnh robot tự gửi email — sản phẩm cố tình KHÔNG cho agent tự gửi) |

### 3.5 Prompt tinh chỉnh sau khi có ảnh đầu tiên

```text
Giữ nguyên bố cục và bảng màu của ảnh này. Sửa những điểm sau:
1. <ví dụ: làm mũi tên biến hình ở giữa dày và rõ hơn — đây là ý chính>
2. <ví dụ: giảm số node từ 7 xuống 5, cho thoáng>
3. <ví dụ: đổi nhân vật bên phải sang tư thế đang gật đầu / giơ tay duyệt>
Không thêm chữ. Không thêm chi tiết mới ngoài ba điểm trên.
```

---

## Phần 4 — CHECKLIST TRƯỚC KHI CHỐT BỘ SLIDE

- [ ] Mỗi tiêu đề slide là một câu khẳng định, không phải nhãn rỗng.
- [ ] Mọi con số truy được về Fact sheet (Phần 1).
- [ ] Mỗi tính năng trên slide neo được vào một P1–P4 hoặc C1–C6.
- [ ] Có đúng một slide xương sống (slide 6) mà cả bài quay về.
- [ ] Hai chốt HITL được chỉ mặt đặt tên, không nói chung chung "có người duyệt".
- [ ] Có một slide tự nêu giới hạn và phần không làm.
- [ ] Kịch bản demo có mốc thời gian và có câu nói lấp lúc màn hình đang tải.
- [ ] Có 6 slide backup cho Q&A.
- [ ] Thumbnail và slide dùng chung bảng màu.
- [ ] Không câu nào chứa "cách mạng hoá", "đột phá", "thông minh nhất".
