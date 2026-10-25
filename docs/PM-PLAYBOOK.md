# PM Playbook — quản lý chất lượng agent bằng số liệu

> Vai PM của nền tảng này cần: biết agent đang chạy tốt hay tệ **trước khi khách
> biết**, chứng minh phiên bản mới tốt hơn phiên bản cũ **trước khi thay**, và
> biến mỗi lần chạy sai thành một ca kiểm tra vĩnh viễn. Tài liệu này là bộ đồ
> nghề cho ba việc đó, xếp theo thứ tự xây. Nguồn: khảo sát thực hành 2025–2026
> (Langfuse/OTel GenAI/OpenAI evals/Anthropic eval guide, WorfBench, ALCE,
> RAGTruth — link cuối tài liệu).

## 1. Trace schema — ghi gì để sau này phân tích được

Nguyên tắc: **mọi câu hỏi PM là một group-by trên trace.** Trường nào không phải
attribute có cấu trúc thì sau này không group-by được — đổi schema muộn là mất
lịch sử.

> **Đã dựng (2026-08-22, [ADR-0053](adr/0053-so-run-dung-chung-va-dashboard-pm-theo-chuan-ai-evaluation.md)):**
> bảng `agent_runs` — **sổ run dùng chung**, một hàng mỗi leg, cả V2 lẫn UC-1,
> ghi trong nền tảng, không cần Langfuse. Mỗi hàng giữ câu hỏi, câu trả lời,
> transcript (cùng hình `SandboxTranscriptEntry` với sandbox) và các cột suy ra
> lúc ghi. `run_costs` giữ nguyên làm hoá đơn V2. Ba cửa ghi: V2 production
> (`RunLedgerEntry.bank`), V2 sandbox (`store_sandbox_run`), UC-1 chat
> (`uc1_agent_chat_routes`). Bài giảng AI Evaluation (COMP3011 D10, slide 32):
> *transcript là lời kể, trace là bằng chứng* — trước ADR-0053 run production
> không để lại bằng chứng nào.

| Trường | Cấp | Giá trị | Trả lời câu hỏi | Trạng thái |
|---|---|---|---|---|
| `outcome` | run | success \| spec_error \| system_error \| guardrail_block \| error \| needs_approval \| budget_stopped | "Lỗi nào nhân viên tự sửa được, lỗi nào phải gọi ta?" | ✅ code quyết định lúc ghi (`src/pm/taxonomy.py::classify_error`); `error` = chưa có từ |
| `trigger` | run | chat_test \| manual | tách "chạy thử trên bàn" khỏi "chạy thật" | ✅ |
| `pipeline` · `agent_key` · `version_key` | run | v2 \| uc1 · chuỗi | một sổ cho hai từ vựng (ADR-0049) | ✅ |
| `failure_node_type` · `first_failure` | run | mcp \| agent_llm \| … · `node_id:tool` | "Loại bước nào hay hỏng?" — lỗi bắt đầu ở đâu (slide 33) | ✅ suy từ transcript |
| `actions` | run | `["gmail.send_email", …]` | "Connector nào hay hỏng?" | ✅ |
| `tool_failures` | run | số | run `success` mà có tool hỏng — lỗi chìm dưới mặt nước | ✅ |
| `input_message` · `answer` · `transcript` | run | | đọc trace; điền sẵn "→ case"; judge production | ✅ |
| thumbs | score | `quality_scores/thumbs_up` theo `run_id` | nhãn người dùng rẻ nhất | ✅ từ cửa sổ chat (`POST /runs/feedback`, theo `trace_id` của event `thread`) và từ PM board |
| label | label | `run_labels`: passed · codes · note | nhãn expert, calibrate judge, taxonomy | ✅ màn đọc run |
| model + usage per step | step | | cost theo bước | ❌ còn theo run |

## 2. Dataset có coverage

**Tìm trên mạng trước** (đúng thứ tự ưu tiên đã chốt):

