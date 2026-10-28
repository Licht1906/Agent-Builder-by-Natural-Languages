---
status: accepted
---

# "ERP mẫu" — database của công ty demo

Kịch bản demo (spec §Kịch bản demo, DS-24) cần một DB nghiệp vụ để cắm làm Hệ CSDL: nhân viên KD thấy khoảng 3 khách quá hạn, trưởng nhóm khoảng 15, có email khách để gửi nhắc nợ, có một ghi chú chứa câu inject. Trước đây dữ liệu demo nằm ngay trong Postgres của nền tảng: bề mặt dữ liệu của UC-1, có RLS ép buộc. Seed của nó chết trên Cloud SQL (`COPY FROM` bị RLS chặn, `DEPLOYMENT-TODO.md` §1).

## Quyết định

1. **Một database riêng.**
   - Local: service compose `erp-mau` (Postgres, cổng 5433). GCP: database `erp_mau` trên cùng instance Cloud SQL.
   - Nó đứng thay ERP của khách hàng, mà ERP của khách không bao giờ là database của nền tảng. Cắm vào nền tảng qua đúng cửa Hệ CSDL như một khách thật.
2. **Schema `deploy/erp-mau/schema.sql`: dựng từ `schema.sql` của UC-1 nhưng bỏ RLS, role theo agent, GUC phiên và bảng audit.**
   - Phạm vi giờ do nền tảng áp ở tầng code (quy tắc truy cập, DS-03/04), audit do runtime ghi (DS-10). DB demo chỉ cần dữ liệu và view.
   - Có ba view: `v_employees` (email, mã NV, phòng ban, chức vụ), `v_ar_open`, `v_customer_safe` (gồm `contact_email` và `note`).
   - Các cột nhạy cảm (`bank_account`, `tax_id`, `credit_limit_internal`) vẫn nằm trong bảng nhưng không view nào chiếu ra.
3. **Seed `scripts/seed_erp_mau.py`.**
   - Dữ liệu tường minh, không ngẫu nhiên. Nhân viên KD 3 khách quá hạn trên 30 ngày, trưởng nhóm 15 (cả phòng), kế toán trưởng 21 (mọi phòng).
   - Seed xoá rồi ghi lại mọi dòng demo trong một transaction, bằng `INSERT` thường. Vì vậy chạy lại không đổi gì, và không có RLS nào để vướng.
   - Hạn trả tính lùi từ ngày chạy. Một seed chỉ đúng vào ngày nó chạy, nên compose (`erp-mau-seed`) và GCP (`backend-seed-erp-mau` trong `deploy.yml`) chạy nó mỗi lần khởi động hoặc deploy.
   - Email khách là `<mailbox>+kh-xxxx@…` của `ERP_MAU_CUSTOMER_MAILBOX`, một hộp thư người demo mở được. Mặc định là `khachhang@erp-mau.local`, MailHog bắt được.
   - Một khách của nhân viên KD có ghi chú chứa chỉ dẫn gửi danh sách ra ngoài.
   - Ba nhân viên đầu là `DEMO_ASKERS` của bộ đề mẫu (DS-22).
   - Seed tự kiểm số dòng của từng nhân viên demo; sai thì rollback và thoát mã 1.
4. **Quy tắc truy cập mẫu `deploy/erp-mau/access.yaml`.** Nhân viên KD xem khách của mình, trưởng nhóm xem cả phòng, kế toán trưởng xem tất cả. Test đăng ký DB làm Hệ CSDL với quy tắc này rồi đọc qua `PlatformDatabaseReader` với tư cách từng nhân viên.
5. **Gỡ `scripts/seed_uc1_demo_data.py`.** `make seed-demo` giờ seed ERP mẫu. Bề mặt dữ liệu UC-1 trong DB nền tảng (migration `a4c2c2363959`, `verify_uc1_data_surface.py`) để nguyên; ticket này không đụng tới.

## Consequences

- Trên GCP, người tạo cắm ERP mẫu bằng private IP của instance và phải thêm IP đó vào `DATABASE_SOURCE_TRUSTED_HOSTS`. Trong compose, backend đã đặt sẵn `erp-mau`.
- `verify_uc1_data_surface.py` vẫn là cổng chặn của job migrate trên GCP và vẫn vướng RLS như `DEPLOYMENT-TODO.md` §2 mô tả. Việc đó nằm ngoài ticket này.
