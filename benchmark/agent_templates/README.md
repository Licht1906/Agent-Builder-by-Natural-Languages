# Benchmark — agent mẫu phủ hết năng lực nền tảng

Bốn mẫu agent, mỗi mẫu là **một file JSON tự chứa**: đặc tả nghiệp vụ đã validate,
graph đầy đủ tới từng config, lời khai về cấu trúc, và lý do vì sao mẫu đó khó.
Chúng nằm trong repo chứ không chỉ nằm trong database, vì một mẫu chỉ sống trong
`agent_versions` là một mẫu không ai đọc lại được sau khi database bị reset.

## Vì sao bộ này tồn tại

Phê bình nguyên văn của chủ sản phẩm về fleet cũ: *"các agent mẫu b tạo ra cũng chỉ
có vài nút thì kiểm chứng được gì"*. Đúng. Đo trên chính database:

```
v130 agent 105  [mcp, agent_llm, end]              ← tên agent là "Gmail có duyệt"
v131 agent 106  [mcp, agent_llm, end]
v132 agent 107  [agent_llm, end]
v133 agent 108  [knowledge_search, agent_llm, end]
```

Không một graph nào có `human_approval`, `collect`, `if_else` hay `while`. Agent 105
tên là *"Gmail có duyệt"* và trong graph của nó không có một node duyệt nào. Bộ này
là câu trả lời: **tám trên chín loại node** xuất hiện thật, và mỗi thứ trong danh sách
dưới đây có ít nhất một mẫu chạy được nó tới cùng.

| loại node | mẫu |
|---|---|
| `human_approval` (cả nhánh duyệt lẫn nhánh từ chối) | 01, 03 |
| `collect` (đúng cách: node mcp đích ở chế độ `single`, action có tên) | 01, 02, 03 |
| `if_else` (điều kiện viết ra thật) | 01, 03 |
| `while` (điều kiện dừng rõ ràng + trần vòng lặp) | 02 |
| `knowledge_search` (Knowledge Base thật, tài liệu trong `knowledge/`) | 02 |
| `mcp` — hai kết nối khác nhau trong một graph | 03 |
| `require_approval: always` (người tạo tự siết chặt hơn mức rủi ro) | 03 |
| `agent_llm`, `end` | tất cả |

`web_search` là loại thứ chín và **cố tình vắng mặt**: bản cài đặt thật của nó đòi
`SEARCH_API_KEY`, môi trường này không có, nên node chỉ ghi được một dòng từ chối vào
transcript. Một mục benchmark mà bước giữa luôn hỏng thì không đo được gì về agent.

## Các mẫu

| file | mức | chứng minh được gì mà mẫu khác không | agent/version đã chạy |
|---|---|---|---|
| `00-doi-chung-tra-loi-thang.json` | **đối chứng** | không gì cả — và đó là công dụng: nếu mục này cũng trượt thì hỏng là hạ tầng đo, không phải model | 123 / 186 |
| `01-gui-thu-co-duyet.json` | khó | một action `high` risk bị luật bắt buộc phải có người đứng trước; `collect` đọc `input_schema` của `send_email` và chặn khi thiếu địa chỉ có `@` | 124 / 187 |
| `02-bien-ban-hop-lap-va-luu-drive.json` | khó | chu trình hợp lệ duy nhất của nền tảng (`while.loop`), và một node truy hồi có tài liệu thật để trả lời | 125 / 190 |
| `03-hai-ket-noi-buoc-duyet-cung.json` | khó | hai `ToolDefinition` khác nhau trong một graph, và cổng duyệt do **người tạo** đặt chứ không do rủi ro sinh ra | 127 / 195 |

Mỗi file có `provenance` ghi từng bước pipeline đã đi thật, kèm số hiệu
`sandbox_runs`, `run_approvals`, `eval_runs` để tra ngược trong database.

## Ba phần của một mục benchmark

1. **`spec`** — một `AgentSolutionSpecification` validate được thật (`SuccessCriterion`
   dùng trường `text`; `DataSourceKind` thuộc `uploaded_file|connected_account|web|user_input`).
2. **`structure`** — graph đúng phải có node/cạnh/config gì, cụ thể tới từng node,
   cộng với `invariants` nói vì sao hình dạng đó là hình dạng bắt buộc.
3. **`why_hard`** — vì sao mức độ đó tồn tại: mỗi gạch đầu dòng là một cách hỏng cụ
   thể mà một model yếu sẽ chọn, kèm hậu quả nó gây ra ở đâu trong nền tảng.

Ngoài ra: `eval_corrections` — những chỗ bộ sinh đề của nền tảng viết sai và người
tạo phải sửa tay, mỗi mục kèm `why`. Chúng khớp theo **hình dạng** case (loại luật,
action, category) chứ không theo id, nên chạy lại vẫn đúng. `known_eval_gaps` là các
case trượt vì nền tảng chứ không vì agent — đọc trước khi tưởng con số là điểm của agent.

## Chạy lại

Nửa **tĩnh** (không cần mạng, không cần backend):

```bash
PYTHONIOENCODING=utf-8 python -m pytest benchmark/agent_templates/test_agent_templates.py
```

Nửa **động** — dựng lại cả fleet, đi hết pipeline thật qua HTTP:

```bash
# cần: Postgres, Qdrant (đã có collection documents_kb), reranker,
#      backend uvicorn 127.0.0.1:8065 kèm --loop src.server_loop:new_event_loop
PYTHONIOENCODING=utf-8 python benchmark/agent_templates/run_fleet.py
PYTHONIOENCODING=utf-8 python benchmark/agent_templates/run_fleet.py --only gui-thu-co-duyet
```

`run_fleet.py` đăng nhập bằng hai tài khoản mẫu (`creator@example.com` để dựng và
xin duyệt, `admin@example.com` để duyệt), tự tạo Knowledge Base nếu chưa có, rồi với
mỗi fixture đi đúng bảy cửa: phỏng vấn → thay và xác nhận spec → dựng, thay và xác
nhận workflow → chạy sandbox → sinh và chạy eval → xin duyệt → duyệt → deploy. Mỗi
cửa là một lời gọi HTTP thật; không có bước nào gọi thẳng vào `src/`.

Nếu Qdrant chưa có collection thì tài liệu sẽ `failed` khi nạp:

```bash
PYTHONIOENCODING=utf-8 PYTHONPATH=. python scripts/init_qdrant.py
```

## Điều bộ này KHÔNG hứa

- **Không gọi Google thật.** Cả ba action ghi mà bộ này dùng —
  `gmail.send_email` (high), `gmail.create_draft` (medium), `google_drive.create_doc`
  (medium) — đều có `mock_response_example`, nên `sandbox_behavior` là `MOCK` và một
  lượt chạy thử không cần token nào. Các action **đọc** là `live_readonly` và bộ này
  không dùng cái nào. Email trong mọi fixture chỉ nhắm tới `titeocr27@gmail.com`.
- **Không phải một bộ điểm.** `eval_pass_rate` trong `provenance` là con số của
  đúng một lần chạy, với đúng bộ đề mà nền tảng sinh ra hôm đó. Nó không so sánh
  được với con số của agent khác, và càng không so được với số công bố dưới tên
  RAGAS hay DeepEval — thuật toán khác hẳn (ADR-0062, ADR-0063).
- **Không chấm `TakesNoAction` trên mẫu 00.** Graph đó không có node `mcp` nào nên
  `side_effecting_actions` trả về rỗng và luật ấy luôn đúng — một luật không thể sai
  thì không phải bằng chứng (ADR-0062).
