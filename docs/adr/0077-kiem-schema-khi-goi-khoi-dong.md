---
status: accepted
---

# Kiểm schema khi gói khởi động

ADR-0059 §4: agent được dựng và chạy thử trên DB mẫu/staging, còn gói xuất ra chạy trên DB production của công ty. Hai DB đó phải cùng schema ở những chỗ agent dùng. DS-13 chỉ cảnh báo khi thiếu view và để lượt đọc bị từ chối. DS-15 biến chuyện này thành từ chối khởi động.

## Quyết định

1. **Bundle mang theo cột của các view mà node `data_query` đọc (`database_schema`).**
   - Dạng `{source_id: {view: [cột]}}`, lấy từ lần probe gần nhất của Hệ CSDL trên nền tảng (`views_built_on`), tức đúng schema agent đã được dựng và eval trên đó.
   - Chỉ có tên view và tên cột, không có dữ liệu hay DSN.
   - Node `data_query` chỉ ghi view; model chọn cột lúc chạy. Không có snapshot này thì gói không biết "cột agent dùng" là gì, và cũng không phân biệt được cột mới với cột cũ.
   - Import bundle vào nền tảng đọc lướt qua trường này (khai trong `BundleImport`, không dùng).
2. **Những gì phải có trên DB thật:**
   - mỗi view mà node `data_query` đọc, kèm các cột trong `database_schema`;
   - view nhân viên, kèm cột khớp (`match_by`) và mọi cột thuộc tính;
   - mỗi view mà một rule cấp, kèm các cột mà `where` của rule lọc theo.
   Kiểm bằng `schema_problems` của lớp dialect, nên nền tảng và gói hỏi cùng một câu theo cùng một cách. Một bảng gốc nằm ở chỗ của view bị nêu là bảng gốc.
3. **Thiếu thì từ chối khởi động, nêu mọi chỗ thiếu một lần.**
   - Mỗi dòng nêu Hệ CSDL, view và cột; gom cho mọi Hệ CSDL, kể cả Hệ CSDL không kết nối được.
   - Giống cách báo thiếu secret (ADR-0075 §5): sửa một lần, không phải khởi động lại từng lỗi một. Thoát mã 2 trước khi mở cổng.
4. **Cột thừa thì cảnh báo, và agent không đọc cột đó.**
   - Trên view mà `database_schema` có mô tả, cột không có lúc dựng agent (và không có trong rule) được ghi cảnh báo lúc khởi động và bị loại khỏi `view_columns`.
   - Agent chưa từng được eval với cột đó, và một cột mới trên view ERP có thể là lương cũng như là tiện ích. Muốn agent đọc thì xuất lại gói từ nền tảng.
5. **Bundle không có `database_schema`** (viết tay, hoặc xuất trước DS-15): vẫn kiểm view của node và mọi view/cột của `access.yaml`. Gói cảnh báo không kiểm được cột của view đó và mở mọi cột của view.

## Consequences

- View mà rule cấp nhưng không node nào đọc giờ cũng nằm trong `allowed_views` của gói, nên quy tắc khớp với nguồn và `scope_on` không đóng cả Hệ CSDL vì "quy tắc lỗi thời".
- Đổi schema DB production sau khi gói đã chạy thì chỉ bị phát hiện ở lần khởi động sau; trong lúc chạy, lượt đọc trên cột đã mất sẽ lỗi như một truy vấn hỏng.
