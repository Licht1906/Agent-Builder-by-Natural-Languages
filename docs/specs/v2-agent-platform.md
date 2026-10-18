# V2 — AI Agent Platform: workflow, không còn pipeline tuyến tính

> Hand-off spec cho V2, dựng trên `VERSION2-REQUIREMENT.md`. Cùng độ sâu/phong cách với [V1](agent-builder.md) (spec của V1). Mọi quyết định kỹ thuật ở đây được kéo nguyên từ các ADR đã chốt trên map #29 — tài liệu này lắp ráp và trỏ nguồn, không re-derive.

## Problem Statement

V1 giải quyết đúng một dạng bài toán: một chuỗi bước tuần tự, mỗi bước một model, một tập tool cố định, chạy từ đầu đến cuối không rẽ nhánh. Điều đó đủ cho *"đọc email khiếu nại → tra đơn hàng → soạn thư trả lời"*, nhưng không đủ cho phần lớn quy trình nghiệp vụ thật: một chuỗi tìm tài liệu → kiểm tra điều kiện → **cần một người duyệt trước khi hành động** → hành động → trả kết quả, nơi bước nào chạy tiếp phụ thuộc vào kết quả bước trước, và một hành động có tác dụng phụ thật (gửi email, hoàn tiền) không được thực thi cho tới khi có người xác nhận.

V1 cũng chỉ có sáu tool code-cứng và không nói chuyện được với hệ thống bên ngoài thật của tổ chức — Gmail, Drive, Outlook, Teams — nơi phần lớn công việc văn phòng thực sự nằm. Và một khi agent được duyệt, V1 chỉ có một cách publish: bất kỳ ai đăng nhập cũng chat được, không có khái niệm "ai được dùng", "qua kênh nào", hay "giới hạn bao nhiêu request mỗi người".

Ba khoảng trống này — **rẽ nhánh/lặp/dừng-chờ-người**, **tool nối ra ngoài tổ chức**, và **publish có kiểm soát truy cập** — là những gì V2 phải đóng, trong khi giữ nguyên nền tảng an toàn V1 đã chứng minh: agent là dữ liệu có cấu trúc được duyệt trước khi chạy thật, không phải code tự sinh.

## Solution

Người tạo mô tả nghiệp vụ bằng ngôn ngữ tự nhiên cho **AI Solution Designer** — một meta-agent phỏng vấn để lấp những gì mô tả ban đầu còn thiếu, và sinh ra **Agent Solution Specification**: bảy trường ngôn ngữ nghiệp vụ (`goal`, `users`, `data`, `actions`, `expected_outcome`, `success_criteria`, `prohibitions`) mà người tạo xác nhận trước khi đi tiếp.

**Workflow Generator** đọc spec đã xác nhận, **chọn** (không tự vẽ tự do) một template từ một danh mục cố định gồm bảy bộ khung workflow, rồi điền cấu hình của từng node trong template đó — phần nào tham chiếu tài nguyên thật (file, MCP connection) được điền theo rule, phần nào là câu chữ nghiệp vụ (prompt, điều kiện) do LLM soạn. Kết quả là một **Workflow JSON** ở trạng thái nháp.

Người tạo chỉnh workflow này trên **Workflow Canvas** — kéo-thả node từ tám loại cố định (Agent/LLM, End, Knowledge Search, MCP, Web search, If/else, While, Human Approval), nối cạnh, sửa thuộc tính, thấy lỗi validate ngay trên sơ đồ — rồi xác nhận để nó trở thành bất biến.

**Workflow Runtime** dịch workflow đã xác nhận thành một graph LangGraph thật: rẽ nhánh theo `If/else`, lặp có trần theo `While`, và **dừng thật** tại `Human Approval` — runtime pause execution, lưu state, và chỉ đi tiếp khi có quyết định duyệt/từ chối thật, kể cả khi đang chạy thử.

Trước khi xin duyệt, người tạo chạy **sandbox**: tool chỉ-đọc chạy thật, tool có tác dụng phụ chạy mô phỏng, `Human Approval` vẫn dừng thật chờ quyết định thật — không có đường tắt nào tự động duyệt hộ. **Automatic Evaluation** sinh test case từ `success_criteria` và `prohibitions`, chấm bằng cả rule-based (đúng tool, không gọi tool cấm, tuân thủ Human Approval) lẫn LLM judge (correctness, relevance, groundedness) — sai lệch rule-based không ai, kể cả admin, vượt rào được.

Admin duyệt dựa trên hồ sơ đầy đủ như V1: spec, graph, transcript sandbox, điểm eval, quyền đã xin. Version được duyệt thì publish thành một **Deployment** — một thực thể tách biệt khỏi Agent, gắn với đúng một `agent_version_id`, có `status` (bật/tắt được mà không xoá Agent), `access_mode` (riêng tư hoặc danh sách người dùng chọn), rate limit theo từng người gọi, và hai kênh dùng song song: Web Chat (JWT) và API (API key riêng của deployment).

