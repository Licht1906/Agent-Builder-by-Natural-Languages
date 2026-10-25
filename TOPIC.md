## Agent Builder by Natural Languages – Trợ lý dựng Agent mới từ mô tả ngôn ngữ tự nhiên

**📍 Thực trạng:** Mỗi đội sản phẩm muốn có agent riêng nhưng thiếu kỹ sư AI; việc dựng agent (chọn tool, prompt, luồng) mất nhiều tuần.

**🎯 Vấn đề:** Cần một meta-agent nhận mô tả nghiệp vụ, tự lập kế hoạch kiến trúc agent (tools, bước, guardrails), sinh cấu hình/luồng LangGraph, chạy thử và cho phép triển khai.

**🔒 Ràng buộc:** HITL duyệt trước khi deploy agent do người dùng tạo; kiểm soát quyền truy cập tool nhạy cảm; eval agent sinh ra đạt tiêu chí tối thiểu; cảnh báo chi phí/độ trễ dự kiến.

**Tech stack:**
- LangGraph (meta-agent + agent sinh ra)
- LLM đa provider
- Thư viện tool chuẩn (search, DB, API)
- DeepEval smoke test
- Langfuse
- Backend FastAPI, PostgreSQL lưu định nghĩa agent
- Frontend Next.js (wizard + visual graph)
- Deploy Docker

**Cơ bản:**
- Web deploy, đăng nhập ≥2 vai trò (Người tạo agent, Admin duyệt)
- Nhập mô tả → agent sinh cấu hình, chọn tool, chạy thử trong sandbox
- Xem luồng trực quan

**Nâng cao:**
- Agent tự eval agent sinh ra, đề xuất cải thiện, HITL phê duyệt deploy
- Versioning agent, memory tái sử dụng mẫu, cảnh báo tool rủi ro

---