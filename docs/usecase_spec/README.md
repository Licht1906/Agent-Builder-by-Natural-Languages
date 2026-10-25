# Use case specs

Đặc tả use case chi tiết, ở độ sâu đủ để cắt thành ticket trực tiếp — khác với `docs/specs/` (hand-off spec cấp platform) và `docs/adr/` (quyết định kiến trúc đơn lẻ, đã chốt).

## Cấu trúc

```
docs/usecase_spec/
├── README.md                  — file này
├── platform/                  — artifact dùng chung cho MỌI use case
│   ├── agentspec.schema.json  — JSON Schema của AgentSpec
│   ├── authz.rego             — Policy Enforcement Point (OPA)
│   └── meta-agent-system-prompt.md
└── uc1-ar-assistant/          — một thư mục cho mỗi use case
    ├── spec.md                — đặc tả chi tiết
    ├── tickets.md             — phân rã ticket, sẵn sàng gh issue create
    ├── agentspec.yaml         — bản AgentSpec cụ thể của use case này
    └── schema.sql             — DDL riêng của use case này (nếu có)
```

Quy tắc chọn `platform/` vs `uc1-.../`: nếu một artifact vẫn đúng nguyên văn khi đổi sang use case khác (schema, policy engine, prompt của agent mẹ), nó thuộc `platform/`. Nếu nó mã hoá riêng nghiệp vụ của use case này (spec cụ thể, schema DB nghiệp vụ, kịch bản demo), nó nằm trong thư mục use case. Thêm UC-2 sau này chỉ cần một thư mục `uc2-<tên>/` mới, dùng lại nguyên `platform/`.

## Danh sách use case

| Use case | Trạng thái | Mô tả một câu |
|---|---|---|
| [`uc1-ar-assistant/`](uc1-ar-assistant/spec.md) | Draft chi tiết, sẵn sàng cắt ticket | Trợ lý theo dõi công nợ quá hạn cho phòng Kinh doanh — meta-agent platform on-prem, kiến trúc OPA + Vault + LangGraph compiler |

## Quan hệ với `docs/specs/v2-agent-platform.md` và `docs/adr/0020`–`0045`

Đây là điểm cần nói rõ, không lảng tránh: `docs/specs/v2-agent-platform.md` cùng chuỗi ADR 0020–0045 mô tả một kiến trúc platform **khác** với kiến trúc trong `uc1-ar-assistant/spec.md` — khác cấu trúc dữ liệu trung tâm (`AgentSolutionSpecification` + `WorkflowJSON` theo node/edge, so với `AgentSpec` một khối của UC-1), khác cơ chế phân quyền (JWT hai vai trò `creator`/`admin` không có RLS/OPA, so với PEP-OPA + Postgres RLS của UC-1), và UC-1 có những phần V2 chưa có (Vault, hash-chain audit log, dry-run probe, air-gap packaging).

Theo quyết định của người yêu cầu tài liệu này (2026-08-13): **UC-1 là kiến trúc mục tiêu đi tiếp, không phải V2.** V2 hiện tại được đánh giá là bản còn tồn đọng nhiều vấn đề chưa giải quyết (xem danh sách "Việc chưa thiết kế" cuối `v2-agent-platform.md`). Định hướng là chuyển kiến trúc platform sang mô hình của UC-1.

Việc thư mục này ghi lại (reorg + viết lại `spec.md`/`tickets.md`) **không** tự động thay đổi `CONTEXT.md`, các ADR đã `accepted`, hay code trong `src/` — những thứ đó vẫn mô tả đúng hệ thống đang chạy hôm nay. Chính thức hoá việc chuyển kiến trúc (ghi một ADR mới nêu rõ V2 bị thay thế bởi kiến trúc gì, cập nhật `CONTEXT.md`, và lập kế hoạch di trú phần `src/` đã có) là một quyết định lớn hơn phạm vi của một lần dọn thư mục docs, và nên là một ticket/ADR riêng trước khi bất kỳ ai bắt đầu code theo `uc1-ar-assistant/`. Cho tới lúc đó, coi nội dung thư mục này là **đặc tả mục tiêu đã duyệt về mặt sản phẩm, chưa được phản ánh vào ADR/CONTEXT.md/code.**
