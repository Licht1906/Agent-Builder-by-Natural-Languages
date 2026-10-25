---
status: accepted
---

# Bộ đề eval ≥100 case: thư viện template + case sinh từ cassette

Agent do người dùng tạo bằng lời không có ai viết tay bộ đề, nên mỗi agent nhận một suite **≥100 case** dùng cho hai việc — **cổng duyệt** và **hồi quy giữa các version** (red-team là vòng riêng, không tính điểm cổng; case đáng giữ thì promote vào suite). Suite được dựng gần như không cần người nhập: phần hành vi/an toàn đổ từ một **thư viện template dùng chung có tag**, phần nghiệp vụ được sinh riêng cho agent với đáp án lấy từ một **cassette** dữ liệu thật đã đóng băng. Grilling 2026-10-25.

## Quyết định

1. **Hai nguồn case.** Thư viện template trong repo (`eval/library/`, review qua PR — tài sản của nền tảng, không phải của người tạo) gắn tag theo năng lực (`injection`, `leak`, `cross_tenant`, `out_of_scope_refusal`, `asks_back_missing_param`, `approval_required`, `prohibition`, `knowledge_grounding`), chấm bằng rule; hệ thống tự khớp tag với capability của version, điền slot bằng code, lưu bản đã điền (không tham chiếu ngược thư viện). Phần còn thiếu để đủ 100 do generator sinh case nghiệp vụ. Tỉ lệ ~40/60 là điển hình, không phải hạn ngạch. Nháp thư viện sinh bằng `gpt-6-luna`, người duyệt.
2. **Eval đọc từ cassette, rehearsal vẫn đọc live.** Khi case được tạo, hệ thống gọi thật action đọc / `data_query` với tham số và `run_as` của case rồi đóng băng response, khoá `(action, tham số, run_as)`. Mọi eval run phát lại cassette. ADR-0023 §4 (`LIVE_READONLY`) giữ nguyên cho chạy thử tương tác — chỉ eval mới cần dữ liệu đứng yên để Baseline và Candidate so trên cùng một thứ.
3. **Đáp án do code tính khi có thể.** Case `data_query` mang query có cấu trúc (ADR-0059 §5); kết quả của chính query đó trên cassette là đáp án, chấm bằng rule (`must_contain` các giá trị chủ chốt). Response text tự do: `gpt-6-luna` đọc cassette viết `expected` + `must_contain`. Case có cassette rỗng/lỗi hoặc đáp án tầm thường bị loại khi sinh.
4. **Người chỉ duyệt case rớt** — "agent sai" hay "case sai → sửa/xoá". Không duyệt trước.
5. **Spec đổi → retire, không giữ orphan.** Case `ungrounded` và `orphan` bị hệ thống retire (`retired_in_version_id`, không xoá); case mới được sinh cho phần mới. **Thay phần `orphan` của ADR-0054 §1** ("vẫn chạy, vẫn chấm"). Delta chỉ so trên phần giao; case mới liệt kê riêng.
6. **Người duyệt thấy mọi thay đổi bộ đề**, tách hai loại: hệ thống retire (kèm số case *đang rớt ở baseline* bị retire) và người tạo xoá/sửa tay. Đây là chỗ chặn đường vòng "sửa câu chữ criterion để rũ case khó".
7. **Hai tầng chạy.** Smoke ~20 case (toàn bộ case an toàn + mẫu phân tầng theo criterion), có cache, < ~1 phút. Gate đủ bộ, **không bao giờ đọc cache** — kết quả chỉ được đóng dấu lên version đã tạo ra nó (ADR-0054) — chạy nền song song có giới hạn, < ~5 phút. Không dùng Batch API: judge chỉ chấm phần nhỏ của suite, lời ít mà người tạo phải chờ lâu.
8. **Trial.** Ở gate, case an toàn chạy 3 lần và phải đậu cả 3; case nghiệp vụ 1 lần. Luật cổng giữ nguyên `src.evals.gate`: vi phạm an toàn không override được, điểm nghiệp vụ override được.
9. **Judge `gpt-6-luna`, ghim snapshot.** Đổi judge làm mất baseline (`src.pm.reports` không so delta khác `judge_model`), nên phải re-baseline version hiện hành của mỗi agent trước khi đổi.

## Considered Options

- **Pool ~1000 câu viết sẵn, chọn theo tag** — loại: với agent do người dùng tạo, câu nghiệp vụ viết sẵn không khớp domain; chỉ phần hành vi là dùng chung được, và phần đó viết dạng template.
- **Fixture tổng hợp do LLM sinh** — loại: dữ liệu quá sạch, lại thành "evidence about the mock". **Đọc live trong eval** — loại: dữ liệu trôi làm cổng chấm nhầm và delta vô nghĩa.
- **LLM tự đặt `expected`** (như `generate_cases` hiện tại) — loại cho mọi case đáp án tính được bằng code.
- **Cache cả ở gate** — loại: tiết kiệm chủ yếu nằm ở vòng lặp smoke, còn cache ở gate đóng dấu kết quả của version trước lên version này.

## Consequences

- Cassette chứa dữ liệu thật của người dùng → lưu và giữ theo cùng chính sách với Evidence. Đổi schema connection → cassette stale, baseline liên quan không so được.
- Suite không phình qua các version, nhưng mỗi lần đổi spec kéo theo một lượt sinh case + ghi cassette mới.
- `EvalCaseOrigin` cần thêm một giá trị cho case đổ từ thư viện.
