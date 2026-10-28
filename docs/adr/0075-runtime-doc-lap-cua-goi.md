---
status: accepted
---

# Runtime độc lập của gói xuất ra

ADR-0059 §10: gói xuất ra là một runtime cố định cộng cấu hình sửa được, chạy trong hạ tầng của công ty. DS-13 dựng runtime đó: `python -m src.standalone`, image `Dockerfile.runtime`.

## Quyết định

1. **Một app riêng, không phải `src.main` bỏ bớt route.**
   - `src.standalone.app` chỉ có `/health`, `/chat`, `/approvals`, `/approvals/{id}`.
   - App này không import bất kỳ router nào trong `src.api`, nên gói không thể vô tình mọc thêm route Designer, Builder, admin hay PM.
   - Test kiểm cả danh sách route lẫn `sys.modules` trong một tiến trình mới.
2. **Chạy đúng runtime V2 của nền tảng, không có bản sao.** Gói gọi `stream_workflow_reply` như cửa production của nền tảng. Nhờ vậy phạm vi `data_query`, mặc định cấm, ràng buộc payload của DS-08 và chuỗi audit của DS-10 trong gói là cùng một đoạn code với nền tảng.
3. **Postgres riêng của gói, chỉ tạo các bảng gói dùng.**
   - Gói tạo `users`, `agents`, `agent_versions`, `deployments` (`Agent` đọc bảng này mỗi khi được nạp), `run_approvals`, `audit_events`, `audit_chain_head`, cùng mọi bảng mà khoá ngoại của chúng trỏ tới.
   - Lúc khởi động, gói tạo một agent và một version đứng thay bundle, để các dòng duyệt và audit có chỗ thuộc về; khởi động lại thì tìm thấy, không tạo trùng.
   - Gói không `create_all` toàn bộ schema, vì vài bảng của nền tảng cần extension `vector` mà Postgres của công ty có thể không có.
4. **Hệ CSDL nằm trong bộ nhớ, đọc qua đúng `PlatformDatabaseReader`.**
   - Mỗi nguồn trong `runtime.yaml` thành một `DatabaseSource` không lưu vào DB: quy tắc lấy từ `access.yaml`, view và cột lấy từ introspect DB thật lúc khởi động.
   - Lớp con chỉ thay chỗ tìm nguồn; mọi cổng kiểm vẫn là của nền tảng.
5. **Mọi secret `runtime.yaml` nhắc tới đều bắt buộc, và báo thiếu một lần cho tất cả.**
   - Mỗi secret thiếu được nêu kèm nơi đã tìm, ví dụ biến môi trường, file hay Vault.
   - Đọc secret xảy ra trước khi import ORM, vì URL Postgres của gói cũng là một secret và `src.db.base` gắn engine ngay lúc import.
   - Không khởi động được thì thoát với mã 2, trước khi mở cổng.
6. **LLM theo `base_url` OpenAI-compatible; model ánh xạ từ model bundle ghi.** Mỗi node giữ model đã được eval trên nền tảng, và chi phí được tính theo giá tham chiếu của model đó. `runtime.yaml` (`llm.models`) ánh xạ model ấy sang model công ty phục vụ; mọi model còn lại, kể cả các lời gọi nội bộ của runtime, dùng `default_model`.
7. **Người hỏi đến từ `auth`.** Bản này chỉ có `trusted_header`: thiếu header thì 401, không bao giờ rơi về một người hỏi mặc định. `oidc`, `jwt` và `custom` là việc của DS-14; khai một chế độ chưa có thì gói từ chối khởi động. (Đã có từ DS-14 — xem ADR-0076.) Lượt duyệt chỉ do chính người hỏi xem và quyết.

## Consequences

- `knowledge_search` trong gói chưa tìm thấy gì cho tới DS-18 (tài liệu cùng Qdrant đi theo gói); gói ghi cảnh báo lúc khởi động.
- Hành động gửi mail trong gói chưa có kênh, vì Gmail OAuth là của người tạo trên nền tảng; DS-16 thêm kênh mail dạng điểm cắm. Cho tới đó, node `mcp` báo lỗi một cách có kiểm soát. (Đã có từ DS-16 — xem ADR-0078.)
- Thiếu view trên DB thật thì gói chỉ cảnh báo và lượt đọc bị từ chối kèm tên view. DS-15 biến chuyện này thành từ chối khởi động. (Đã có từ DS-15 — xem ADR-0077.)
- Chi phí trong gói là ước tính theo giá tham chiếu của nền tảng, không phải chi phí thật của LLM nội bộ.
