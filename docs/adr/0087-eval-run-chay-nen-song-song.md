---
status: accepted
---

# Eval run chạy nền, song song có giới hạn

ADR-0069 §7 muốn bộ đề ≥ 100 case chạy cổng trong khoảng 5 phút. Trước EV-01, runner chạy từng case một
(`for case in cases: await …`), và `POST …/workflow-eval-runs` giữ request tới khi xong. Mỗi case là một lượt
chạy agent cộng một lượt judge, nên 100 case mất 25–40 phút, quá timeout HTTP rất xa.

## Quyết định

1. **Request trả ngay, run chạy nền.**
   - `POST …/workflow-eval-runs` làm mọi việc trả lời được tức thì: từ chối graph không chạy được hoặc bộ đề
     rỗng (409 như cũ), phân loại case `ungrounded`, đọc ngưỡng và judge một lần, rồi ghi một `EvalRun` ở
     trạng thái `running`, 0/N. Phản hồi là run đó.
   - Phần còn lại là một job `BackgroundTasks` (`execute_workflow_eval`), cùng cơ chế ingest tài liệu đang dùng.
   - `GET …/eval-runs/{id}` trả `done_count/total_count` khi đang chạy, kết quả khi `completed`, `error` khi `failed`.
2. **Song song có giới hạn.**
   - `asyncio.Semaphore(Settings.eval_concurrency)`, mặc định 8, cấu hình qua `EVAL_CONCURRENCY`.
   - `results` lưu theo thứ tự case trong bộ đề (`asyncio.gather`), không theo thứ tự xong. Cùng bộ đề chạy
     tuần tự hay song song cho cùng một bản ghi.
3. **Một case lỗi chỉ hỏng case đó.**
   - Lượt chạy agent vốn đã không ném lỗi ra ngoài (`_eval_run`).
   - Phần chấm điểm (judge từ chối, rule không đọc được) giờ được bọc ở `_guarded_result`: case đó `failed`,
     kèm lý do. Trước đây judge lỗi làm hỏng cả request.
   - Lỗi ở mức cả run, ví dụ không ghi được kết quả, làm run `failed` kèm `error`.
4. **Chỉ run `completed` là bằng chứng.**
   - `EvalRun.status` (`running` / `completed` / `failed`). `latest_run` (cổng duyệt, hồ sơ duyệt, báo cáo PM)
     và bảng PM chỉ đọc run `completed`. Run đang chạy hay đã dừng không thay được run xong gần nhất.
   - Route `…/eval-runs/latest` thì trả run mới nhất bất kể trạng thái, để tab Chấm điểm mở lại đúng run đang
     chạy khi người tạo quay lại. `blocked_reason` trên đó vẫn tính theo run `completed`.
5. **Khởi động lại thì không resume.** Job sống trong process đã nhận request. Lúc boot,
   `recover_interrupted_eval_runs` đánh `failed` mọi run còn `running`, như `recover_interrupted_ingestions`.
6. **UI.** Tab Chấm điểm hiện "Đang chạy 37/150", hỏi lại run mỗi 2 giây, báo khi xong hoặc khi dừng. Rời màn
   hình chỉ dừng việc hỏi lại, không dừng run.

## Considered Options

- **Hàng đợi riêng (Celery, Cloud Tasks).** Loại: repo chưa có worker nào, và ingest đã chạy được trên
  `BackgroundTasks`. Đổi sang hàng đợi thật là việc của lúc cần chạy nhiều process hoặc sống qua restart.
- **Bảng job riêng cạnh `eval_runs`.** Loại: ticket muốn `GET …/eval-runs/{id}` trả cả tiến độ lẫn kết quả.
  Một cột trạng thái, cộng bộ lọc `completed` ở hai chỗ đọc `EvalRun`, ít hơn một bảng mới và phép join.
- **Stream tiến độ qua SSE.** Loại: người tạo phải rời màn hình được; hỏi lại một GET rẻ và chịu được mất kết nối.

## Consequences

- Các case trong một run chạy song song dùng chung `KnowledgeStore` / `DatabaseReader` của request. Session
  của chúng được dùng tiếp sau khi request đóng, như các route streaming vẫn làm.
- Model giả lập theo thứ tự (`scripted_chat_model_factory`) chỉ còn hợp với bộ đề một case hoặc
  `eval_concurrency = 1`. Test nhiều case dùng model định tuyến theo prompt.
- Test qua transport ASGI chạy job nền xong rồi mới trả phản hồi POST. Helper `finished_eval_run` đọc lại run
  bằng GET. Điều "request trả trước khi case nào chạy" được kiểm qua chính phản hồi: `running`, 0/N.
- AC "100 case < 5 phút trên agent demo" phụ thuộc độ trễ của model thật. Test chỉ kiểm giới hạn đồng thời và
  thứ tự, không đo thời gian thật.
- Cửa eval V1 (`…/eval-runs`) vẫn chạy đồng bộ: nó không phải đường của bộ đề ≥ 100 case.
