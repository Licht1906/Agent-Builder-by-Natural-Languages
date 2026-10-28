---
status: accepted
---

# Trang chat trong gói xuất ra

Nhân viên công ty dùng agent qua trình duyệt (US-17): đăng nhập bằng tài khoản công ty, hỏi, đọc câu trả lời kèm nguồn, rồi duyệt mail agent soạn. DS-17 thêm một trang chat tĩnh vào gói, do chính runtime độc lập phục vụ.

## Quyết định

1. **Một bản build thứ hai từ cùng mã nguồn, không phải một app mới.**
   - `npm run build:package` (`vite.package.config.js`, entry `frontend/package/index.html`) build ra `frontend/dist-package/`. `Dockerfile.runtime` build bản này trong một stage Node và chép vào `/app/ui` (`AGENT_UI_DIR`).
   - Mỗi lượt chat được vẽ bằng `AgentTurn` của Chat V2 và dựng bởi `lib/turns.js`, từ đúng các tên event của runtime. Nhờ vậy thẻ duyệt hiện payload nguyên văn, đúng thứ runtime đã hash (DS-08), và nguồn trích dẫn của `data_query` hiện giống trên nền tảng.
   - Trang không có lịch sử hội thoại, nút đánh giá hay route nào của nền tảng.
2. **Runtime phục vụ trang ở cùng origin với API.**
   - `/` trả về trang, `/assets` chứa file tĩnh, `/ui-config` cho biết tên agent và cách đăng nhập. Trang gọi `/chat` và `/approvals` cùng origin, nên không cần CORS và không có URL nào bị biên dịch cứng vào bản build.
   - `/ui-config` chỉ có tên và URL: issuer, `client_id`, scope. Không có secret, không có khoá JWT, và không có tên header mà gateway đặt.
   - Chưa build trang thì `/` trả 404 kèm lời giải thích; API vẫn chạy.
3. **Đăng nhập theo `auth.mode`, trang chỉ mang credential tới runtime; runtime mới là nơi xác thực (DS-14).**
   - `oidc`: Authorization Code + PKCE ngay trong trình duyệt, với một public client. Trang không có client secret, gói cũng không. Trang gửi ID token, nên `auth.oidc.audience` đặt bằng `client_id` (thêm trường `client_id` và `scope` vào `auth.oidc`). Code trả về mà không kèm đúng `state` của tab thì bị từ chối. Thiếu `client_id` thì gói cảnh báo lúc khởi động và trang nói rõ lý do.
   - `jwt`: dán token, hoặc nhận token qua URL fragment `#token=`; token được xoá khỏi thanh địa chỉ ngay sau đó.
   - `trusted_header` / `custom`: trang không gửi gì của riêng nó; gateway hoặc adapter của công ty lo việc nhận diện.
   - Token chỉ nằm trong `sessionStorage`, đóng tab là mất. Token hết hạn bị bỏ trước khi gửi. Runtime trả 401 thì trang đưa người dùng về màn đăng nhập.
4. **Header của trang.**
   - `Referrer-Policy: no-referrer`: code OIDC trên URL không lọt qua header Referer.
   - `X-Frame-Options: DENY`.
   - CSP chỉ cho phép script, style và kết nối tới chính origin của gói, cộng thêm origin của issuer. Font web của nền tảng (Google Fonts) bị chặn có chủ đích: mạng công ty có thể không ra được Internet, và trang không nên gọi ra ngoài.

## Consequences

- Trang chat của gói dùng font hệ thống thay vì font của nền tảng.
- Một sửa đổi ở `AgentTurn`, `ApprovalPayload` hay `SourcesPanel` đi vào cả nền tảng lẫn gói. Đó là mục đích, nhưng cũng nghĩa là những component này không được gọi API riêng của nền tảng. (`TurnThumbs` chỉ hiện khi lượt có `trace_id`; trang của gói không gán `trace_id`.)
- Bản demo dùng `deploy/package/docker-compose.demo.yml`: issuer OIDC giả lập và MailHog.