| Bộ | Dùng vào việc | Lấy gì |
|---|---|---|
| **WorfBench** (Apache-2.0, HF `zjunlp`) | Meta agent soạn workflow từ NL — khớp hình bài toán nhất | **Mượn metric WorFEval** (subsequence/subgraph matching giữa graph model và graph chuẩn) — chấm draft không cần judge; dataset gốc dùng smoke-test |
| **TaskBench** (Microsoft, HF) | 3 tầng lỗi: decompose / tool graph / **tham số** | Mượn cấu trúc "instruction → tool graph + params" làm khuôn golden case |
| **BFCL** (Apache-2.0, `pip install bfcl-eval`) | Model gọi tool đúng không | Smoke-test model + mượn cách chấm AST cho argument |
| **ALCE** (Princeton, MIT code) | RAG có trích dẫn | **Giao thức citation precision/recall** — implement lại trên dữ liệu của mình (giao thức không phụ thuộc ngôn ngữ) |
| **RAGTruth** (HF, ~18k nhãn hallucination) | Calibrate judge | Judge của ta phải bắt được hallucination người đã đánh dấu |
| CRAG (CC BY-NC — chỉ nội bộ) | Taxonomy 8 loại câu hỏi | Checklist coverage khi tự sinh |

**Tự sinh (phương án cuối, nhưng không tránh được)** — làm đúng phương pháp.

Lý do phải tự sinh **không phải ngôn ngữ** mà là **thế giới**: không bộ công khai
nào có "tổ CNTT dựng agent cho phòng ban, dữ liệu ở ERP nội bộ, hành động ra
ngoài phải qua cổng duyệt". Mượn được *metric* và *khuôn case*, không mượn được
bối cảnh.

1. Bắt đầu từ **thất bại thật**: mỗi run sai + mỗi lần user phàn nàn trong chat
   → một case. 20–50 case đầu tiên lấy từ đây, không bịa.
2. Chốt **các trục**: phòng ban được phục vụ (Kinh doanh, Kế toán, Nhân sự,
   CNTT) × độ phức tạp graph (1 bước / tuyến tính / có nhánh / có vòng lặp) ×
   nguồn dữ liệu (ERP nội bộ, kho tài liệu, hộp thư, không) × có hành động ra
   ngoài tổ chức không × độ mơ hồ của lời yêu cầu × cách diễn đạt (kể lể / ra
   lệnh) × **ngôn ngữ (Việt / Anh / lẫn hai thứ tiếng)**.

   Ngôn ngữ là **một trục coverage bình đẳng**, không phải ràng buộc của sản
   phẩm: nền tảng phục vụ cả hai, prompt của meta agent viết bằng tiếng Anh và
   trả lời theo ngôn ngữ người tạo dùng. Vì thế bộ đề phải có ca cho cả ba ô —
   và ô "lẫn hai thứ tiếng" là ô hay hỏng nhất, không phải ô rìa: người dùng
   thật viết "cho tôi cái agent tự động gửi report hàng tuần" suốt ngày.
3. Lấy lưới tổ hợp các trục → LLM sinh instance cho từng ô (chống mode-collapse
   happy-path), người duyệt từng case.
4. Bộ đề phải **cân cả hai chiều**: có case đúng phạm vi *và* case phải từ chối
   (việc A→B rule cứng → đáp án tốt là từ chối làm).

Golden set hiện có: `golden/meta_agent_v1.jsonl` (19 ca cho meta agent — xem
`docs/eval-meta-agent-golden-v1.md`) và `golden/uc1_ar_v3.jsonl` (agent UC-1).
Mở rộng theo khuôn trên.

> **Nợ kỹ thuật cần nói rõ:** 19 ca hiện tại là **soạn ra**, không thu từ run
> thật — ngược với bước 1 ngay trên. Bộ đề tự soạn có hai tật đã biết: dễ hơn
> thực tế, và trùng giả định của người soạn (nó kiểm tra cái mình *nghĩ* là khó,
> không phải cái *thật sự* hay hỏng). Ca thu từ thất bại thật phải thay dần vào,
> và mỗi lần thay là một lần đổi baseline — ghi lại, đừng so ngang qua các bản.

## 3. Rubric

Quy tắc chung: **mỗi chiều một điểm riêng**, không có "điểm tổng thể"; anchor
hành vi cụ thể ở 5/3/1; ưu tiên boolean/categorical hơn thang 1–10; judge phải
xuất `reasoning` trước `score`; **chốt rubric + ngưỡng trước khi chạy**
(guardrail pre-register của repo).

