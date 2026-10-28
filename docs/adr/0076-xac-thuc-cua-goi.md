---
status: accepted
---

# Xác thực người hỏi trong gói xuất ra

ADR-0059 §10 để công ty tự chọn cách nhận diện người hỏi (`auth.mode` trong `runtime.yaml`). Phạm vi dữ liệu đến từ danh tính đã xác thực chứ không đến từ hội thoại, nên đây là cửa duy nhất mà một lỗi sẽ thành "ai cũng xem được mọi thứ". DS-14 hiện thực bốn chế độ.

## Quyết định

1. **Một hợp đồng cho mọi chế độ: một giá trị, hoặc 401.**
   - `Authenticator(request) -> str | None`. Giá trị đó so với `identity.match_by` của `access.yaml` (DS-03).
   - Không có kết quả thứ ba: không có người hỏi mặc định, không có "ẩn danh".
   - Token hợp lệ nhưng nêu một người không có trong view nhân viên vẫn trả 200, và mọi lượt đọc đều bị từ chối ("không có quyền"); quyết định đó thuộc về bộ máy phạm vi, không thuộc tầng xác thực.
2. **`jwt`.**
   - Khoá kiểm là một secret bắt buộc của gói: khoá chung cho HS*, khoá công khai PEM cho RS*/ES*.
   - `algorithms` là danh sách cố định, nên `alg: none` hay đổi thuật toán đều bị từ chối.
   - `exp` bắt buộc; `iss`/`aud` được kiểm khi có khai.
3. **`oidc`.**
   - Khoá ký lấy qua `/.well-known/openid-configuration`, hoặc từ `jwks_url` nếu có khai, **ngay lúc khởi động**: issuer không với tới được, hoặc tự khai một issuer khác, thì gói từ chối khởi động thay vì từ chối từng request.
   - Gặp `kid` chưa biết thì tải lại JWKS một lần; đó là cách theo kịp xoay khoá mà không cần khởi động lại.
   - `exp`, `iss`, `aud` đều bắt buộc.
4. **`custom`.**
   - File của công ty (mặc định `auth_adapter.py` cạnh `runtime.yaml`) phải có hàm `resolve_principal(request)`, đồng bộ hay async đều được; thiếu hàm thì gói từ chối khởi động.
   - Adapter ném lỗi, hoặc trả về thứ không phải chuỗi khác rỗng, thì là 401 và được ghi log. Adapter hỏng không biến thành 500 hay thành "cho qua".
5. **`trusted_header`** giữ nguyên như DS-13: chỉ đúng header đã khai mới được tính.

## Consequences

- `trusted_header` chỉ an toàn khi gói chỉ nhận request từ gateway; việc này do hạ tầng của công ty bảo đảm, gói không kiểm được.
- Gói không tự làm luồng đăng nhập OIDC (redirect, cookie). Nó chỉ kiểm bearer token mà UI hoặc gateway gửi kèm. UI chat của gói (DS-17) sẽ cần lấy token từ IdP.
