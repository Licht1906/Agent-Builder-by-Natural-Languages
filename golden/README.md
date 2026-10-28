# `golden/*.jsonl` — seed, không sửa tay ở đây

ADR-0054 §6 (EVAL-7 / #157): các file trong thư mục này là **seed** để CI và
bản air-gap dựng lại database — không còn là nơi người tạo sửa bộ đề.

- `uc1_ar_v3.jsonl` — golden set của agent UC-1. Pipeline UC-1 và script nạp
  riêng của nó đã gỡ (DS-21); file ở lại như một bộ đề thật để kiểm importer
  chung (`tests/test_api/test_eval_suite_import.py`) và làm nguồn khi dựng bộ
  đề cho agent công nợ trên V2.
- `meta_agent_v1.jsonl` — cùng schema nạp được bằng importer chung
  (`POST /agents/{agent_id}/eval-suite/import`), chưa có script nạp riêng.
- `meta_agent_data_source_v1.jsonl` — DS-11: các case "nhu cầu đọc dữ liệu
  nghiệp vụ → `data_query`" (mô tả demo; trợ lý tồn kho chỉ đọc; trợ lý thuần
  tài liệu không được có `data_query`). Cùng schema với `meta_agent_v1.jsonl`
  nhưng để file riêng: ngưỡng của `src.evals.meta_agent_gate.THRESHOLDS` được
  đăng ký trước cho đúng tập case của file kia, và thêm case vào đó là dời
  ngưỡng — một quyết định cần diff và người duyệt riêng. Chấm bằng
  `load_cases(Path("golden/meta_agent_data_source_v1.jsonl"))` + `grade`.

Sửa file ở đây chỉ còn ý nghĩa với **seed** — dữ liệu một DB trắng bắt đầu
với — không ảnh hưởng gì tới một DB đã nạp rồi.