### 3a. Chất lượng bản nháp workflow (meta agent)

| # | Chiều | Kiểu | Ai chấm |
|---|---|---|---|
| D1 | Schema hợp lệ | bool | code — `WorkflowJSON.model_validate` |
| D2 | Graph lành mạnh | bool | code — `graph_problems` (validation.py) |
| D3 | Tool tồn tại & an toàn (tool trong catalog, write có duyệt) | bool | code |
| D4 | Phủ đúng ý định (đủ yêu cầu, không thêm việc thừa) | 1–5 | judge |
| D5 | Chọn đúng loại node (tất định ≠ phán đoán ≠ công cụ) | 1–5 | judge |
| D6 | Tham số tool đúng, không bịa | 1–5 | judge (hoặc AST khi có gold) |
| D7 | Người không code đọc hiểu được | 1–5 | judge |
| D8 | Trung thực phạm vi (ngoài phạm vi thì nói, không vờ làm được) | bool | judge |

Anchor mẫu D4: **5** = mọi yêu cầu có bước tương ứng; **3** = đúng việc chính,
sót một ràng buộc phụ; **1** = hiểu sai việc chính hoặc bịa yêu cầu.

### 3b. Câu trả lời có trích dẫn (RAG)

| # | Chiều | Kiểu |
|---|---|---|
| F1 | Faithfulness (mọi mệnh đề được nguồn đỡ) | 1–5 |
| F2 | Citation precision (tỷ lệ citation thật sự đỡ câu nó gắn) | 0–1, giao thức ALCE |
| F3 | Citation recall (tỷ lệ mệnh đề cần dẫn có dẫn) | 0–1, ALCE |
| F4 | Đúng & đủ so với câu hỏi | 1–5 |
| F5 | Biết dừng khi thiếu evidence (không bịa) | bool |

### 3c. Catalog metric nền tảng — tên RAGAS/DeepEval trên một seam judge (ADR-0054 §4)

Khi mentor nhắc "RAGAS, DeepEval, …" — đó là **cách gọi tên** cho các chiều đo
nền tảng đã có, không phải một bộ máy chấm khác đang chạy ở đây. Bảng dưới là
`src.evals.metrics.METRIC_CATALOG` ở dạng đọc được; đổi bảng thì sửa
`METRIC_CATALOG`, không sửa doc này trước — doc theo code, không phải ngược
lại.

> **Nói thẳng một lần: đây là *ánh xạ tên*, không phải tương đương thuật toán.**
> Mọi hàng `judge` dưới đây là **một** lần gọi model, trả về điểm 0..1 kèm lý do.
> Thư viện gốc làm khác: RAGAS `faithfulness` tách câu trả lời thành từng mệnh đề
> rồi đối chiếu từng mệnh đề với ngữ cảnh; DeepEval `AnswerRelevancy` sinh ngược
> câu hỏi từ câu trả lời rồi so với câu hỏi thật; RAGAS `context_precision` còn
> chấm cả **thứ hạng** của từng đoạn tìm được. Khác thuật toán thì khác phương
> sai, khác kiểu sai — nên **số ở đây không so được với số công bố dưới cùng tên
> đó**. Nó chỉ so được với chính nó qua các version của nền tảng, và đó đúng là
> phép so duy nhất ngưỡng `DEFAULT_THRESHOLD` cần. Khi trình bày trước mentor:
> nói "faithfulness **theo lối** DeepEval", đừng nói "điểm DeepEval".

| Category (platform) | RAGAS | DeepEval | Chấm bằng | Cần gì từ run |
|---|---|---|---|---|
| `workflow_success` | — | TaskCompletion (một phần) | rule `ReachesEnd` | transcript |
| `tool_correctness` | — | ToolCorrectness | rule `CallsAction`/`ForbidsAction` | tool calls |
| `human_approval_compliance` | — | — | rule `ApprovalCompliance` | approvals |
| `source_groundedness` | faithfulness | Faithfulness · Hallucination (nghịch) | judge, đọc `retrieval_context` + tool results | answer + evidence |
| `correctness` | — | G-Eval (rubric = `expected`) | judge | answer + expected |
| `relevance` | answer_relevancy | AnswerRelevancy | judge | input + answer |
| `context_precision` | context_precision · context_recall | ContextualPrecision/Recall | judge, đọc kết quả của bước tra cứu (`KnowledgeSearch` **hoặc** `WebSearch`) — chỉ áp dụng khi graph có một trong hai node đó, không có thì case bị bỏ qua (`skipped[]`, "không áp dụng" chứ không phải "trượt") | retrieval_context + expected |
| `task_completion` | — | TaskCompletion | judge, đọc cả transcript | input + transcript + answer |

