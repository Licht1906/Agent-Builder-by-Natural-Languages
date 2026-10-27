---
status: accepted
---

# Nháp quy tắc truy cập từ mô tả bằng lời

DS-12: người tạo nói "nhân viên chỉ xem khách của mình, trưởng nhóm xem cả phòng", và meta-agent dịch câu đó thành nháp `rules` của DS-03. ADR-0059 §6 đã chốt rằng LLM không bao giờ quyết định phạm vi. Nháp là chỗ ranh giới đó dễ bị xoá mờ nhất, nên ADR này ghi rõ nó dừng ở đâu.

## Quyết định

1. **Nháp, không bao giờ là quyết định.** `POST /database-sources/{id}/access-rules/draft` trả `{rules, dropped, problems}` và không ghi gì vào DB. Form DS-03 nạp nháp, hiện băng "Nháp — chưa lưu", và chỉ nút Lưu có sẵn mới áp dụng quy tắc. Nút đó đi qua đúng các bước kiểm của `save_access_rules`.
2. **`identity` phải có trước, và phải khớp Hệ CSDL.** Không khớp thì trả 422 trước khi gọi model. Phạm vi xoay quanh người hỏi, nên chưa biết người hỏi là ai thì không có gì để đề xuất.
3. **Model được cho xem Hệ CSDL như nó thật sự là:**
   - các chức vụ đọc từ cột chức vụ của view nhân viên (một lần đọc, tính vào giới hạn `/test` như `/distinct-values`);
   - các thuộc tính của `identity`;
   - các view đã tick cùng cột của chúng. View nhân viên không có trong danh sách này, vì nó để tra người hỏi, không phải để cấp quyền xem.
4. **Thứ không có thì bị loại và được báo; thứ bị mất điều kiện thì bị bỏ, không bao giờ nới ra.**
   - Bị loại: chức vụ không có trong view nhân viên (so sau khi chuẩn hoá NFC và bỏ phân biệt hoa thường), view chưa tick, cột không có, thuộc tính `identity` không đọc.
   - Một `where` mất hết điều kiện thì bỏ cả quyền trên view đó, **không đổi thành `all`**. Rule không còn view nào thì bỏ, tức chức vụ đó bị cấm.
   - Cái sai an toàn duy nhất là thấy ít hơn.
5. **Hỏi lại model chỉ khi câu trả lời không phải JSON.** Một chức vụ không có thật thì hỏi lại cũng không làm nó tồn tại, nên trường hợp đó bị loại chứ không hỏi lại. Sau ba lần không đọc được thì trả 502.

## Consequences

- Chức vụ đang có trên form mà nháp không có rule vẫn được giữ, ở trạng thái cấm. Nạp nháp không làm mất cột nào người tạo đang thấy.
- Mô tả do người tạo gõ hoặc dán vào form; chưa tự lấy từ mô tả agent lúc phỏng vấn.
