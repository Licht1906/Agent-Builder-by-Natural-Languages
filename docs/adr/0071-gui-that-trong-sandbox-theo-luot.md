---
status: accepted
---

# "Gửi thật" trong sandbox: chỉ lời gọi đã duyệt đúng payload, chỉ một lượt

ADR-0059 (Hệ quả) cho phép một lệch có chủ đích với V2: sandbox luôn mô phỏng hành động rủi ro (`SandboxBehavior.MOCK`), nhưng để demo gửi Gmail thật, sandbox có một công tắc gửi thật theo lượt chạy. DS-09 hiện thực công tắc đó. ADR này ghi lại công tắc được phép mở tới đâu.

## Quyết định

1. **Thứ được chạy thật là lời gọi đã duyệt, không phải mọi thứ đang bị mô phỏng.** Một lời gọi chỉ chạy thật khi đủ ba điều kiện: lượt chạy là sandbox và bật `send_for_real`; hành động cần người duyệt (`requires_human_approval` hoặc `require_approval: always`); và đã qua kiểm `action_hash` của ADR-0070. Hành động mô phỏng mà không ai phải duyệt (ví dụ `gmail.create_draft`) vẫn là mô phỏng, kể cả khi công tắc bật.
2. **Công tắc thuộc về lượt chạy.** Trường `send_for_real` của request lượt thử, mặc định `false`, không lưu vào thread. Lượt chạy dừng ở bước duyệt mang công tắc trên hàng `run_approvals.live_sends`, vì chặng gửi là một request khác. Màn hình tắt công tắc ngay khi lượt chạy bắt đầu.
3. **Gửi bằng kết nối của chính người tạo.** Không có đường riêng: công cụ Gmail vốn mượn tài khoản của chủ agent, và trên cửa sandbox thì chủ agent chính là người đang chạy thử.
4. **Vẫn là chạy thử, có gắn cờ.** Dòng transcript có `simulated=false` cùng `live_in_sandbox=true`. `agent_runs.live_sends` đánh dấu lượt đã gửi thật, còn `trigger` vẫn là `chat_test`, nên không widget production nào của PM tính lượt này. Thẻ duyệt nói rõ "duyệt là gửi thật".
5. **Eval không bao giờ gửi thật.** Runtime chỉ nhận công tắc khi `sandbox=True` và không có bảng quyết định soạn sẵn.

## Considered Options

- **Bật là mọi hành động mô phỏng đều chạy thật** — loại: biến một công tắc demo thành cửa gửi không qua duyệt, với các hành động mức `medium` không có ai đứng trước.
- **Công tắc gắn với thread hoặc version** — loại: ticket yêu cầu "lượt sau mặc định tắt lại", và một công tắc được nhớ chính là thứ khiến người ta gửi nhầm.
- **Một `RunTrigger` thứ ba** cho lượt gửi thật — loại: đây vẫn là người tạo đang thử agent của mình, nên một cờ là đủ để PM lọc.

## Consequences

- Thẻ duyệt của lượt bật Gửi thật có cảnh báo riêng. Một lượt thử đã dừng từ trước bản nâng cấp này có `live_sends = false` và vẫn chạy mô phỏng.
- Gửi thật vẫn bị mọi ràng buộc của ADR-0070: payload khác thì bị từ chối trước khi gửi, và một lần duyệt chỉ cho đúng một lần gọi.