`hallucination` không phải một hàng riêng — nó là nghịch đảo của
`source_groundedness`, hai tên cho một phép đo. Mọi metric `judge` chấm 0..1
kèm lý do rồi cắt nhị phân tại `src.evals.metrics.DEFAULT_THRESHOLD` (0.7,
hằng số đăng ký trước) — số thô giữ lại trong `results[].metrics[]` để sau
này calibrate, quyết định (đạt/không) luôn là số đã cắt (bài giảng s52).
Không cài gói `ragas`/`deepeval` — lý do ở docstring `src.evals.uc1_deepeval_runner`
và ở `src.evals.metrics`: một đường mạng riêng, một seam thứ hai, không hợp
NFR-05 (air-gap).

Judge chạy trên `Settings.judge_model` (mặc định = model của meta-agent) chứ
không phải trên spec của agent đang được chấm — đổi biến này là đổi baseline,
`eval_runs.judge_model` ghi lại model đã chấm từng lần chạy.

## 4. Evaluator — xếp tầng theo chi phí

1. **Code checks, 100% draft, miễn phí**: D1–D3 đã có sẵn dưới dạng
   `graph_problems` — việc còn thiếu là **ghi kết quả thành score** gắn vào
   trace/version thay vì chỉ hiện lên canvas. Khi có gold-graph: chấm cấu trúc
   bằng subgraph-matching kiểu WorFEval, tất định, không tốn LLM.
2. **LLM-judge, sampled**: D4–D8, F1–F5. Bắt đầu bằng **một** judge duy nhất:
   faithfulness + citation (F1–F3) trên run production — hallucination trên tài
   liệu khách, ở lần chạy không có người tạo ngồi cạnh, là rủi ro thiệt hại lớn
   nhất.
3. **Người**: tự chấm tay 50 trace đầu, đo agreement judge-vs-người trước khi
   tin judge; chấp nhận criteria drift — rubric là thứ lặp.

Pointwise + rubric cho monitoring liên tục; pairwise (đảo vị trí 2 lần, bất nhất
= hoà) chỉ khi so hai phiên bản prompt/model của meta agent.

**Regression gate**: mỗi lần sửa prompt meta agent → chạy lại golden set, so
baseline, ngưỡng đã pre-register, tụt thì không merge. `src/evals/meta_agent_gate.py`
là runner + gate cho meta agent (chấm tất định: required/forbidden node types,
thứ tự duyệt-trước-hành-động, tính cục bộ, mentions).

### Mốc đối chiếu bên ngoài — n8n

n8n mở công khai eval harness của AI Workflow Builder
(`packages/@n8n/instance-ai/evaluations`): **28 check nhị phân trên 8 chiều** —
`structure` 4, `connection_topology` 4, `parameter_correctness` 8, `intent_match`
1, `communication` 1, `ai_nodes` 6, `nodes_craftsmanship` 2, `security` 2. Họ đo
`pass@k` / `pass^k`, phân loại thất bại làm 5 nhóm (`builder_issue`, `mock_issue`,
`framework_issue`, `verification_gap`, `build_failure`), và định nghĩa regression
theo mức tin cậy thống kê chứ không theo cảm giác (hard regression: tụt ≥30 điểm
phần trăm, xác suất do nhiễu <5%).

Con số đáng để chỉnh kỳ vọng:

> *"Suite pass rates typically sit between 40–65%; most failures are `builder_issue`."*

Đội chuyên, LangGraph, model mạnh, tool có validation — **40–65%**. Ngưỡng
pre-register của ta là 83% / 80% / 75% / 75%. Hai khả năng, và **chưa đo nên chưa
biết là cái nào**: bộ đề của ta dễ hơn thực tế đáng kể, hoặc ngưỡng của ta phi
thực tế.

