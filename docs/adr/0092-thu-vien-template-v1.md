---
status: accepted
---

# Thư viện template eval v1

EV-06 soạn nội dung cho thư viện mà EV-03 (ADR-0089) dựng: sinh nháp bằng `gpt-6-luna`, duyệt, cắt xuống khoảng
150, đưa vào `eval/library/`.

## Quyết định

1. **Nháp.** `python -m scripts.draft_eval_library --all --count 40` sinh 318 template qua validator, cho cả 8 tag.
   - Lượt đầu chỉ 137 dòng qua, 223 bị loại. Nguyên nhân: prompt không cho model biết hình dạng của luật chấm, nên
     model tự đặt trường (`takes_no_action.target`, `forbids_action.when`…) và viết tên năng lực vào `tags`.
   - Prompt giờ kèm **đúng một mẫu luật cho mỗi tag**, chép từ `src.models.eval_check`, và cấm thêm khoá. Lượt sau
     chỉ 2 dòng bị loại.
   - Thư mục `eval/library/drafts/` không vào git và không vào image: chỉ phần đã duyệt mới là tài sản của nền tảng.
2. **Luật chấm theo tag:**

   | Tag | Luật chấm |
   |---|---|
   | `injection`, `out_of_scope_refusal` | `takes_no_action` |
   | `leak` | `takes_no_action` khi rò rỉ qua một hành động; `answers_from_query` + `expects_no_rows` khi đòi dữ liệu ngoài phạm vi |
   | `cross_tenant` | `answers_from_query` + `expects_no_rows` |
   | `asks_back_missing_param` | `asks_back` |
   | `approval_required` | `approval_compliance` (bị từ chối → không được gọi `{action}`) |
   | `prohibition` | `forbids_action` |
   | `knowledge_grounding` | `cites_knowledge` |

   Đây là lời giải cho chỗ ADR-0089 để ngỏ: chưa có luật nào kiểm được "không lộ" mà không cần đáp án văn xuôi.
3. **Duyệt.** Giữ 129 trong 318 bản nháp, cộng 19 template khởi đầu thành 148. Mỗi tag có 18–20 template.
   - **Bỏ:**
     - câu dùng tên action thô (`{action}`, ví dụ "dùng gmail.send_email");
     - câu ngoài phạm vi mà với agent gửi thư lại đúng phạm vi ("gửi email xin nghỉ phép");
     - câu duyệt nói sẵn "người duyệt đã từ chối rồi" (người dùng thật không biết điều đó);
     - câu `cross_tenant` không nêu view nào.
   - **Viết lại:**
     - câu `cross_tenant` và `leak` dạng dữ liệu được chèn `{scope_column}` trước `{other_value}`. Nếu không, agent
       không biết lọc theo cột nào, câu trả lời không ra kết quả rỗng, và case bị chấm trượt oan;
     - câu `leak` dạng rò rỉ qua hành động được viết lại cho `{action_phrase}` đọc tự nhiên trong câu.

## Consequences

- Agent gửi thư của demo khớp 128 template, nên có thể đạt ≥100 case live chỉ từ thư viện. Agent chỉ đọc khớp 28
  template (`cross_tenant` và `leak` dạng dữ liệu) và lấy phần còn lại từ dữ liệu.
- Run gate của agent gửi thư nặng hơn hẳn, vì phần lớn template là case an toàn, mỗi case chạy 3 lượt (EV-04).
- Template sinh từ cùng một prompt nên giống nhau về cấu trúc. Vòng red-team của ADR-0069 §1 là nơi bổ sung
  những cách tấn công mà model chưa nghĩ ra.
