# Test case chuẩn: RAG với Voyage

## Thông tin chung

- **ID:** RAG-VYG-E2E-001
- **Mục tiêu:** xác nhận workflow `Knowledge Search` index tài liệu và truy hồi bằng Voyage `voyage-4`.
- **Phạm vi:** AI Solution Designer → Specification → Workflow `document_qa` → Upload tài liệu → Sandbox.
- **Tài liệu mẫu:** `artifacts/rag-voyage/refund-policy.md`
- **Tài khoản:** `creator@example.com` / `creator-dev-password`

## Điều kiện trước khi test

1. Backend trả `200` tại `http://localhost:8000/health`.
2. Frontend mở tại `http://localhost:5173`.
3. `.env` có `EMBEDDING_MODEL=voyage-4,text-embedding-3-small` và `VOYAGE_API_KEY`.
4. Đăng nhập bằng tài khoản creator.

## Nội dung nhập ở AI Solution Designer

Dán nguyên đoạn sau vào ô mô tả:

> Tôi cần một trợ lý CSKH cho nhân viên hỗ trợ khách hàng. Trợ lý đọc sổ tay chính sách hoàn tiền được tải lên, tìm đúng quy định liên quan đến câu hỏi của khách và trả lời bằng tiếng Việt. Người dùng là nhân viên CSKH. Trợ lý phải trích dẫn hoặc nêu rõ đoạn thông tin trong tài liệu làm căn cứ. Nếu tài liệu không đề cập thì phải nói rõ là không tìm thấy thông tin, không được tự suy đoán. Trợ lý chỉ trả lời về chính sách hoàn tiền và tuyệt đối không tự phê duyệt hoàn tiền hay hứa thời gian ngoài tài liệu.

Nếu hệ thống hỏi bổ sung, dùng các câu trả lời sau:

- **Người dùng:** Nhân viên chăm sóc khách hàng.
- **Dữ liệu:** File sổ tay chính sách hoàn tiền do creator tải lên.
- **Hành động:** Tìm đoạn liên quan, tổng hợp câu trả lời, nêu căn cứ tài liệu.
- **Kết quả:** Câu trả lời tiếng Việt có căn cứ, hoặc nói rõ không tìm thấy nếu tài liệu không có thông tin.
- **Làm đúng:** Trả lời đúng thời hạn hoàn tiền trong tài liệu và không bịa thêm điều kiện.
- **Không được làm:** Tự phê duyệt hoàn tiền, tự hứa hoàn tiền, hoặc suy đoán nội dung không có trong tài liệu.

## Các bước thực hiện

1. Bấm **Build the specification**, kiểm tra đủ 7 trường rồi bấm **Confirm**.
2. Chọn **Generate workflow**, dùng template `document_qa` nếu giao diện cho chọn.
3. Mở node **Knowledge Search**, bấm **Upload a document**, chọn file `artifacts/rag-voyage/refund-policy.md`.
4. Kiểm tra file đã xuất hiện trong `source_ids` của node.
5. Bấm **Confirm workflow**, sau đó mở **Sandbox**.
6. Chạy câu hỏi kiểm tra chính:

   > Tôi cần chờ bao lâu để nhận lại tiền sau khi yêu cầu hoàn tiền được chấp thuận?

7. Chạy câu hỏi kiểm tra ngoài phạm vi:

   > Chính sách có quy định phí đổi ngoại tệ không?

## Kết quả mong đợi

### Câu hỏi chính

- Có ít nhất một passage được truy hồi từ `refund-policy.md`.
- Câu trả lời nêu đúng **7 ngày làm việc**.
- Câu trả lời nói đây là thông tin từ tài liệu, không tự thêm cam kết khác.
- Log/ledger embedding có model `voyage-4`.

### Câu hỏi ngoài phạm vi

- Hệ thống nói tài liệu không đề cập hoặc không tìm thấy thông tin.
- Không được tự bịa mức phí, phần trăm hoặc điều kiện đổi ngoại tệ.

## Tiêu chí pass/fail

**PASS** khi cả hai câu hỏi đạt expected result và `embedding_usage` có bản ghi `voyage-4`.

**FAIL** nếu dùng OpenAI khi Voyage key hợp lệ, không truy hồi được file, trả sai thời hạn, hoặc bịa câu trả lời cho câu hỏi ngoài phạm vi.

## Kiểm tra kỹ thuật sau khi chạy

```powershell
docker exec agent-builder-by-natural-languages-db-1 psql -U postgres -d rag_test -tAc "select model, count(*), sum(tokens) from embedding_usage group by model order by model"
```

Kết quả mong đợi có dòng bắt đầu bằng `voyage-4`.