## User Stories

### 1. AI Solution Designer — phỏng vấn và Agent Solution Specification

1. Là người tạo agent, tôi muốn mô tả bài toán bằng ngôn ngữ tự nhiên, để hệ thống trích ra mục tiêu, người dùng, dữ liệu, hành động và kết quả mong muốn mà không cần tôi điền form kỹ thuật.
2. Là người tạo agent, tôi muốn hệ thống chỉ hỏi tôi về những gì còn thiếu hoặc mâu thuẫn trong mô tả, để không phải trả lời lại điều đã nói.
3. Là người tạo agent, tôi muốn đính kèm file hoặc kết nối tài khoản (Gmail/Drive/...) ngay lúc phỏng vấn khi agent cần dữ liệu thật, để đặc tả không chỉ là mô tả suông.
4. Là người tạo agent, tôi muốn khai rõ tiêu chí thành công và những việc agent tuyệt đối không được làm, để sau này cả evaluation lẫn kiểm soát runtime dựa đúng vào lời tôi đã khai.
5. Là người tạo agent, tôi muốn xem lại toàn bộ Agent Solution Specification và xác nhận trước khi hệ thống sinh workflow, để chắc hệ thống hiểu đúng ý tôi trước khi đi xa hơn.
6. Là người tạo agent, tôi muốn phiên phỏng vấn được lưu lại nếu tôi đóng trang giữa chừng, để quay lại tiếp tục chứ không phải làm lại từ đầu.

### 2. Workflow Generator

7. Là người tạo agent, tôi muốn hệ thống tự đề xuất một workflow nháp từ đặc tả đã xác nhận, để không phải tự vẽ sơ đồ từ con số không.
8. Là người tạo agent, tôi muốn workflow được sinh dựa trên một mẫu có sẵn phù hợp với bài toán của tôi (hỏi đáp tài liệu, thực hiện hành động có duyệt, v.v.), để nó có hình dạng đáng tin thay vì một sơ đồ LLM tự bịa.
9. Là người tạo agent, tôi muốn các trường tham chiếu tài nguyên thật (file, tool) trong workflow được điền đúng theo dữ liệu tôi đã khai, để không phải tự tay nối lại từng node.
10. Là người tạo agent, tôi muốn nhận được một workflow nháp ngay cả khi không mẫu nào khớp hoàn hảo, để tôi có điểm khởi đầu để sửa trên canvas thay vì bị từ chối hoàn toàn.

### 3. Workflow Canvas

11. Là người tạo agent, tôi muốn xem workflow của mình dưới dạng sơ đồ kéo-thả được, để hiểu và sửa hình dạng của nó trực quan.
12. Là người tạo agent, tôi muốn thêm node mới từ một bảng loại node cố định (Agent/LLM, End, Knowledge Search, MCP, Web search, If/else, While, Human Approval), nối cạnh giữa các node, và xoá node, để tự tay chỉnh workflow.
13. Là người tạo agent, tôi muốn bấm vào một node để sửa thuộc tính của nó (prompt, điều kiện, tool, số vòng lặp tối đa...), để tinh chỉnh hành vi từng bước.
14. Là người tạo agent, tôi muốn thấy lỗi cấu trúc (node chưa nối, tool rủi ro cao thiếu bước duyệt phía trước, nhánh không dẫn tới kết thúc) hiển thị ngay trên sơ đồ, để sửa trước khi xác nhận.
15. Là người tạo agent, tôi muốn workflow ở trạng thái nháp lưu được dù chưa hoàn chỉnh, để không bị chặn giữa chừng lúc đang chỉnh.
16. Là người tạo agent, tôi muốn xác nhận workflow khi đã ưng ý, để nó trở thành bất biến và đủ điều kiện chạy sandbox.

### 4. Workflow Runtime

17. Là người tạo agent, tôi muốn workflow của tôi chạy đúng như sơ đồ đã vẽ — rẽ nhánh If/else theo điều kiện, lặp While có trần, dừng đúng ở Human Approval — để hành vi chạy thật khớp với những gì tôi đã xác nhận.
18. Là người tạo agent, tôi muốn một lần chạy dừng lại thật sự tại bước cần người duyệt và giữ nguyên trạng thái, để không có hành động có tác dụng phụ nào xảy ra trước khi có người xác nhận.
19. Là người duyệt (admin), tôi muốn nhận được yêu cầu phê duyệt đúng lúc workflow dừng lại chờ, kèm đủ ngữ cảnh để quyết định, để không phải đoán agent đang cần gì.
20. Là người duyệt (admin), tôi muốn quyết định duyệt hoặc từ chối (kèm lý do khi từ chối) khiến workflow đi đúng nhánh tương ứng, để việc tôi quyết định có tác dụng thật.
21. Là admin, tôi muốn mọi lần chạy — cả sandbox lẫn production — được ghi lại đầy đủ (node đã chạy, tool đã gọi, quyết định duyệt), để điều tra được khi có sự cố.

