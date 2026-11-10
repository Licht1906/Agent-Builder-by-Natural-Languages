# ADR-0094 — Agent được thêm/sửa/xoá dữ liệu trong Hệ CSDL, mọi lần ghi qua người duyệt

- **Trạng thái:** accepted
- **Ngày:** 2026-11-10
- **Sửa:** ADR-0059 §5 ("query có cấu trúc", chỉ đọc) và ràng buộc C4 của UC-1 ("agent tuyệt đối không ghi/sửa/xoá ERP").

## Bối cảnh

Theo ADR-0059, agent chỉ đọc được Hệ CSDL qua view. Người dùng cần agent làm trọn việc nghiệp vụ, ví dụ nhắc nợ xong thì ghi lại ngày khách hẹn trả. Ghi rủi ro hơn đọc: một lần ghi sai làm hỏng dữ liệu thật. Vì vậy cơ chế ghi phải chặt hơn cơ chế đọc, không lỏng hơn.

## Quyết định

1. **Node riêng `data_write` ("Ghi dữ liệu")**, không gộp vào `data_query`. Nhìn canvas là biết bước nào thay đổi dữ liệu.
2. **Không text-to-SQL.** Model điền một thao tác ghi có cấu trúc: `table`, `operation` (`insert` | `update` | `delete`), `values`, `where`. `where` chỉ là các điều kiện `cột = giá trị`. `update` và `delete` bắt buộc có `where`. Code dựng câu SQL có tham số.
3. **Chỉ ghi vào bảng gốc được tick riêng** (`allowed_write_tables`), tách khỏi danh sách view được đọc. Tick một view để đọc không bao giờ đồng nghĩa với được ghi.
4. **Quyền ghi khai trong quy tắc truy cập**, cạnh quyền đọc: `rules[].tables.<bảng> = {operations, where | all_rows}`. Mặc định cấm. Phạm vi của người hỏi được:
   - AND vào `WHERE` của `update`/`delete`, nên người hỏi không chạm được dòng ngoài phạm vi;
   - ghi vào giá trị của `insert`, nên dòng mới luôn thuộc phạm vi người hỏi;
   - đối chiếu với `values`: một thao tác ghi đặt cột phạm vi thành giá trị khác (chuyển dòng sang người khác) bị từ chối.
5. **Mọi lần ghi phải được người duyệt đúng nội dung.**
   - Validator từ chối graph có đường tới `data_write` không đi qua `approved` của một `human_approval`.
   - Bước duyệt chuẩn bị sẵn thao tác ghi và hiển thị nguyên văn trên thẻ duyệt.
   - Node ghi tính lại `action_hash` và từ chối nếu khác. Mỗi lần duyệt chỉ dùng được cho một lần ghi.
   - Thao tác mà người hỏi không có quyền thì không được đưa lên thẻ duyệt.
6. **Một transaction, có trần số dòng** (`max_rows`, mặc định 20). Vượt trần thì rollback toàn bộ. Sau khi duyệt, các cổng kiểm tra được hỏi lại một lần nữa trước khi ghi.
7. **Sandbox mô phỏng việc ghi**, trừ khi lượt chạy bật "Gửi thật" (DS-09), giống hệt cách xử lý email. Eval không bao giờ ghi.
8. **Mỗi lần ghi vào audit chain** (DS-10), dạng `action`, tool `data_write`, kèm hash của payload và người duyệt.
9. **Đợt này chỉ PostgreSQL ghi được.** SQL Server và MySQL vẫn khoá read-only ở tầng phiên kết nối. `run_write` của hai loại này từ chối với `WRITE_UNSUPPORTED` trước khi kết nối, và khi probe không liệt kê bảng nào để tick.
10. **Bỏ tick một bảng ghi chỉ chặn việc ghi**, không đóng việc đọc của cả Hệ CSDL. Khi lưu quy tắc thì vẫn kiểm tra đầy đủ.

## Hệ quả

- Ràng buộc C4 của UC-1 không còn tuyệt đối. Bảo đảm thay thế gồm: bảng được tick riêng, quyền ghi khai riêng, người duyệt đúng payload, trần số dòng, và audit.
- Nên dùng tài khoản DB có quyền ghi **chỉ** trên các bảng được tick. Hiện nền tảng dùng chung một DSN cho đọc và ghi; DSN ghi riêng là việc để sau.
- Meta-agent biết vẽ `collect → data_query → human_approval → data_write → agent_llm → end`, nhưng chưa tự đề xuất nháp quyền ghi; người tạo khai quyền ghi trên form.
- ERP mẫu có thêm bảng `ar_followups` (ghi chú nhắc nợ, ngày hẹn trả) và view `v_ar_followups` để demo.
