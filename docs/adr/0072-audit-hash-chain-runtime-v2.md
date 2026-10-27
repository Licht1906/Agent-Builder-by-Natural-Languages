---
status: accepted
---

# Audit hash-chain trong runtime V2

ADR-0059 §11 giữ audit hash-chain của UC-1 và chuyển nó vào runtime V2, để dùng cả trên nền tảng lẫn trong gói xuất ra. Bản UC-1 (`audit_log` trong `schema.sql`) là một trigger `plpgsql` ghi mỗi quyết định của PEP. V2 không có PEP; thứ cần kiểm toán là các lần đọc Hệ CSDL và các hành động gửi đi.

## Quyết định

1. **Ghi gì.** Mỗi lần chạy node `data_query` một bản: người hỏi, Hệ CSDL, view, query có cấu trúc, phạm vi đã AND vào (`[[cột, giá trị]]`), số dòng, hoặc lý do bị cấm / bị từ chối. Mỗi hành động cần người duyệt một bản: `tool`, `action_hash` (DS-08), mã lần duyệt, người duyệt, kết quả (`sent` / `simulated` / `refused` / `failed`), cờ gửi thật trong sandbox (DS-09).
2. **Không bao giờ ghi nội dung.** Không dòng dữ liệu nào, không thân thư nào. Hash của payload đủ để chứng minh đó là thư nào.
3. **Chuỗi tính bằng Python, không bằng trigger.** `entry_hash` là SHA-256 của `prev_hash` nối với JSON chuẩn hoá của mọi cột còn lại, kể cả `seq` và thời điểm. Nhờ vậy gói xuất ra (DS-13) giữ cùng một chuỗi trên bất kỳ CSDL nào nó được trỏ tới.
4. **`seq` liên tục do chuỗi cấp; ghi nối tiếp nhau qua `audit_chain_head`.** Một dòng duy nhất bị khoá `FOR UPDATE` cho mỗi lần ghi, để hai lượt chạy cùng lúc không rẽ nhánh chuỗi. Dòng đầu chuỗi giữ `last_hash`, nhờ đó phát hiện được đuôi chuỗi bị cắt.
5. **Kiểm toàn vẹn bằng cách đi lại toàn chuỗi**, có ở hai nơi: endpoint `GET /api/v1/admin/audit/verify` (chỉ admin) và lệnh `PYTHONPATH=. python scripts/verify_audit_chain.py` (thoát với mã 1 khi đứt). Lệnh báo `seq` đầu tiên bị đứt cho cả bốn kiểu: sửa nội dung, sửa lại hash để che, mất bản ghi, cắt đuôi.
6. **Ghi audit thất bại thì node thất bại.** Lượt đọc được ghi audit trước khi dòng dữ liệu tới model, nên một lượt đọc không ghi được audit thì không được dùng.
7. **Ghi ở mọi thế giới**, phân biệt bằng `mode`: `trial`, `prod`, `eval`. Bộ ghi đi qua `RunServices.audit`, mặc định là chuỗi của nền tảng. Gói xuất ra truyền bộ ghi của riêng nó.

## Considered Options

- **Giữ trigger `plpgsql` của UC-1** — loại: trói chuỗi vào Postgres và vào `schema.sql`, trong khi gói xuất ra phải chạy được trên CSDL của công ty.
- **Khoá DB chặn UPDATE/DELETE** (rule của UC-1) — chưa làm: phát hiện sửa đổi mới là thứ ticket đòi, và tài khoản chủ CSDL vẫn gỡ được rule. Có thể thêm sau như một lớp phụ.
- **Ghi cả các tham số đã gửi** — loại: thân thư và nội dung khách hàng không thuộc về sổ kiểm toán.

## Consequences

- Eval chạy hàng trăm case sẽ ghi hàng trăm bản `mode=eval`. Người đọc lọc theo `mode` và `trace_id`.
- Chuỗi chỉ chứng minh không ai sửa sổ mà không bị phát hiện. Nó không ngăn được việc sửa, và không chống được người xoá cả chuỗi lẫn dòng đầu chuỗi rồi dựng lại từ đầu.