### 5. Các tool cốt lõi

22. Là người tạo agent, tôi muốn agent tìm kiếm được trong tài liệu PDF/DOCX tôi đã tải lên, để trả lời dựa trên đúng nguồn của tổ chức tôi.
23. Là người tạo agent, tôi muốn agent gọi được vào Gmail/Drive/Outlook/Teams qua MCP, để nó thao tác trên dữ liệu thật của tôi thay vì chỉ trả lời suông.
24. Là người tạo agent, tôi muốn agent tìm kiếm được trên web khi câu hỏi cần thông tin ngoài tài liệu nội bộ.
25. Là người tạo agent, tôi muốn mỗi tool cho tôi biết đầu vào/đầu ra, quyền truy cập cần có, và mức rủi ro của nó, để tôi hiểu vì sao có tool bị kiểm soát chặt hơn.
26. Là người tạo agent, tôi muốn đăng ký một MCP tool mới (ngoài danh mục sẵn có) và gửi admin duyệt, để nối agent vào một dịch vụ nội bộ của đội tôi.
27. Là người tạo agent, tôi muốn không thể tự khai một tool rủi ro cao là rủi ro thấp, để chính tôi cũng không vô tình phá vỡ kiểm soát của tổ chức.

### 6. Sandbox

28. Là người tạo agent, tôi muốn chạy thử toàn bộ workflow của mình trước khi xin duyệt, để biết nó có thực sự làm đúng việc trước khi đưa vào chạy thật.
29. Là người tạo agent, tôi muốn tool chỉ-đọc chạy thật và tool có tác dụng phụ chỉ mô phỏng trong lúc chạy thử, để không gửi email thật hay đổi dữ liệu thật khi đang thử.
30. Là người tạo agent, tôi muốn thấy đúng node nào đã chạy, tool nào đã gọi, input/output của từng bước, và nhánh nào được chọn ở mỗi điểm rẽ nhánh, để lần ra chỗ agent sai.
31. Là người tạo agent, tôi muốn Human Approval vẫn dừng lại chờ quyết định thật ngay cả khi đang chạy thử, để bằng chứng sandbox phản ánh đúng những gì sẽ xảy ra khi chạy thật.
32. Là người tạo agent, tôi muốn thấy token, thời gian và chi phí ước tính của lần chạy thử, để biết chi phí thực tế trước khi xin duyệt.
33. Là người tạo agent, tôi muốn agent dừng lại và báo lỗi tử tế khi vượt ngân sách, kể cả khi đang ở trong một vòng lặp While, để một vòng lặp không đốt hết ngân sách của tôi.

### 7. Automatic Evaluation

34. Là người tạo agent, tôi muốn hệ thống tự sinh test case từ tiêu chí thành công và những việc bị cấm tôi đã khai, để không phải tự nghĩ cách kiểm tra agent.
35. Là người tạo agent, tôi muốn evaluation kiểm tra được cả những điều không thể chấm bằng cảm tính LLM — workflow có chạy xong không, agent có gọi đúng tool không, có gọi tool bị cấm không, có tuân thủ đúng quyết định Human Approval không — bằng luật cứng đọc thẳng từ transcript, không chỉ dựa vào một LLM giám khảo.
36. Là người tạo agent, tôi muốn xem kết quả từng case kèm lý do đạt/không đạt, để biết phải sửa gì.
37. Là admin, tôi muốn một lỗi rule-based (gọi tool bị cấm, làm hành động nhạy cảm sau khi bị từ chối duyệt) không thể được vượt rào bởi bất kỳ ai, để những sai phạm an toàn không bao giờ bị nương tay.
38. Là người tạo agent, tôi muốn khi chưa đạt ngưỡng thì được giải thích rõ vì sao chưa xin duyệt được, để biết việc tiếp theo phải làm.

### 8. Quản lý, Publish và triển khai Agent

