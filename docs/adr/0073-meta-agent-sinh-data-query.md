---
status: accepted
---

# Meta-agent: nhu cầu đọc dữ liệu nghiệp vụ → `data_query`

DS-11 biến năng lực đọc Hệ CSDL (ADR-0059 §5) thành thứ người tạo mô tả được bằng lời: "Tạo trợ lý công nợ… khi khách quá hạn trên 30 ngày thì soạn email nhắc nợ gửi khách" phải ra một canvas `collect → data_query → agent_llm → human_approval → <action>`.

## Quyết định

1. **Designer đọc dữ liệu nghiệp vụ có cấu trúc là `kind: database`.** Đó là công nợ, hoá đơn, đơn hàng, tồn kho, khách hàng, nhân viên… nằm trong ERP, CRM hay CSDL, kể cả khi người tạo không nói chữ "cơ sở dữ liệu". Cách model tự viết (`ERP`, `csdl`, `SQL`, `Hệ CSDL`…) được chuẩn hoá về `database`. `ref` vẫn để `null`: người tạo tự gắn Hệ CSDL trên màn hình spec.
2. **`source_id` và `views` được gắn bằng quy tắc, sau khi graph đã vẽ xong và trước khi lưu** (`generator.bound_databases`), cho cả đường Composer lẫn đường template:
   - Node `data_query` thứ n đọc nguồn `database` thứ n của spec.
   - `views` là các view đã tick của Hệ CSDL, **trừ view nhân viên** mà quy tắc truy cập dùng để tra người hỏi; view đó để quyết định phạm vi, không để đọc.
   - Mọi giá trị model viết vào đây đều bị bỏ.
   - Nguồn chưa gắn `ref`, hoặc không phải của người tạo, thì để trống cho canvas báo đỏ.
3. **Composer và Workbench được dạy cùng một quy tắc:**
   - Mỗi Hệ CSDL đúng một `data_query`, không bao giờ `knowledge_search` hay `mcp`.
   - Prompt không nói về phạm vi, vì node tự lọc theo người hỏi.
   - Nếu vừa đọc vừa gửi thì khung là `collect → data_query → agent_llm → human_approval → mcp → agent_llm → end`.
4. **Ba luật kiểm hình dạng mới**, trả lời từ chối cho model sửa:
   - spec có Hệ CSDL mà graph thiếu `data_query`;
   - graph có `data_query` mà spec không có Hệ CSDL;
   - số `data_query` nhiều hơn số Hệ CSDL.
5. **Ngưỡng chọn dòng không bắt thành `if_else` khi graph đọc Hệ CSDL.** Luật "mỗi ngưỡng là một `if_else`" (2026-08-30) được bỏ qua khi graph có `data_query`: "quá hạn trên 30 ngày" là bộ lọc của truy vấn. Composer vẫn được vẽ `if_else` nếu bài toán chia bậc.
6. **Hai template mới trong thư viện:** `database_qa` (`data_query → agent_llm → end`) và `database_outreach` (khung của demo). Template cũng là đường dự phòng khi Composer thất bại.
7. **Case golden của DS-11 nằm ở file riêng** `golden/meta_agent_data_source_v1.jsonl`. `THRESHOLDS` của `meta_agent_gate` được đăng ký trước cho đúng tập case của `meta_agent_v1.jsonl`; thêm case vào đó là dời ngưỡng, một quyết định cần người duyệt riêng.

## Considered Options

- **Để model điền `source_id`** — loại: ADR-0025 §5. Một id hợp lý mà model bịa ra trông như đã nối, nhưng không trỏ tới đâu cả.
- **Đưa `views` vào spec** — loại: spec là văn bản nghiệp vụ của người tạo. View là chi tiết của Hệ CSDL và đổi được sau khi spec đã xác nhận.
- **Giữ luật ngưỡng → `if_else` cả với `data_query`** — loại: chính khung demo trong spec không có `if_else`, và Composer sẽ bị từ chối ba lần rồi rơi về template.

## Consequences

- `action` của node gửi vẫn do người tạo chọn trên canvas. Khi chưa chọn, validator chưa biết hành động có rủi ro hay không. Khi đã chọn `send_email`, xoá `human_approval` là báo đỏ và không xác nhận được.
- Trong khung demo, `collect` hỏi người dùng địa chỉ nhận (`to`), trong khi email khách nằm trong dữ liệu vừa đọc. Luật `verbatim` của DS-08 cũng bỏ mọi địa chỉ người dùng chưa từng gõ. Để demo bước 5 chạy được, cần quyết định cho phép giá trị đọc từ `data_query` làm nguồn hợp lệ của tham số `email` — việc này để cho DS-24 hoặc một ticket riêng.
