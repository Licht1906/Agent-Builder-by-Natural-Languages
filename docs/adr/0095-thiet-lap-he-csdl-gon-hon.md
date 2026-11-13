# ADR-0095: Thiết lập Hệ CSDL gọn hơn (không phân quyền, tự đoán, mô tả cho agent)

- **Trạng thái:** accepted
- **Ngày:** 2026-11-11
- **Sửa:** ADR-0059 §6, theo đó mọi Hệ CSDL đều phải có `identity` và `rules`.

## Bối cảnh

Muốn cắm một DB vào, người tạo phải tự làm sáu bước: nhập kết nối, tick view, tick bảng ghi, chọn view nhân viên, khai thuộc tính, rồi điền ma trận quyền. Với một DB đơn giản, không có gì cần tách riêng theo người, thì thậm chí không làm được: thiếu bảng nhân viên là bị cấm hết. So sánh với Julius cho thấy hai điều nên học. Thứ nhất, mặc định là mở (*"with nothing selected, ALL tables and columns are accessible"*). Thứ hai, người tạo được mô tả kết nối bằng lời (*Custom Instructions*).

## Quyết định

1. **`open: true` là một dạng quy tắc truy cập riêng.**
   - Dạng này không có `identity`, không có `rules`, không tra người hỏi. Mọi người dùng agent đọc mọi view đã tick, và thêm/sửa/xoá trên mọi bảng đã tick cho phép ghi, như nhau.
   - Phải chọn tường minh ("Không phân quyền") rồi lưu. **Không có quy tắc nào vẫn nghĩa là cấm hết**, như cũ.
   - Mọi thao tác ghi vẫn qua bước duyệt, có trần số dòng và audit (ADR-0094).
   - `access.yaml` của gói xuất ra chỉ có một dòng `open: true`.
   - Eval trên DB mở không cần "chạy với tư cách".
2. **Lần kiểm tra kết nối thành công đầu tiên tự tick mọi view.** Các lần sau không đụng tới lựa chọn của người tạo. Bảng cho phép ghi thì **không bao giờ** được tự tick.
3. **Form quy tắc tự đoán view nhân viên và thuộc tính từ tên cột**:
   - view nhân viên là view có cột kiểu `email` và cột kiểu chức vụ (`position`, `chuc_vu`, `role`…);
   - các thuộc tính quen thuộc được điền sẵn: `employee_code`, `department` và các biến thể tên.

   Đây chỉ là gợi ý điền sẵn trên form; người tạo thấy và sửa được trước khi lưu.
4. **Mô tả Hệ CSDL cho agent** (`agent_notes`): người tạo tự viết bằng lời, ví dụ view nào chứa gì, các bảng liên kết ra sao, quy ước nghiệp vụ. Mô tả được đặt trước hội thoại trong prompt của model soạn truy vấn và soạn thao tác ghi, **chỉ làm ngữ cảnh, không bao giờ là quyền**: phạm vi dữ liệu vẫn do reader quyết định. Mô tả đi theo vào gói xuất ra (`data_sources[].agent_notes`) và được đóng băng cùng cassette của eval.

## Hệ quả

- Với DB đơn giản, việc thiết lập gồm: nhập kết nối → kiểm tra → chọn "Không phân quyền" → lưu.
- Với DB cần phân quyền: nhập kết nối → kiểm tra → mở quy tắc (đã điền sẵn view nhân viên và thuộc tính) → mô tả bằng lời → **Đề xuất quy tắc** → rà lại → lưu.
- Chưa có connector ERP dùng quyền sẵn có của người dùng; việc này để sau, cần phỏng vấn kỹ trước khi làm.