39. Là người tạo agent, tôi muốn publish version đã duyệt thành một Deployment, để nó bắt đầu phục vụ end-user qua Web Chat hoặc API.
40. Là người tạo agent, tôi muốn bật/tắt Deployment mà không phải xoá Agent, để tạm dừng phục vụ rồi bật lại đúng bản đang chạy mà không cần chọn lại version.
41. Là người tạo agent, tôi muốn chọn Deployment của mình chỉ tôi dùng được, hoặc mở cho một danh sách người dùng cụ thể, để kiểm soát ai chạm được vào agent đang publish.
42. Là người tạo agent, tôi muốn có một API key riêng cho Deployment của mình và tạo lại được khi cần, để nối agent vào hệ thống khác của đội tôi một cách an toàn.
43. Là người tạo agent, tôi muốn giới hạn số request mỗi người gọi tới Deployment của mình, để kiểm soát chi phí khi nhiều người cùng dùng chung một Deployment.
44. Là end-user, tôi muốn nhiều người dùng chung một Deployment mà cuộc trò chuyện của tôi không lẫn với người khác, để dữ liệu của tôi luôn tách biệt.
45. Là người tạo agent, tôi muốn rollback nhanh về version cũ đã duyệt nếu bản mới có lỗi, để xử lý sự cố mà không phải chờ duyệt lại.
46. Là người tạo agent, tôi muốn xem dashboard số lượt chạy, lỗi, chi phí và các execution gần nhất của Deployment, để biết agent của tôi đang phục vụ ra sao.
47. Là admin, tôi muốn mọi thay đổi trên Deployment (ai publish, khi nào) được ghi lại, để phục vụ audit.

## Implementation Decisions

### Quyết định trung tâm: năm cấu trúc, không còn một `AgentSpec` gộp chung

V1 gộp mọi thứ vào một `AgentSpec` (interview slots + steps + guardrails + budget). `VERSION2-REQUIREMENT.md` tách thành **năm cấu trúc** phải chốt trước khi code, mỗi cấu trúc một tầng trách nhiệm riêng, mỗi cấu trúc một ticket của map #29:

| # | Cấu trúc | Ticket | ADR |
|---|---|---|---|
| 1 | Agent Solution Specification | #30 | [ADR-0020](../adr/0020-v2-agent-solution-specification-schema.md) |
| 2 | Workflow JSON | #32 | [ADR-0022](../adr/0022-v2-workflow-json-schema.md) |
| 3 | Node Definition | #31 | [ADR-0021](../adr/0021-v2-node-definition-schema.md) |
| 4 | Tool Definition | #34 | [ADR-0023](../adr/0023-v2-tool-definition-schema.md) |
| 5 | Deployment Definition | #40 | [ADR-0029](../adr/0029-v2-deployment-definition-schema.md) |

