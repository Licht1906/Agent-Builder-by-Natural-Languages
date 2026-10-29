---
status: accepted
---

# Thư viện template eval và việc dựng bộ đề ≥100 case

ADR-0069 §1: mỗi agent có một bộ đề ≥100 case live. Phần hành vi và an toàn lấy từ một thư viện template dùng
chung, tự khớp với năng lực của agent, slot điền bằng code. Phần còn thiếu là case nghiệp vụ do generator sinh.
EV-03 dựng cơ chế này. Nội dung thư viện v1 (~150 template) là việc của EV-06.

## Quyết định

1. **Thư viện là file trong repo: `eval/library/*.jsonl`, mỗi dòng một template** (`src/evals/library.py`).
   - Các khoá: `id`, `tags` (8 tag của ADR-0069), `applies_when` (danh sách năng lực), `input` và `criterion` có
     slot, `expected_check` (bắt buộc), `category`, `run_as` (`in_scope` / `other_scope` / `nobody`).
   - Validator từ chối template không chấm được:
     - thiếu luật chấm, hoặc luật không parse được sau khi điền slot mẫu;
     - slot, tag hay năng lực không tồn tại;
     - dùng slot mà `applies_when` không bảo đảm điền được;
     - `id` trùng.
   - `python -m src.evals.library` kiểm toàn bộ thư viện, và một test chạy lại đúng việc đó.
2. **Năng lực đọc từ graph và spec.**
   - Từ graph và spec: hành động có tác dụng phụ, node duyệt, `collect`, `knowledge_search`, `web_search`, spec có
     điều cấm, có đọc Hệ CSDL.
   - Riêng `multi_scope` đọc từ dữ liệu: tìm hai người trong view nhân viên đọc cùng một view, bị lọc theo cùng một
     cột nhưng khác giá trị (hai nhân viên KD, hai sổ khách). Cách tìm: đọc thật một dòng với tư cách từng người,
     lúc dựng bộ đề, giống cách ghi cassette.
   - Hai người đó là `in_scope` và `other_scope`. Giá trị phạm vi của họ là slot `{in_value}` / `{other_value}`.
   - Template `cross_tenant` hỏi dữ liệu của phạm vi người kia, và chấm bằng `answers_from_query` với
     `expects_no_rows`. Đáp án do cassette của chính người hỏi quyết định (EV-02).
3. **Case điền xong không trỏ ngược về thư viện.**
   - Case lưu text và luật đã điền, `origin = library` (giá trị mới của `EvalCaseOrigin`), `extra.template_id`,
     `tags`.
   - Thư viện chỉ được kéo **một lần cho mỗi version**: dựng lại trên cùng version chỉ bù cho đủ, không kéo
     template mới.
   - Template đã điền rồi thì không điền lại theo `(template_id, run_as)`. Sửa một template không đổi case nào đã
     có trong suite.
4. **Bù cho đủ ≥100, theo thứ tự, chỉ khi còn thiếu** (`src/evals/suite_builder.py`,
   `POST …/eval-suite/build`, nút "Dựng bộ đề"):
   1. template khớp năng lực;
   2. với agent đọc Hệ CSDL, **case nghiệp vụ sinh bằng code từ dữ liệu**:
      - lần lượt từng người trong view nhân viên, từng view graph đọc;
      - các câu hỏi: đếm, tổng, dòng lớn nhất và nhỏ nhất, ngưỡng (trung vị), phân nhóm theo cột ít giá trị;
      - mỗi câu là `answers_from_query`, đáp án tính trên cassette của người hỏi, ghi luôn vào `expected`;
      - case có đáp án rỗng, toàn số 0 hoặc đọc lỗi bị loại ngay khi sinh (ADR-0069 §3);
   3. nếu vẫn thiếu, `generate_workflow_cases` một lượt cho những tiêu chí chưa có case do model sinh.
   - "Live" đếm đúng như một lần chạy phân loại (`runnable_cases`): áp dụng được cho graph, có đủ dữ liệu đóng
     băng, và là `live` chứ không phải `orphan`.
5. **Luật chấm mới `cites_knowledge`** cho template `knowledge_grounding`: đạt khi lượt chạy có tra cứu tài liệu,
   câu trả lời có ít nhất một trích dẫn, và mọi trích dẫn đều là đoạn tìm được ở lượt đó.
6. **`scripts/draft_eval_library.py`** sinh nháp template bằng `gpt-6-luna`, theo từng tag.
   - Chỉ giữ dòng qua validator và mang đúng tag. Ghi vào `eval/library/drafts/`, thư mục mà `load_library`
     không đọc. Dòng bị loại được in ra kèm lý do.
   - Người duyệt cắt và chuyển vào `eval/library/` qua PR (EV-06).

## Considered Options

- **Gọi LLM nhiều vòng để bù tới 100.** Loại cho agent đọc Hệ CSDL: case từ dữ liệu có đáp án tính bằng code,
  không tốn model và chấm được bằng rule. Agent không đọc dữ liệu vẫn chỉ có một lượt model. Nếu lượt đó không đủ
  100, báo cáo dựng sẽ cho thấy (`live_count < target`).
- **Lưu thời điểm kéo thư viện vào suite.** Loại: "đã có case thư viện nào được giới thiệu ở version này chưa" đã
  đủ trả lời câu hỏi đó, không cần thêm cột.

## Consequences

- Thư viện khởi đầu có 19 template, đủ cho các tiêu chí của EV-03. Tag `leak` chưa có template nào: chưa có luật
  chấm nào kiểm được "không lộ" mà không cần đáp án văn xuôi. EV-06 phải quyết luật cho tag này trước khi viết
  template.
- Câu hỏi dữ liệu dùng tên view và cột thô (`amount_due`, `v_ar_open`), và có câu không mang nghĩa nghiệp vụ, như
  "tổng `days_overdue`". Chúng vẫn chấm đúng, nhưng không phải câu người dùng thật sẽ hỏi. Muốn câu hỏi tự nhiên
  hơn thì cần mô tả cột (bình luận view) hoặc một lượt model viết lại câu hỏi mà vẫn giữ truy vấn.
- Dựng bộ đề đọc DB thật: một lượt dò phạm vi, một lượt nhìn từng view cho mỗi người hỏi, rồi ghi cassette. Phải
  dựng lúc DB của khách truy cập được.
- Downgrade migration không gỡ giá trị `LIBRARY` khỏi enum: Postgres không bỏ được một giá trị enum, và các hàng
  đang mang nó vẫn phải đọc được.
