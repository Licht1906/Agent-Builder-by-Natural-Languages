# ADR-0059 — Hợp nhất UC-1 vào V2 thành năng lực "Nguồn dữ liệu"; agent chạy thật được xuất thành gói độc lập

- **Trạng thái:** accepted
- **Ngày:** 2026-10-25
- **Thay thế:** định hướng "UC-1 là kiến trúc mục tiêu, V2 bị thay thế" trong `docs/usecase_spec/README.md` và ghi chú đầu `CONTEXT.md` (từng trỏ tới một "ADR-0046" chưa bao giờ được commit vào repo).
- **Spec đầy đủ:** [`docs/specs/data-source-agent.md`](../specs/data-source-agent.md)

## Bối cảnh

Repo đang chạy hai pipeline song song: V2 (spec 7 trường → workflow node/edge → canvas → sandbox → eval → duyệt → deployment) là luồng chính có UI hoàn chỉnh; UC-1 (`AgentSpec` một khối, bảng `uc1_*`, trang `UC1*.jsx`, OPA sidecar, Vault, RLS) là một pipeline riêng dựng cho đúng một bài toán — trợ lý công nợ — với chức vụ nghiệp vụ (`sales_rep`, `sales_manager`) hard-code vào `UserRole` của nền tảng.

Thứ có giá trị trong UC-1 không phải pipeline của nó mà là các cơ chế: đọc DB nghiệp vụ chỉ qua view, phạm vi dữ liệu theo người hỏi, chặn hành động ghi bằng người duyệt, provenance "dữ liệu không tin cậy", audit hash-chain, đóng gói on-prem.

Thêm một sự thật vận hành: nền tảng chạy trên Google Cloud. DB nghiệp vụ của một công ty là tài sản chỉ dùng nội bộ — nền tảng không với tới, và không nên với tới, DB production của khách.

## Quyết định

1. **V2 là khung; UC-1 trở thành năng lực của agent mô tả bằng ngôn ngữ tự nhiên.** Agent công nợ trở thành một template/demo dựng bằng luồng chính, không còn là pipeline riêng.
2. **"Nguồn dữ liệu" (Data Source) là khái niệm chung**, gồm hai loại: **Tài liệu** (`KnowledgeBase` hiện có) và **Hệ CSDL** (mới, hấp thụ `UC1Connection`). Trang "Kho tri thức" đổi thành "Nguồn dữ liệu" với hai tab. Spec V2 giữ `DataSource`, thêm `kind = database` với `ref` = id Hệ CSDL.
3. **Nền tảng là xưởng dựng, không phải nơi nhân viên công ty khách dùng hằng ngày.** Trên nền tảng, người tạo agent chạy thử bằng ô "Chạy với tư cách…" (chọn một người trong bảng nhân viên của Hệ CSDL). Chạy thật là việc của **gói xuất ra**, chạy trong hạ tầng của công ty.
4. **Dựng và chạy thử trên DB mẫu/staging cùng schema**; gói xuất ra để trống DSN, runtime kiểm schema khi khởi động và từ chối chạy nếu thiếu view/cột agent dùng.
5. **Query có cấu trúc, không text-to-SQL.** Node mới `data_query`: model điền JSON (view, cột, filter theo toán tử whitelist, group/aggregate, order, limit); code dựng SQL có tham số và **luôn AND thêm bộ lọc phạm vi**. Mỗi kết quả trích dẫn Nguồn dữ liệu + view + khoá dòng.
6. **Phạm vi dữ liệu = quy tắc truy cập khai báo (`access.yaml`)**: `identity` (view nhân viên, khoá khớp, các thuộc tính đọc từ DB) + `rules` (chức vụ → view → `all` | `where: {cột_view: thuộc_tính_người_hỏi}`). **Mặc định cấm.** Áp ở tầng code (mọi DB); Postgres/SQL Server còn được đặt thuộc tính người hỏi vào phiên DB làm lớp phòng thủ phụ cho RLS phía khách (tuỳ chọn). LLM không bao giờ quyết định phạm vi.
7. **Hành động gửi mail dùng cơ chế V2**: `human_approval` do chính người thao tác duyệt, **bắt buộc** trước mọi hành động gửi ra ngoài (V2 đã suy rủi ro từ tác dụng và không có `never`), bổ sung ràng buộc `action_hash` — duyệt đúng payload đó. Không giới hạn người nhận. Trên nền tảng gửi qua Gmail OAuth; trong gói, kênh mail là điểm cắm.
8. **Hỗ trợ PostgreSQL, SQL Server, MySQL** qua một lớp dialect. MySQL không có lớp phòng thủ phụ phía phiên DB.
9. **Credential qua một giao diện `SecretStore`.** Nền tảng dùng `EncryptedDbSecretStore`, bỏ Vault khỏi deploy (đang chạy dev-mode, mất dữ liệu khi khởi động lại). Gói xuất ra chọn backend env / file / Vault.
10. **Gói xuất ra = runtime cố định + cấu hình sửa được**: image runtime (không có xưởng dựng), bundle (spec + workflow), `access.yaml`, `runtime.yaml` (`auth.mode`: `oidc` | `jwt` | `trusted_header` | `custom`; `mail.mode`; `llm` OpenAI-compatible; DSN secret), UI chat tối giản, tài liệu gốc kèm Qdrant và ingest lại bằng endpoint embedding của công ty. Công ty chỉnh cấu hình/adapter; PEP-tương-đương, duyệt, provenance, audit không nằm trong phần sửa được.
11. **Gỡ khỏi nền tảng**: pipeline `AgentSpec` + các trang/bảng `uc1_*`, hàng chờ `uc1_pending_actions`, OPA sidecar, role Postgres theo agent, `sales_rep`/`sales_manager` trong `UserRole`. **Giữ và chuyển vào runtime V2**: provenance, audit hash-chain, seed ERP mẫu, đóng gói air-gap.

## Hệ quả

- Một agent duy nhất phục vụ cả tổ chức: cùng agent, mỗi người hỏi thấy một tập dòng khác nhau — vì phạm vi đến từ danh tính đã xác thực và quy tắc khai báo, không đến từ hội thoại.
- "Dữ liệu thật không rời công ty" chỉ đúng khi `llm.base_url` của gói trỏ vào LLM nội bộ. Gói phải nói thẳng điều này (README + chú thích trong `runtime.yaml`).
- Quy tắc truy cập chỉ có một nơi khai báo (`access.yaml` / cấu hình Hệ CSDL), không còn đồng bộ với `authz.rego`.
- Lệch có chủ đích với V2: sandbox V2 luôn mô phỏng action rủi ro (`SandboxBehavior.MOCK`). Để demo gửi mail thật, sandbox có một công tắc gửi thật **theo từng lượt chạy**, mặc định tắt, chỉ có tác dụng sau khi `human_approval` đã duyệt.
- Phần `docs/usecase_spec/uc1-ar-assistant/` trở thành tài liệu lịch sử + nguồn cho dữ liệu demo; không còn là đặc tả để code theo.