`Agent`/`AgentVersion` (V1's identities) đều được giữ lại, nhưng `AgentVersion` giờ mang hai tài liệu thay vì một — `spec: AgentSolutionSpecification` và `workflow: WorkflowJSON` — mỗi tài liệu có cổng xác nhận riêng của nó:

```
spec_pending_confirmation → spec_confirmed
  → (Workflow Generator chạy) →
workflow_pending_confirmation → workflow_confirmed
  → (sandbox, eval, xin duyệt — như V1) →
approved → (Deployment trỏ tới) deployed
```

### 1. Agent Solution Specification ([ADR-0020](../adr/0020-v2-agent-solution-specification-schema.md))

```
AgentSolutionSpecification
  goal: str
  users: list[str]
  data: list[DataSource]                    # { name, kind, ref }
  actions: list[str]
  expected_outcome: list[str]
  success_criteria: list[SuccessCriterion]  # { text, category }
  prohibitions: list[str]

SuccessCriterionCategory =
  | "workflow_success" | "tool_correctness" | "human_approval_compliance"
  | "source_groundedness" | "correctness" | "relevance"
```

Sáu trong bảy trường là ngôn ngữ nghiệp vụ tự do — spec nói *cái gì*, không nói *bằng cách nào*, không hé lộ loại node hay template nào sẽ được dùng. `data` là ngoại lệ duy nhất mang tham chiếu tài nguyên thật (`ref`) vì người tạo có thể đính kèm file/kết nối MCP ngay lúc phỏng vấn. `success_criteria.category` là enum đóng sáu giá trị khớp thẳng sáu tiêu chí Automatic Evaluation của requirement §7 — "gọi tool bị cấm" cố tình *không* nằm trong sáu giá trị này, vì check đó đọc thẳng `prohibitions`.

Việc dò thiếu/mâu thuẫn khi phỏng vấn sống ở một `InterviewSession` riêng, không versioned — spec chỉ được dựng khi phiên phỏng vấn hoàn tất, nên spec không bao giờ ở trạng thái dở dang. Điều kiện dừng phỏng vấn không xác định (LLM tự quyết định khi nào đủ rõ) — khác V1 (ADR-0014), đánh đổi độ sâu câu trả lời lấy một chỉ báo tiến độ dự đoán được.

### 2. Node Definition ([ADR-0021](../adr/0021-v2-node-definition-schema.md))

Danh mục đóng, định nghĩa trong code như `TOOL_REGISTRY` của V1:

```
AgentLlm         { prompt: str, model: str }                              -> ["done"]
End              {}                                                        -> []
KnowledgeSearch  { source_ids: list[str] }                                 -> ["found"]
MCP              { tool_id: str, action: str }                            -> ["done"]
WebSearch        {}                                                        -> ["done"]
IfElse           { condition: str }                                       -> ["true", "false"]
While            { condition: str, max_iterations: int }                  -> ["loop", "exit"]
HumanApproval    { approver_role: str, message_template: str }            -> ["approved", "rejected"]
```

`AgentLlm` không tự gọi tool nội bộ (khác `Step` của V1) — dùng tool nghĩa là vẽ một node tool riêng và nối nó vào `AgentLlm`, để graph luôn là nguồn sự thật duy nhất về tool nào thực sự chạy. `While` là node quyết định phẳng (không phải subgraph lồng) với một cổng `loop` cho phép quay lại node trước đó — chu trình chỉ được phép qua đúng cổng này, và `max_iterations` bắt buộc trên mọi `While`. `start_node_id` luôn tường minh, không suy luận. Một cổng output nối tối đa một cạnh; một cổng input nhận không giới hạn số cạnh vào; mọi đường từ `start_node_id` phải tới được `End`.

### 3. Workflow JSON ([ADR-0022](../adr/0022-v2-workflow-json-schema.md))

```
WorkflowJSON
  start_node_id: str
  nodes: list[NodeInstance]     # {id, type, config, position} — theo ADR-0021
  edges: list[Edge]
  metadata: WorkflowMetadata

Edge
  id: str
  source_node_id: str
  source_port: str        # một trong các output port đặt tên của node nguồn
  target_node_id: str

WorkflowMetadata
  budget: { max_total_tokens: int, max_wall_seconds: int }
  template_id: str | None
```

Bao bì phẳng — `nodes`/`edges`/`metadata` là các trường ngang hàng, khớp đúng thứ một canvas kéo-thả cần serialize. Không có trường `allowed_tools`: tool nào một workflow thực sự dùng đọc thẳng từ node `MCP`/`KnowledgeSearch`/`WebSearch` có trong graph. `metadata.budget` là nơi trần chi phí của V1 (`AgentSpec.budget`) chuyển sang, vì Agent Solution Specification cố tình không mang trường kỹ thuật nào. Luật cấu trúc của ADR-0021 (chu trình/fan-out/reachability) chỉ được cưỡng chế tại thời điểm xác nhận (`workflow_pending_confirmation → workflow_confirmed`), không phải mỗi lần lưu nháp.

### 4. Tool Definition ([ADR-0023](../adr/0023-v2-tool-definition-schema.md))

```
ToolDefinition
  id: str
  family: "knowledge_search" | "mcp" | "web_search"
  name: str
  source: "built_in" | "creator_registered"
  status: "approved" | "pending" | "rejected" | "disabled"
  credential: ToolCredential
  actions: list[ToolAction]
  usage_scope: "public" | "org_restricted" | "creator_only"
  requires_human_approval: bool

ToolAction
  name: str
  input_schema: JSONSchema
  output_schema: JSONSchema
  risk_level: "low" | "medium" | "high"
  sandbox_behavior: "live_readonly" | "mock"
  mock_response_example: JSONSchema | null   # bắt buộc khi sandbox_behavior = "mock"
```

Registry lai: tool xây sẵn (Knowledge Search, Web search, MCP xây sẵn, OAuth-wrapper Gmail/Drive) định nghĩa trong code; tool người tạo tự đăng ký là row DB bắt đầu ở `pending`, chỉ vào danh sách dùng được khi admin duyệt. Một `ToolDefinition` là **một kết nối/MCP server**, không phải một action — nhiều `actions[]` chia sẻ chung `credential`/`usage_scope`. `risk_level` hệ thống tự gán theo hiệu ứng của action (đọc=`low`, ghi/gửi không thể hoàn tác=`high`), **không bao giờ tự khai** — đây là điều feed vào `sandbox_behavior` (mặc định `mock` cho rủi ro trung/cao) và `requires_human_approval` (bắt buộc với rủi ro cao), thứ mà graph validator của ADR-0021 cưỡng chế cấu trúc: từ chối lưu một node `MCP` rủi ro cao nếu phía trước nó trên graph không có `HumanApproval`.

### 5. Deployment Definition ([ADR-0029](../adr/0029-v2-deployment-definition-schema.md), triển khai ở [ADR-0036](../adr/0036-v2-deployment-implementation.md))

```
Deployment
  id: int
  agent_id: int              # FK, duy nhất — tối đa một Deployment mỗi Agent
  agent_version_id: int      # FK, con trỏ có thể đổi (kế thừa ADR-0013)
  status: "active" | "disabled"
  access_mode: "private" | "selected_users"
  api_key_hash: str
  rate_limit_per_user: int
  created_by: int
  created_at: datetime
  updated_at: datetime

DeploymentAllowedUser
  deployment_id: int
  user_id: int
```

Deployment là một entity độc lập, mở rộng con trỏ `Agent.deployed_version_id` của V1 (ADR-0013) chứ không thay thế — publish version mới là ghi đè `agent_version_id` trên cùng một row, không tạo row mới. Lúc triển khai (#50, ADR-0036 §1) cột `agents.deployed_version_id` được backfill rồi **xoá hẳn**: hai con trỏ ghi được là hai câu trả lời cho "version nào đang chạy", và ngày chúng lệch nhau không có gì trong schema nói được cái nào sai. Publish lần đầu mặc định `private` — đây là chỗ V2 phá vỡ hành vi V1 một cách có chủ ý (ADR-0036 §2). `status: disabled` chính là "unpublish" mà ADR-0013 từng để ngỏ, không cần làm `agent_version_id` nullable — tắt rồi bật lại không cần chọn lại version. `access_mode` chỉ còn `private` (chỉ owner) và `selected_users` (owner + danh sách trong `DeploymentAllowedUser`) — bỏ `organization` vì V2 chưa có mô hình tổ chức/tenant nào, và bỏ public link vì requirement doc tự nói có thể để ngoài MVP. Không có trường `auth_mode` lưu trữ: Web Chat (JWT) và API (`api_key_hash` riêng của deployment, một key, tạo lại được) đều luôn sẵn sàng song song, không phải một lựa chọn per-deployment. `rate_limit_per_user` áp theo từng người gọi, không phải tổng theo deployment — đóng đúng khoảng trống ADR-0013 từng nêu. Cách đếm chốt ở #51 ([ADR-0037](../adr/0037-v2-deployment-rate-limiting.md)): cửa sổ trượt 60 giây theo `(deployment_id, Caller.key)` — đúng cái khoá mà checkpointer dùng để tách hội thoại, nên "cùng một người gọi" chỉ có một định nghĩa — tính ngay tại cửa trước khi run bắt đầu, cả Web Chat lẫn API; vượt trần là 429 kèm `Retry-After`. Chủ sở hữu đặt số này bằng `PUT /agents/{id}/deployment/rate-limit` (1..1000).

### Workflow Generator & Template Catalog ([ADR-0025](../adr/0025-v2-workflow-template-catalog.md))

```
TemplateSpec
  id: str
  name: str
  description: str
  nodes: list[NodeInstance]       # bộ khung cố định, config chứa placeholder
  edges: list[Edge]
  start_node_id: str
  required_data_kinds: list[DataSource.kind]
  source: "built_in" | "creator_submitted"
  status: "pending" | "approved" | "rejected" | "disabled"
```

Một template là **bộ khung graph tĩnh**, không phải builder tham số hoá — số node/cạnh không đổi giữa các lần dùng, để `template_id` còn ý nghĩa cho dashboard so sánh version. Registry một tầng, lai như `ToolDefinition`: bảy template xây sẵn (`document_qa`, `action_with_approval`, `planning_helper`, `customer_service`, `document_comparison`, `internal_knowledge_assistant`, `data_enrichment`) ở `approved`; người tạo chỉ nộp template mới qua "lưu thành template" từ một workflow **đã xác nhận** (không bao giờ từ graph tự do hoặc LLM tự vẽ), vào hàng chờ admin duyệt.

Chọn template: LLM của Workflow Generator đề xuất `template_id` từ nội dung spec, một rule validator từ chối đề xuất nếu thiếu nguồn dữ liệu cần thiết hoặc thiếu `HumanApproval` phía trước một action rủi ro cao — không khớp hoàn hảo vẫn tạo ra một draft `workflow_pending_confirmation` thay vì bị chặn cứng. Điền cấu hình chia theo loại trường: trường tham chiếu tài nguyên thật (`source_ids`, `tool_id`) điền theo rule từ `data[]`/`actions[]` của spec; trường ngôn ngữ tự nhiên (`prompt`, `condition`) do LLM soạn.

### Workflow Runtime & Human Approval pause/resume ([ADR-0024](../adr/0024-v2-human-approval-pause-resume.md))

Dừng tại `HumanApproval` dùng đúng cơ chế `interrupt()` của LangGraph; resume dùng `Command(resume=...)` — vị trí graph (đang ở nhánh nào, vòng lặp thứ mấy) không cần theo dõi riêng vì checkpointer Postgres (đã có từ V1, ADR-0004) tự lưu đúng vị trí đó.

```
run_approvals
  id, run_id, node_id, occurrence_index
  approver_role      # chỉ mang tính thông tin — bất kỳ admin nào cũng resume được
  message, status, decided_by, decided_at, reason
```

`run_approvals` là bề mặt truy vấn/audit (danh sách hàng chờ, lịch sử quyết định) — checkpoint LangGraph vẫn là nguồn sự thật để resume. Key `(run_id, node_id, occurrence_index)` append-only vì một `HumanApproval` bên trong `While` có thể dừng nhiều lần trong cùng một run. `run_id` là identity mới, khác `ThreadRef` của V1 (một cuộc hội thoại nhiều lượt) vì một run workflow là một lần chạy graph có thể dừng và tiếp tục nhưng vẫn là một thực thể. Quyết định (và lý do từ chối) được ghi vào `RuntimeState.messages` để node phía sau hành động dựa trên *vì sao*, không chỉ *cổng nào*. `max_wall_seconds` loại trừ thời gian đang pause chờ người — một reviewer chậm không làm cạn ngân sách của agent.

### Sandbox mở rộng cho branching/looping ([ADR-0026](../adr/0026-v2-sandbox-branching-looping.md))

Không có trần lặp riêng cho sandbox — `While` dùng đúng `max_iterations` của chính nó, giữ nguyên nguyên tắc V1 "sandbox và production là cùng một lần chạy". Chạm trần thì đi qua cổng `exit` như điều kiện đã sai, run vẫn `finished` chứ không `failed`. Mỗi entry transcript của node quyết định (`IfElse`/`While`/`HumanApproval`) mang thêm `port_taken`; mỗi lượt lặp trong `While` có entry riêng gắn `iteration`. Độ phủ nhánh/vòng lặp (mọi cổng của mọi node quyết định, cả hai phía của `HumanApproval`) được tính theo **hợp** trên toàn bộ các lần chạy sandbox của một version — chỉ mang tính thông tin cho hồ sơ duyệt, không chặn `mark_sandbox_passed`.

### Automatic Evaluation mở rộng ([ADR-0028](../adr/0028-v2-workflow-level-eval-checks.md))

Eval chạy `HumanApproval` qua **chế độ thực thi thứ ba**, khác `interrupt()` thật của sandbox/production: bộ chạy eval cấp quyết định từ một bảng quyết định theo từng case, không treo chờ người. `success_criteria.category` định tuyến case theo hai đường chấm điểm không giao nhau: `correctness`/`relevance`/`source_groundedness` vẫn chấm bằng LLM judge như V1; `workflow_success`/`tool_correctness`/`human_approval_compliance` chấm bằng cách đọc thẳng transcript, không qua judge. "Gọi tool bị cấm" sinh một case riêng cho mỗi mục trong `prohibitions`, cố tình soạn để thử khiêu khích hành động cấm đó. Một lỗi rule-based (gọi tool cấm, thực hiện hành động nhạy cảm sau khi bị từ chối duyệt) **không thể vượt rào** — khác với điểm judge dao động, đây là sự kiện xác định đọc thẳng từ transcript.

### Workflow Canvas ([ADR-0027](../adr/0027-v2-workflow-canvas-ux.md))

Xây mới, không kế thừa sơ đồ chỉ-đọc của V1 (ADR-0018 tắt drag/connect/delete có chủ đích). Biến thể thắng: sidebar bên trái liệt kê tám loại node để kéo-thả vào canvas; bấm một node mở panel thuộc tính nổi (góc trên-phải) kèm nút xoá; node lỗi có viền đỏ + badge cảnh báo, cộng một số đếm lỗi tổng trên toolbar cạnh nút Save. Layout ngang trái-sang-phải (khác cột trên-xuống của V1), khớp cách các template mô tả graph dạng `A → B → C`.

### Hồ sơ duyệt cho version V2 ([ADR-0035](../adr/0035-v2-approval-dossier.md))

Một `Dossier` duy nhất, mang bộ tài liệu nào mà version thật sự có: version V1 được duyệt trên `spec`/`flow`/`tools`/`allowed_domains`/`costs`, version V2 trên `solution_spec`/`workflow`/`actions`/`budget` — bộ còn lại là `null`, không phải bản rỗng, và client đọc "đây là hồ sơ loại nào" từ chính bộ tài liệu đã tới chứ không từ một cờ. Quyền V2 là cặp `tool_id.action` đọc thẳng từ các node `MCP` (ADR-0022 §3 cố tình không có `allowed_tools`), rủi ro lấy từ Tool Definition registry; `scopes` của kết nối đứng chỗ `allowed_domains` của V1. Action registry không tra được thì **vẫn liệt kê**, đánh dấu rõ và xếp trên các action chỉ-đọc — "không ai bảo chứng được" không phải là "an toàn". Điểm eval hiện kèm tách rule-based / judge và số case vi phạm luật cứng, và màn hình thôi mời bấm vượt rào khi con số đó khác 0 (ADR-0028 §6).

### Xác thực và phân quyền (kế thừa V1)

JWT hai vai trò `creator`/`admin` như [ADR-0002](../adr/0002-auth-and-two-roles.md), không đổi. `AddressableAgent` của V1 (ADR-0013) — cổng cho phép người không phải owner xem/chat một agent — giờ đọc qua `Deployment.status == "active"` thay vì `Agent.deployed_version_id is not None`, và hẹp thêm theo `access_mode`/`DeploymentAllowedUser`: một Deployment `selected_users` ẩn với người không có trong danh sách, kể cả trên `GET /agents/running`.

## Out of Scope

- Rẽ nhánh/vòng lặp tuỳ ý ngoài `If/else`/`While` — không có topology do LLM tự phát minh tự do; luôn qua template catalog + canvas.
- Đa agent orchestration vượt ngoài tám loại node cố định.
- Embed widget, bot Slack/Teams, agent marketplace, chia sẻ qua public link — requirement doc tự để những thứ này lại ("có thể để sau").
- Đa tenant/workspace (`org_id`/`tenant_id`) — chưa có mô hình tổ chức nào ở V2; `access_mode` cố tình không có giá trị `organization` cho tới khi việc này được quyết.
- Kế hoạch migrate từ V1 — V2 là greenfield, không phải đường nâng cấp.
- Nhiều Deployment đồng thời cho một Agent, hoặc lịch sử publish dạng append-only — V2 giữ đúng một Deployment/Agent, kế thừa tinh thần con trỏ của ADR-0013.

## Further Notes

### Việc chưa thiết kế, biết trước là sẽ cần

Danh sách này kéo nguyên từ mục "Not yet specified" của map #29 tại thời điểm ticket này đóng — mỗi mục là một quyết định thật đang chờ, không phải một khoảng trống bị quên:

- **Mô hình đa tenant/workspace** (`org_id`/`tenant_id`) — điều kiện "nếu có Workspace/Organization" của requirement doc chưa được quyết là có hay không. `access_mode` (Deployment Definition) phụ thuộc trực tiếp vào quyết định này.
- **Vai trò ngoài creator/admin** — Human Approval có cần một vai trò "approver" riêng, tách khỏi admin?
- **`UserConnection`** (OAuth token theo từng end-user, khoá theo `(user_id, provider)`) — ADR-0023 giả định bảng này tồn tại nhưng chưa thiết kế schema hay cơ chế xoay refresh-token. Là điều kiện tiên quyết cho MCP tool chạy thật và cho config-fill dựa trên `data[].ref` của ADR-0025.
- ~~**Cơ chế đếm rate limit**~~ (token-bucket/sliding-window, tầng enforce) — đã chốt ở [ADR-0037](../adr/0037-v2-deployment-rate-limiting.md) (#51): cửa sổ trượt 60 giây theo `(deployment_id, Caller.key)`, đếm ngay tại cửa (dependency của `chat` và `invoke`) và giữ trong bộ nhớ tiến trình. Còn để ngỏ: nhiều worker thì trần nhân lên theo số worker — ngày scale ngang là ngày phải chuyển bộ đếm ra storage dùng chung.
- **Cơ chế ngân sách qua nhánh/vòng lặp** — ADR-0022 chốt *nơi* trần nằm (`WorkflowJSON.metadata.budget`), chưa chốt liệu một run qua `While`/`If-else` có cần gì thêm ngoài ledger tuyến tính hiện có.
- ~~**Endpoint bật/tắt Deployment và quản lý `access_mode`/`DeploymentAllowedUser`**~~ — đã chốt ở [ADR-0036](../adr/0036-v2-deployment-implementation.md) §3 (#50): `PUT /agents/{id}/deployment/status` và `PUT /agents/{id}/deployment/access`, tách riêng chứ không gộp một `PATCH`.
- **UI hàng chờ duyệt** cho tool `pending` (ADR-0023) và template `pending` (ADR-0025) — chỉ mới có state cần expose, chưa có màn hình.
- **Heuristic đọc/ghi** phân loại `risk_level` (ADR-0023 §3), giờ có hai điểm gọi (gán risk, chọn template ADR-0025) — vẫn implicit theo từ khoá, đáng làm một pass tách bạch rõ ràng.
- **UI hồ sơ duyệt** hiển thị độ phủ nhánh/vòng lặp (ADR-0026 §5) — dữ liệu đã có, màn hình chưa thiết kế.
- **Form thuộc tính từng loại node trên Canvas** (ADR-0027) — chỉ mới prototype `title`/`prompt` của `AgentLlm` làm đại diện.
- **Storage upload file + luồng kết nối OAuth** cho `data[].ref` (ADR-0020) — cần có từ ngày đầu của bước phỏng vấn, chưa thiết kế nơi lưu.
- **Dashboard so sánh/rollback version** vượt ngoài bản thân schema Deployment Definition.
