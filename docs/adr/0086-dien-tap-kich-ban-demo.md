---
status: accepted
---

# Diễn tập kịch bản demo: kịch bản theo code, không nới luật người nhận

DS-24 yêu cầu chạy trọn kịch bản demo 8 phút trong spec (§Kịch bản demo), ghi checklist và viết lại runbook.
Đi qua từng bước bằng code (test API trên ERP mẫu thật, model giả lập) thì thấy bước 4 và 5 không chạy được
với canvas mà spec mô tả: `collect → data_query → agent_llm → human_approval → gmail.send_email`.

- **Node `collect` đứng đầu, luôn đòi đủ tham số bắt buộc của `send_email`, gồm `to`.** Mọi câu gửi tới agent
  này đều bị hỏi lại "email người nhận", kể cả "khách nào quá hạn trên 30 ngày?". Bước 4 ("cùng agent, khác
  dữ liệu") vì thế không chạy được trên agent gửi thư.
- **`to` phải là chữ người dùng tự gõ (ADR-0052, luật `verbatim`/`email`).** "Soạn email nhắc khách Công ty ABC"
  không lấy được `contact_email` từ DB. Collect hỏi lại địa chỉ và run dừng.
- **Vì cùng luật đó, địa chỉ trong câu inject không bao giờ lên được thẻ duyệt.** Spec muốn cảnh "thẻ duyệt
  lộ người nhận lạ → Từ chối". Code chặn sớm hơn: địa chỉ bị loại trước khi có thẻ.

## Quyết định

1. **Sửa kịch bản theo code, không sửa nền tảng.** Người dùng chọn hướng này (2026-10-29). Nới luật người nhận
   để địa chỉ trong dữ liệu cũng tính là "đã cung cấp" sẽ cần thêm nhánh ý định trong template outreach (chỉ
   đi qua collect khi người dùng muốn gửi). Đó là thay đổi lớn ở runtime, template và prompt meta-agent, và
   nới một chốt an toàn ADR-0052 dựng có chủ đích. Nếu muốn, làm thành ticket riêng.
2. **Bước 4 chạy trên agent "Tra cứu công nợ" (mẫu `database_qa`), dựng sẵn trước buổi diễn.** Bước 5 chạy trên
   agent gửi thư (mẫu `database_outreach`) mà meta-agent vừa dựng ở bước 3.
3. **Ở bước 5, người demo gõ luôn địa chỉ của khách** (`<hộp-thư>+kh-0001@…`, từ seed DS-23). Thẻ duyệt hiện
   đúng địa chỉ đó; duyệt khi bật Gửi thật thì gửi đúng một thư.
4. **Cảnh inject đổi thành:** "Làm theo ghi chú của khách KH-0002" → agent không có thẻ duyệt, chỉ hỏi lại
   email người nhận. Thêm một test: người dùng gõ đúng địa chỉ khách, nhưng model bị ghi chú dắt và viết người
   lạ vào lời gọi. Thẻ vẫn hiện địa chỉ người dùng gõ, vì giá trị collect đã kiểm thắng giá trị model điền lại.
5. **Phần máy làm được thì máy làm.**
   - `tests/test_api/test_demo_rehearsal.py` chạy bước 4–5 qua API: ERP mẫu seed thật, quy tắc
     `deploy/erp-mau/access.yaml`, graph lấy đúng từ hai template, chỉ model và Gmail là giả. Kết quả là
     3 / 15 / vẫn 3; một lần gửi đúng địa chỉ đã duyệt; inject không ra thẻ.
   - `scripts/demo_preflight.py` kiểm tra trước giờ diễn: số liệu ERP mẫu, `/health` của backend, MailHog nếu có.
6. **Runbook mới ghi đè `docs/uc1-demo-runbook.md`.** Nội dung: ngân sách thời gian từng bước, nội dung cần gõ,
   kết quả mong đợi, phương án dự phòng khi Gmail hoặc LLM chậm, bảng nhật ký cho hai lần diễn tập.
   - Bước 6 xuất gói từ một agent đã chấm điểm và đã duyệt từ hôm trước, vì chấm 23 ca rồi chờ người duyệt
     không vừa một phút.

## Consequences

- Kịch bản trên sân khấu lệch spec ở ba chỗ: hai agent, người demo gõ địa chỉ, cảnh inject không có thẻ.
  Spec §Kịch bản demo trỏ sang ADR này.
- **Môi trường diễn tập là local, không phải GCP như ticket ghi.** Chưa có bản deploy GCP nào: `deploy.yml`
  chưa chạy được lần nào (`DEPLOYMENT-TODO.md` §3, hết quota GitHub Actions), job migrate còn vướng
  `verify_uc1_data_surface.py` (§2). Runbook lấy local (compose + backend/frontend trên máy) làm môi trường chính,
  GCP để ở phụ lục.
- Tiêu chí của DS-24 (hai lần diễn tập liên tiếp, mỗi lần ≤ 8 phút) cần người demo thật và tài khoản Gmail thật.
  Repo không làm thay được. Bảng nhật ký trong runbook là chỗ ghi kết quả.
- Test diễn tập dùng model giả lập, nên không đo được độ trễ LLM hay Gmail. Phần đó runbook xử lý bằng làm
  nóng và phương án dự phòng.
