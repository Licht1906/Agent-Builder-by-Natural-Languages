---
status: accepted
---

# Retire case khi spec đổi, và "Thay đổi bộ đề" ở màn duyệt

ADR-0069 §4–6 muốn bộ đề luôn khớp spec hiện tại: case không còn phù hợp bị retire (không giữ orphan), case mới
được sinh cho phần mới, người tạo đọc từng case rớt. Đồng thời người duyệt phải thấy rõ bộ đề đã đổi gì. Lý do:
đường vòng dễ nhất qua cổng là sửa câu chữ một criterion để rũ bỏ những case khó.

## Quyết định

1. **Retire thay cho orphan.**
   - Cột mới trên case: `retired_in_version_id`, `retired_reason`, `retired_at`, `retired_failing_at_baseline`.
   - Case bị retire ra khỏi suite: `list_cases` không trả về, không chạy, không đếm. Nó không bị xoá và vẫn đọc
     được trong lịch sử (`GET …/eval-suite/history`).
   - **Phần `orphan` của ADR-0054 §1 ("vẫn chạy, vẫn chấm") được thay**, đúng như ADR-0069 §5.
2. **Chỉ retire những case mà chính lần đổi này làm hỏng.**
   - Điều kiện: version trước chấm được case đó (`live`), còn version này thì không (`orphan` vì criterion đã đổi
     chữ hoặc bị bỏ; `ungrounded` vì action hay node không còn).
   - Case vốn đã không khớp từ trước thì để nguyên, ví dụ case nạp từ file với một criterion lạ. Đó không phải do
     lần đổi này gây ra.
   - Chỉ version **mới nhất** của agent mới retire. Chạy lại eval trên một version cũ không được viết lại bộ đề mà
     các version sau đang dùng.
   - Retire xảy ra ở hai chỗ: khi dựng bộ đề (`POST …/eval-suite/build`), và ngay trước mỗi lần chạy eval trên
     version mới nhất.
3. **Sinh case cho phần mới.** Sau khi retire, "Dựng bộ đề" luôn sinh case cho mọi criterion và điều cấm chưa có
   case nào. Một criterion đổi chữ được coi là criterion mới, kể cả khi suite đã đủ 100. Phần còn thiếu tới 100 vẫn
   bù như EV-03.
4. **Mọi thay đổi được ghi lại** trong bảng `eval_suite_changes`, gồm loại, người làm, thời điểm, version, và nội
   dung case lúc đó. Bản ghi xoá giữ lại nội dung của case, nên vẫn còn sau khi case bị xoá. Có bốn loại:
   - hệ thống retire;
   - người tạo sửa;
   - người tạo xoá;
   - người tạo đọc một case rớt: "agent sai" hoặc "case sai" (`POST …/eval-cases/{id}/triage`).
     Đọc là "case sai" thì người tạo sửa hoặc xoá tiếp, và các thay đổi đó cũng được ghi.
5. **Màn duyệt có khối "Thay đổi bộ đề"**, tính từ baseline (bản trước gần nhất có run gate) tới version đang xin
   duyệt:
   - hệ thống retire N case, **trong đó K case đang rớt ở baseline**;
   - người tạo sửa / xoá M case, kèm tên người làm;
   - P case mới chưa có trong run của baseline.
6. **Báo cáo Baseline · Candidate · Delta chỉ so trên phần giao.**
   - Phần giao: những case cả hai run đều hỏi, ghép theo `suite_case_id`.
   - Case mới được liệt kê riêng là "chưa có baseline", không tính vào delta.
   - Ngưỡng của cổng vẫn đọc toàn bộ run candidate, như cổng vẫn làm.
   - Baseline cũ chưa có `suite_case_id` thì vẫn so toàn bộ như trước.

## Considered Options

- **Retire mọi case `orphan`/`ungrounded`**, đúng chữ ADR-0069 §5. Loại: cách này retire luôn những case người tạo
  tự nạp với criterion riêng, chỉ vì một lần chạy eval. ADR-0069 nói tới orphan do *spec đổi*, nên chỉ những case
  mà lần đổi đó làm hỏng mới bị retire.
- **Retire ngay lúc tạo version.** Loại: version được tạo ở nhiều nơi (lưu canvas, phân nhánh, nhân bản), và việc
  sinh case cần model. Retire lúc chạy eval và lúc dựng bộ đề là đủ để cổng không bao giờ chấm trên case đã lỗi
  thời.
- **Xoá mềm cả case người tạo xoá.** Loại: người tạo xoá là việc của họ. Nhật ký có tên người làm và nội dung case
  là đủ cho người duyệt.

## Consequences

- Nếu người tạo đổi spec mà không bấm "Dựng bộ đề", lần chạy eval tiếp theo vẫn retire case cũ, nhưng không có
  case cho criterion mới cho tới khi họ dựng lại. Màn duyệt khi đó cho thấy N retire mà không có P mới.
- Migration `f5c3a8e2b6d9` là một trong những migration mà test suite không chạy (test tạo schema bằng
  `create_all`). Nó đã được chạy lên và xuống trên DB dev.
