---
status: accepted
---

# Kênh mail của gói xuất ra

Trên nền tảng, `gmail.send_email` gửi bằng tài khoản Gmail của người tạo, qua OAuth token của họ. Trong mạng của công ty, cả hai đều không có. DS-16 cho gói một kênh mail do công ty chọn trong `runtime.yaml` (`mail.mode`), và bundle không phải đổi gì.

## Quyết định

1. **Kênh mail thay đúng một thứ: phần thực thi của kết nối `gmail`.**
   - Runtime V2 có thêm `WorkflowRunContext.invokers` (`{tool_id: implementation}`), truyền vào qua `stream_workflow_reply(invokers=...)`. Gói ánh xạ `gmail` sang kênh mail.
   - Action, mức rủi ro, `human_approval` đứng trước và ràng buộc payload bằng `action_hash` (DS-08) vẫn là của registry và runtime. Một lần gửi chưa duyệt hoặc bị sửa sau khi duyệt bị từ chối trước khi tới kênh, giống hệt trên nền tảng.
   - Đổi `mail.mode` không cần sửa bundle, vì bundle vẫn chỉ ghi `gmail.send_email`.
2. **Ba chế độ.**
   - `smtp`: relay của công ty, mặc định STARTTLS. Địa chỉ `from` lấy từ cấu hình, model không chọn được. Có `password_secret` thì bắt buộc bật `starttls`, để mật khẩu không bao giờ đi dạng rõ. Thân thư gửi dạng quoted-printable, nên relay không hỗ trợ 8BITMIME cũng không làm hỏng tiếng Việt.
   - `gmail_oauth`: một tài khoản Gmail của công ty. Gói đổi refresh token lấy access token (client secret và refresh token là secret của gói), giữ access token tới gần lúc hết hạn, và token bị từ chối thì lần gửi sau lấy lại.
   - `custom`: file `mail_adapter.py` của công ty, có đúng một hàm `send_email(to, subject, body) -> str`. Thiếu hàm thì gói từ chối khởi động. Hàm báo lỗi hoặc không trả về chuỗi thì coi như thư chưa gửi; lượt chạy vẫn tiếp tục và báo lại điều đó.
   - Cả ba chế độ dựng thư bằng cùng `email_message` của nền tảng, nên người nhận đọc thấy cùng một nội dung.
3. **Bundle có thể gửi mail mà chưa có `mail` thì gói từ chối khởi động.** "Có thể gửi" nghĩa là `gmail.send_email` nằm trong `mcp_action_names` của workflow. Secret của kênh mail cũng là secret bắt buộc, được báo thiếu cùng lúc với các secret khác.
4. **Các action `gmail` khác (đọc hộp thư) không chạy trong gói.** Gói không có hộp thư nào để đọc. Khi khởi động, gói cảnh báo kèm tên action; nếu workflow gọi tới thì node báo lỗi kèm tên action.

## Consequences

- Kênh mail của gói không ghi lại thư đã gửi ở đâu khác ngoài audit chain (DS-10) và câu trả lời của relay.
- Thêm một chế độ mới, ví dụ Microsoft Graph, chỉ cần thêm một nhánh trong `src.standalone.mail`. Runtime và bundle không đổi.
