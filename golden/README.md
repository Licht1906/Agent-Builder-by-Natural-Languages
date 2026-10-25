# `golden/*.jsonl` — seed, không sửa tay ở đây

ADR-0054 §6 (EVAL-7 / #157): các file trong thư mục này là **seed** để CI và
bản air-gap dựng lại database — không còn là nơi người tạo sửa bộ đề.

- `uc1_ar_v3.jsonl` — nạp một lần vào suite của agent UC-1 bằng
  `python scripts/import_golden_set.py --agent <agent id> --file golden/uc1_ar_v3.jsonl`
  (idempotent theo `id` từng case). Sau khi nạp, `uc1_golden_set_runner` đọc
  case từ suite (`eval_suites`/`eval_cases`), không đọc file này nữa. Muốn
  thêm/sửa một case: sửa qua UI/API suite của agent (importer chung, EVAL-2),
  không sửa file này bằng PR.
- `meta_agent_v1.jsonl` — cùng schema nạp được bằng importer chung
  (`POST /agents/{agent_id}/eval-suite/import`), chưa có script nạp riêng.

Sửa file ở đây chỉ còn ý nghĩa với **seed** — dữ liệu một DB trắng bắt đầu
với — không ảnh hưởng gì tới một DB đã nạp rồi.
