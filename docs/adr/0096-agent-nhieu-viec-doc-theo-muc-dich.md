---
status: accepted
---

# Agent nhiều việc: rẽ nhánh trước, mỗi nhánh đọc theo mục đích, bước điền thấy ngày và dữ liệu đã đọc

## Bối cảnh

Ngày 13/11/2026, agent "trợ lý công nợ đầy đủ" (TOPIC.md: trả lời câu hỏi, gửi thư nhắc nợ, ghi chú hẹn trả) qua
được validator nhưng chạy sai 2/3 câu trong sandbox với LLM thật:

- ghi chú lưu `customer_id: "ABC"` (đúng là `KH-0001`) và `promised_date: 2023-11-20` (sai năm);
- thư nhắc nợ không có số hoá đơn, số tiền, số ngày quá hạn;
- body gửi đi khác bản người duyệt đọc trên thẻ.

Nguyên nhân không phải prompt của người tạo, cũng không phải thiếu loại node:

1. **Runtime.** Bước điền câu đọc/câu ghi không biết hôm nay, và bước điền câu ghi không thấy dòng vừa đọc. Node gửi mail
   cho model điền lại `body` thay vì lấy bản nháp. `collect` còn giữ `subject`/`body` do model bịa ra lúc hỏi địa chỉ.
   Bộ rẽ nhánh chỉ thấy tin nhắn mới nhất ("ketoan@abc.example").
2. **Meta-agent.** Prompt composer bắt "MỘT `data_query` cho mỗi nguồn", nên graph có một bước đọc chung trước khi rẽ
   nhánh, không biết đọc cho việc gì. Hàm gắn nguồn gắn node đọc thứ n vào nguồn thứ n, nên node đọc thứ hai không
   được gắn. Node ghi được cấp mọi bảng đã tick ghi, kể cả `fin_invoice`.

## Quyết định

1. **`DataQueryConfig.purpose`** (prose, không bắt buộc, marker mới `OPTIONAL_PROSE`): một câu nghiệp vụ nói bước đọc
   lấy gì, để làm gì. Bước điền câu truy vấn đọc nó.
2. **Composer nguyên tắc 6:** bài toán từ hai việc trở lên thì graph bắt đầu bằng chuỗi `if_else` hỏi thẳng ý định
   người dùng (kèm câu mẫu), không có `agent_llm` phân loại đứng trước. Mỗi nhánh có `data_query` riêng với `purpose`.
   Luật "một `data_query` mỗi nguồn" được thay bằng ba luật kiểm hình dạng:
   - không có hai node đọc trên cùng một đường;
   - không có node đọc đứng trước hai việc có tác dụng phụ;
   - graph có từ hai node đọc thì node nào cũng phải có `purpose`.
   Thêm luật: không có `agent_llm` đầu graph chỉ để phân loại cho `if_else`.
3. **Gắn nguồn:** spec có một Hệ CSDL thì mọi node đọc/ghi gắn vào nó. View và bảng chỉ gồm những gì quy tắc truy cập
   của ít nhất một chức vụ cho phép đọc/ghi.
4. **Bước điền** (truy vấn, thao tác ghi, tham số action) có thêm ngày hôm nay. Truy vấn và thao tác ghi còn có thêm
   dữ liệu đã đọc trong lượt và `purpose`.
5. **Tham số `agent_writes` lấy nguyên văn từ bản nháp** mà thẻ duyệt hiển thị (tách theo nhãn "Tiêu đề:" / "Nội
   dung:"), và thắng cả giá trị của `collect` lẫn của bước điền. `collect` không lưu tham số thuộc `agent_writes`.
6. **Bộ rẽ nhánh:** lượt ngắn chỉ bổ sung thông tin thì thấy thêm yêu cầu ở lượt trước. Điều kiện về ý định được xét
   theo điều người dùng hiển nhiên muốn, không đòi đúng từng chữ.
7. **Quy ước cho mọi `agent_llm`:**
   - bản soạn không có ngoặc vuông, không tự thêm hậu quả hay điều khoản;
   - bản đưa duyệt không nói việc đã xong;
   - kết quả mô phỏng thì nói rõ là chạy thử;
   - thư dùng khuôn "Tiêu đề:" / "Nội dung:".

## Hệ quả

- Dựng lại từ đúng spec cũ (agent kiểm thử #4, #5) ra graph ba nhánh ngay lần đầu, không phải quay về mẫu. Ba câu của
  TOPIC.md chạy đúng: 3 khách quá hạn; thư có HD-0001, 15.000.000 đ, 60 ngày, body gửi trùng thẻ duyệt; ghi chú
  `KH-0001`, `2026-11-20`, có nội dung.
- Agent dựng trước thay đổi này (như #3) vẫn chạy theo graph cũ. Muốn hưởng phần meta-agent thì phải dựng lại. Các sửa
  ở runtime có hiệu lực ngay cho mọi agent.
- Tách tiêu đề/nội dung theo nhãn là quy ước giữa prompt và code. Bản nháp không nhãn với hai tham số thì vẫn để bước
  điền làm, như trước.
