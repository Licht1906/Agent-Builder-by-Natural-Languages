---
status: accepted
---

# Duyệt đúng payload: `human_approval` ràng buộc lời gọi, không ràng buộc node

ADR-0059 §7 bỏ giới hạn người nhận, nên bước `human_approval` là chốt chặn duy nhất giữa model và thế giới bên ngoài. Trước DS-08, "đồng ý" là đồng ý với một **node**: người duyệt chỉ đọc `message_template`, còn tham số `send_email` (người nhận, tiêu đề, nội dung) do model điền **sau** khi đã duyệt, ở chính node action. Người duyệt không thấy thứ sẽ gửi, và model đổi được nó sau cái gật đầu.

## Quyết định

1. **Payload được dựng trước khi dừng.** Node `human_approval` tìm các node `mcp` mà cổng `approved` dẫn tới (dừng ở `human_approval` kế tiếp) và có hành động cần người duyệt — `requires_human_approval` hoặc `require_approval: always`, đúng hai cách đọc của validator. Với mỗi node đó, nó dựng lời gọi bằng chính đường điền tham số của node action (`_action_and_args`: giá trị `collect` đã kiểm, cùng lần gọi model, cùng luật `verbatim`). Thẻ duyệt hiển thị nguyên văn `{tool, args}`.
2. **`run_approvals.actions` lưu `{node_id, tool, action, args, hash}`**, ghi một lần cùng hàng. `hash` là SHA-256 của JSON chuẩn hoá `{"tool", "args"}` (khoá sắp xếp, không khoảng trắng, giữ nguyên ký tự không phải ASCII). Không trim hay đổi hoa thường: thứ được hash phải đúng thứ người duyệt đọc.
3. **Khi chạy tiếp, payload được đọc lại từ hàng, không dựng lại.** `interrupt()` chạy lại node từ đầu. Nếu dựng lần hai, model có thể viết thân thư khác với bản vừa được duyệt.
4. **Node action tính lại hash ngay trước khi gọi** và so hai chỗ: với giấy phép trong state (`approved_actions`), và — khi có người duyệt thật — với hàng `run_approvals` đã `approved` trong DB, để checkpoint không tự nhận một cái gật đầu không ai đưa. Khác → từ chối, không gọi tool, lý do ghi vào transcript, kèm tên tham số đã đổi.
5. **Không có gì được nói thêm kể từ lúc duyệt → gửi đúng bản đã duyệt, không gọi model lần nữa.** Có node nào viết thêm vào hội thoại sau khi duyệt (ví dụ một `agent_llm` soạn lại) → node action dựng lại như bình thường, và hash quyết định. Vì thế "soạn lại mà giữ nguyên thư" vẫn gửi được, còn "soạn lại thành thư khác" thì bị từ chối.
6. **Một lần duyệt cho đúng một lần gọi.** Giấy phép bị dùng hết khi node action chạy (gửi hay bị từ chối đều tính), và bị rút khi cổng đó ra quyết định mới. Một cổng đứng trước `While` không cấp phép cho mọi vòng lặp.
7. **Kiểm cả trong sandbox, trước bản mô phỏng.** Lượt chạy thử từ chối đúng thứ production từ chối (ADR-0024 §7). Eval có quyết định soạn sẵn (ADR-0028 §1) cũng dựng payload ở cổng và bị ràng buộc như vậy, chỉ là không có hàng DB để so.

## Considered Options

- **Node action luôn điền lại rồi so hash** (đọc nguyên văn ticket) — loại: model điền lại cùng một hội thoại vẫn có thể diễn đạt thân thư khác, nên một lần duyệt hợp lệ sẽ thường xuyên bị từ chối oan. Mục 5 giữ đúng ý ticket ở chỗ có ý nghĩa: hội thoại đã đổi sau khi duyệt.
- **Node action luôn gửi bản đã duyệt, bỏ qua mọi thứ sau đó** — loại: một node soạn lại sau khi duyệt sẽ âm thầm bị lờ đi, và tác giả workflow không biết bản họ soạn lại chưa bao giờ được gửi.
- **Chỉ lưu hash trong state** — loại: state là checkpoint; mục 4 đòi một nguồn mà graph không tự ghi được.

## Consequences

- Lần gọi model điền tham số dời từ node action lên node duyệt, nên tổng số lần gọi model và thứ tự của chúng không đổi; kịch bản model trong test chuyển lần điền sang chặng trước khi dừng.
- Thẻ duyệt có thể hiện một lời gọi **thiếu** người nhận (khi người dùng chưa từng gõ địa chỉ, luật `verbatim` bỏ giá trị model đoán). Đó là sự thật về thứ sẽ được gửi, và người duyệt nên từ chối.
- Hàng `run_approvals` cũ (trước DS-08) có `actions = []`; một lần chạy dừng từ trước khi nâng cấp mà được duyệt sau đó sẽ không có giấy phép, và node action sẽ từ chối — an toàn, đổi lại phải chạy lại.
