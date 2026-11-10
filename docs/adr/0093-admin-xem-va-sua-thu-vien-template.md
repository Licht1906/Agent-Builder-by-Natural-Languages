---
status: accepted
---

# Admin xem và sửa thư viện template eval trên giao diện

ADR-0089 đặt thư viện template ở `eval/library/*.jsonl`, sửa bằng PR. ADR-0092 đổ vào đó 148 template. Nhưng chưa có
màn hình nào cho thấy thư viện: muốn biết "Dựng bộ đề" lấy câu hỏi gì thì phải mở file. Case đã dựng thì hiện trong tab
Đánh giá, nhưng chỉ xoá được, không sửa chữ được, dù `PATCH …/eval-cases/{id}` đã có từ #13.

## Quyết định

1. **Trang admin "Thư viện eval"** (`GET /admin/eval-library`): mọi template, file chứa nó, tag, năng lực cần có, luật
   chấm, và số case live trong các bộ đề được điền từ nó (`extra.template_id`). Lọc theo tag, tìm theo chữ.
2. **Thêm / sửa / xoá** (`POST`, `PUT …/{id}`, `DELETE …/{id}`, chỉ admin) ghi thẳng vào file JSONL
   (`src.evals.library_store`).
   - **File vẫn là thư viện.** Không có bản sao trong DB. Một lần sửa ghi lại đúng một file, các dòng khác giữ nguyên
     từng byte, ghi qua file tạm rồi `os.replace`. Kết quả là một `git diff` để review và commit, đúng tinh thần
     ADR-0069 §1.
   - **Không ghi gì không hợp lệ.** Template được kiểm như `load_library` kiểm (hình dạng, slot, luật chấm được sau khi
     điền) và kiểm trùng id trước khi chạm vào file.
   - **Không đổi id.** Suite builder dedup theo `(template_id, run_as)`, nên đổi id sẽ làm template vào bộ đề lần thứ
     hai. Muốn đổi thì thêm template mới rồi xoá cái cũ.
   - Tên file chỉ là `*.jsonl` trần trong thư viện, không có thư mục (không ra ngoài, không vào `drafts/`).
3. **docker-compose mount `./eval/library`** vào backend, để sửa trên UI rơi vào file của repo, không mất khi rebuild
   image.
4. **Tab Đánh giá có nút sửa case**: tiêu chí, câu hỏi, đáp án mong đợi, qua `PATCH` sẵn có (lịch sử sửa giữ người
   sửa, EV-05). `expected_check` vẫn không sửa tay được (lý do ở `src.api.eval_routes`).

## Consequences

- Sửa template không đổi case đã có trong bộ đề (ADR-0089): case giữ chữ lúc được điền. Thẻ template hiện số case đó để
  admin biết. Template mới vào bộ đề ở lần "Dựng bộ đề" sau.
- Khi chạy từ image không mount thư mục, sửa trên UI chỉ sống tới khi container bị tạo lại.
- Hai admin cùng sửa trong một process được khoá tuần tự. Nhiều worker thì không có khoá chung: hai lần lưu cùng lúc
  vào cùng một file có thể làm mất thay đổi của người lưu trước. Chấp nhận được với tần suất sửa thư viện.