Guardrail vẫn nguyên: **chạy trước, nhìn số sau, không nới ngưỡng sau khi nhìn.**
Nếu trượt thì kết luận đúng là "bản này chưa đạt", không phải "ngưỡng đặt cao quá".
Ngưỡng chỉ được đổi bằng một diff có review, và bản ghi phải nói rõ đổi *sau* khi
đã thấy kết quả nào.

Hai thứ đáng mượn của họ ngay, cả hai đều rẻ:
- **Phân loại thất bại**: hiện gate chỉ nói đạt/không. Biết một ca trượt vì
  *builder* dở hay vì *bộ đề* sai là khác biệt giữa sửa prompt và sửa đề.
- **`pass^k`** (chạy k lần, đếm ca đạt *mọi* lần): tính bất định của meta agent là
  thứ người dùng cảm nhận rõ nhất, mà `pass@1` giấu mất hoàn toàn.

## 5. Dashboard — theo chuẩn bài giảng AI Evaluation

> **Đã dựng lại (2026-08-22, [ADR-0053](adr/0053-so-run-dung-chung-va-dashboard-pm-theo-chuan-ai-evaluation.md)):**
> đăng nhập `pm@example.com` → PM control room (`/api/v1/pm/*`, `PMDashboard.jsx`).
> Đọc `agent_runs` (cả V2 lẫn UC-1), `quality_scores`, `run_labels`, `eval_runs` /
> `uc1_eval_runs`, `run_approvals`, `meta_agent_gate_runs`. Giả lập **ẩn mặc định**,
> bật bằng nút; banner nói bao nhiêu run có trace.
>
> **2026-08-23:** tách thành **sáu tab trên sidebar** (`frontend/src/pages/pm/`), mỗi tab
> một câu hỏi: Tổng quan (cảnh báo + #1 #2 + activation) · Run & trace (#7 + bảng run) ·
> Chất lượng theo version (#4 + báo cáo + gate) · Chấm tay (màn đọc run đi qua hàng đợi +
> #8) · Chi phí & độ trễ (#3) · Duyệt & phản hồi (#5 #6). Cửa sổ ngày và nút giả lập dùng
> chung — cửa sổ ngày gõ được số bất kỳ (1–365; 7/28/90 là lối tắt), và hai bảng nhiều
> chữ (phễu duyệt, bảng run) cho kéo mép cột, nhớ trong `localStorage`. Chuyển tab chỉ ở
> sidebar: trang không lặp lại danh sách sáu tab lần nữa. Mọi câu giải thích của widget
> nằm sau dấu ⓘ cạnh tiêu đề (`components/ui.jsx::Hint`) — chữ giữ nguyên, chỉ hiện khi
> di chuột; trên màn hình còn lại là số, nhãn và câu từ chối.
> Màn đọc run có nút **mở trong Langfuse** khi cấu hình `LANGFUSE_*` +
> `LANGFUSE_PROJECT_ID` (cùng `trace_id` với `agent_runs`; xem `RUN_GUIDE.md`).
> Bộ đề của agent con (upload, suite theo agent, lưới tình huống, catalog metric) thiết kế ở
> [ADR-0054](adr/0054-bo-test-agent-con-suite-theo-agent-tu-nap-luoi-tinh-huong-catalog-metric.md),
> giao nhóm làm qua issue #150 (map) và #151–#158.

Hình dạng lấy từ bài giảng COMP3011 D10 (*AI Evaluation*), ánh xạ như sau:

| # | Widget | Slide | Trả lời |
|---|---|---|---|
| — | **Cảnh báo Q1–Q4 + LAT/COST** với ngưỡng chốt trước (`src/pm/thresholds.py`) | 19, 49, 50 | success dưới ngưỡng? offline–online lệch? failure mode mới? thumbs-down tăng mà judge ổn? latency/cost vượt trần? |
| 1 | Success theo `trigger`, kèm n | 5 | Agent Success Rate trên lưu lượng thật |
| 2 | Outcome theo taxonomy + "success có tool hỏng" + self-fix ratio | 33, 35 | lỗi ai sửa; lỗi chìm dưới mặt nước |
| 3 | Cost/run, cost/success, latency P50/P90, cầu dao | 45 | threshold checks |
| 4 | **Offline vs online theo phiên bản**: eval pass (gate), online success, judge (mẫu), thumbs, expert, link có căn cứ; cờ `regressed` / `diverged`; nút **Chấm mẫu judge**; nút **báo cáo** | 18, 19-Q2, 65 | bộ đề có mô tả thực tế không; bản mới hơn bản cũ không |
| 5 | Phễu duyệt + Approve/Reject | — | |
| 6 | Thumbs theo tuần + ghi chú | 9, 11 | evals là user research |
| 7 | **Lỗi bắt đầu ở đâu**: theo `failure_node_type`, theo action, theo trace code | 47 | 80% dồn một lát = failure mode |
| 8 | **Hiệu chỉnh judge**: ma trận nhầm lẫn judge × expert, TPR/TNR/κ, nhãn "đủ tin" chỉ khi đạt ngưỡng trên ≥ 30 nhãn, danh sách bất đồng | 53–57 | judge có nghĩ giống ta không |
| — | **Màn đọc run** (ngăn kéo): câu hỏi → trace → câu trả lời → Đạt/Không đạt (G/B) + trace code + ghi chú; ý kiến judge **ẩn cho đến khi đã quyết**; hàng đợi "cần đọc" ưu tiên run đã có judge, rồi run bị thumbs-down | 61–64 | một nhãn dùng ba lần |
| — | **Báo cáo eval một phiên bản**: baseline (bản trước) · candidate · delta theo category, regression (case từng đạt nay trượt), nhiễu ±2 điểm, verdict ship/iterate/hold kèm việc tiếp theo | 18, 50 | |

Ba tầng chấm (slide 59, Swiss cheese): **L1 code** trên 100% run lúc ghi
(`tool_groundedness`, `citation_groundedness`, `tools_ok`); **L2 judge** lấy mẫu
production (`POST /pm/judge/sample` hay `scripts/judge_production_sample.py`),
mỗi tiêu chí một judge, verdict nhị phân, tiêu chí lấy từ `success_criteria`
của chính spec; **L3 người** — nhãn expert trên màn đọc run.

Cần gạt: đọc & gán nhãn run, thumbs, **"→ case"** (điền sẵn input từ trace; UC-1
chỉ trả dòng JSONL), chấm mẫu judge, duyệt, cầu dao, gate.

Funnel activation của một agent (đo bằng event, không phải survey):
mở session → có spec đầu tiên → dry-run đầu tiên → **bật tool ghi** → publish →
**có người ngoài chủ agent dùng** → còn chạy đều sau 2/4 tuần. Mốc **"bật tool
ghi"** vẫn chưa đo.

## 6. Thứ tự xây (đã xếp theo lợi/chi phí, engineering time hạn hẹp)

Trạng thái tính đến **2026-08-22** (sau ADR-0053):

| # | Việc | Trạng thái |
|---|---|---|
| 1 | Trace: `session_id`, `user_id`, `outcome` thô | ✅ `tracing.py`; **và** `agent_runs` giữ trace trong nền tảng (ADR-0053) |
| 2 | Ghi `graph_problems` thành score mỗi save/confirm (D1–D3 tự động) | ✅ `services/quality_scores.py` |
| 3 | Golden case → `golden/`, nối runner, ngưỡng pre-register | ⚠️ gate chạy giả lập; 3 ca không thể đạt; **để sau** theo yêu cầu (chưa có data) |
| 4 | Thumbs + ghi chú | ✅ PM board **và cửa sổ chat của end-user** (`POST /runs/feedback`) |
| 5 | Judge trên production theo mẫu, calibrate trên nhãn tay | ✅ máy: `src/pm/judge.py` + widget calibration; **chưa có nhãn** — đang 0 nhãn / cần 30 |
| 6 | Dashboard | ✅ dựng lại theo chuẩn slide (§5) |
| 7 | Taxonomy lỗi → `outcome` chi tiết | ✅ `src/pm/taxonomy.py` — tất định, lúc ghi; `error` = chưa có từ, board đếm riêng |
| 8 | Trục coverage + sinh bộ theo lưới, pairwise A/B | ❌ để sau cùng golden set |
| 9 | UC-1 ghi sổ chung | ✅ `uc1_agent_chat_routes._record_in_ledger`; cột duyệt UC-1 chưa vào widget 5 |

**Đã đảo 4 ↔ 5 so với bản trước.** Judge phải được calibrate trên nhãn người mới
đáng tin, mà thumbs chính là nguồn nhãn rẻ nhất — làm judge trước thumbs là
calibrate bằng không khí. Thumbs còn có lợi kép: nó vừa sinh nhãn cho bước 5, vừa
là nguồn ca cho bước 3 ("phàn nàn thật → golden case").

**Ghi chú bước 3 — chỗ đáng lo nhất hiện nay.** Gate tồn tại, ngưỡng đã đóng
băng, và **chưa chạy với model thật lần nào**. Nghĩa là chưa có một con số nào về
chất lượng meta agent, nên mọi khẳng định "bản mới hơn bản cũ" hiện không có gì
đỡ ngoài lập luận kiến trúc. Đây là việc rẻ nhất còn lại (19 ca) và đắt nhất nếu
để nguyên.

*Cập nhật 2026-08-21:* chạy gate bằng **oracle biết đáp án** (`src/pm/gate.py`,
fidelity 1.0 = builder hoàn hảo) là phép kiểm tĩnh bộ đề, và nó cho thấy
**`multi-turn-01`, `multi-turn-04`, `diagnose-03` không thể đạt với bất kỳ
model nào**: grader đọc `precedence: [human_approval, mcp]` là "duyệt trước
*mọi* node mcp", trong khi cả ba graph mở đầu bằng một mcp chỉ đọc
(`lay_cong_no`) và ý của ca là "duyệt trước bước gửi". Hệ quả: họ `multi_turn`
tối đa 3/5 < ngưỡng 4/5 → **gate như đang đăng ký không bao giờ xanh**. Theo
đúng guardrail, chưa sửa gì — đề, grader, ngưỡng đều giữ nguyên; dashboard nêu
đích danh ba ca, và mọi thất bại ở đó được phân loại `case_issue` chứ không đổ
cho builder. Sửa là một diff có review: hoặc `precedence` nhận node id cụ thể,
hoặc grader chỉ gác mcp có action không-read-only theo catalog (ADR-0051).
Đây chính là cái lợi của "phân loại thất bại" mà mục 4 mượn của n8n — phát
hiện ra trước khi tốn một token nào.

**Ghi chú bước 5 — máy đã có, chỗ đặt thì chưa.** `src/evals/uc1_deepeval_runner.py`
đã chấm `faithfulness`/`answer_relevancy` **theo lối DeepEval** (một lần gọi
judge trên chính seam model của nền tảng, không phải thuật toán của thư viện), và
đang được dùng làm **cổng duyệt UC-1** (`uc1_approval_routes.py`). Cái còn thiếu
không phải judge, mà là **đặt nó lên run production theo mẫu** + calibrate. Đừng
viết judge thứ hai.

## Nguồn chính

Langfuse data model & LLM-as-a-judge · OpenTelemetry GenAI semantic conventions
· OpenAI agent evals / graders / evaluation best practices · Anthropic
"Demystifying evals for AI agents" · WorfBench (ICLR'25) · TaskBench
(NeurIPS'24) · BFCL · ALCE (EMNLP'23) · RAGTruth (ACL'24) · CRAG (KDD Cup'24)
· EvalGen "Who Validates the Validators" (UIST'24) · Hamel Husain evals FAQ.

**Đối thủ trực tiếp, đọc được mã nguồn** — n8n AI Workflow Builder:
`n8n-io/n8n`, `packages/@n8n/instance-ai/` — `evaluations/README.md` (28 check /
8 chiều, pass@k, ngưỡng regression), `skills/workflow-builder/SKILL.md` (~60 luật
viết tay), `docs/architecture.md` (vòng lặp build→verify→debug, cùng lỗi hai lần
thì dừng). Đây là nguồn tham chiếu tốt nhất hiện có vì cùng hình bài toán *và*
công khai được số thật.
