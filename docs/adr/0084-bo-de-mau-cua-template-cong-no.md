---
status: accepted
---

# Bộ đề mẫu của template công nợ

Golden set của UC-1 (`golden/uc1_ar_v3.jsonl`) có 23 câu hỏi về công nợ: tra cứu, từ chối, lộ dữ liệu, injection, xuyên phạm vi. Bộ đề này được chấm bằng runner riêng của UC-1, gắn với một `principal`. DS-21 đã gỡ cả runner lẫn pipeline, nhưng những gì bộ đề kiểm thì vẫn còn giá trị. DS-22 đưa nó vào eval V2.

## Quyết định

1. **Chuyển sang schema suite V2 (`golden/cong_no_v1.jsonl`), giữ đủ 23 câu.**
   - `principal` → `run_as`: mỗi case do một nhân viên demo hỏi (DS-05). Ba email nằm trong `DEMO_ASKERS` (`src/evals/sample_suites.py`), một chỗ duy nhất mà DS-23 seed theo.
   - Tra cứu, từ chối, lộ dữ liệu và xuyên phạm vi được judge chấm, theo một kỳ vọng nêu **tính chất** (chỉ khách của chính người hỏi, có trích dẫn view, không có mã số thuế), không nêu con số. Lý do: DB demo (DS-23) có số liệu khác UC-1, và một bộ đề chấm theo con số sẽ sai ngay khi dữ liệu đổi.
   - "Làm theo chỉ dẫn" và chỉ dẫn bị chèn vào câu hỏi hay dữ liệu được chấm bằng luật `takes_no_action`, đọc thẳng transcript. Trên `database_qa`, graph không có hành động nào để từ chối, nên các case này bị bỏ qua (ADR-0062 §3). Trên `database_outreach` chúng được tính.
   - `source_case` giữ id UC-1 gốc để truy ngược.
2. **Bộ đề đi theo template.** `SAMPLE_SUITES` gắn bộ đề với `database_qa` và `database_outreach`. `GET`/`POST /agents/{id}/eval-suite/sample` nạp nó qua đúng importer chung (cùng báo cáo từng dòng, cùng chống trùng). Tab Evaluation có nút "Nạp bộ đề mẫu", chỉ hiện khi template của agent có bộ đề mẫu.
3. **Khoá chống trùng của importer thêm `run_as`.** Khoá cũ `(input, criterion)` của ADR-0054 §2 có trước DS-05. Một câu hỏi do nhân viên KD và do trưởng nhóm hỏi là hai case khác nhau, và chính cặp đó tạo nên bài kiểm xuyên phạm vi. Các dòng không có `run_as` vẫn giữ danh tính như cũ.
4. **Bỏ `golden/uc1_ar_v3.jsonl`.** Test importer kiểm alias kiểu UC-1 (`expected_output`, `group`) giờ dùng ba dòng viết ngay trong test.

## Consequences

- Không còn module `uc1_*` nào trong `src/evals` hay `src/services` (runner và tracing của UC-1 đã gỡ ở DS-21).
- DS-23 phải seed đúng ba email trong `DEMO_ASKERS` vào view nhân viên của "ERP mẫu" (hai nhân viên KD cùng phòng và trưởng nhóm của họ), thì bộ đề mới chạy đúng trên agent demo.
