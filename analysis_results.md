# Phạm vi & Đối tượng sử dụng: Agent Builder by Natural Languages

Tài liệu này tổng hợp kết quả phiên phỏng vấn `/grill-me` để định nghĩa rõ **Đối tượng sử dụng** và **Phạm vi chức năng (Scope)** của sản phẩm AI Agent Builder.

---

## 1. Đối tượng sử dụng chính (Target Users)

Hệ thống hướng tới hai vai trò chính trong doanh nghiệp/tổ chức:

1. **Người tạo Agent (Creator) — Business Analyst (BA) / Product Manager (PM)**:
   - **Đặc điểm**: Hiểu rất rõ nghiệp vụ, quy trình logic của đội nhóm mình. Có khả năng tư duy logic tốt, hiểu khái niệm "các bước xử lý", "prompt chỉ dẫn", "quyền truy cập công cụ" (tools).
   - **Hạn chế**: Không biết lập trình AI, không biết code Python/LangGraph.
   - **Kỳ vọng**: Dùng giao diện ngôn ngữ tự nhiên để mô tả và được meta-agent hướng dẫn thiết kế đồ thị luồng xử lý mà không cần viết code.
2. **Người phê duyệt (Admin/Approver)**:
   - **Đặc điểm**: Đội ngũ Quản trị/Nền tảng (Platform team) hoặc bảo mật hệ thống.
   - **Kỳ vọng**: Kiểm soát chặt chẽ các agent được sinh ra trước khi chạy thật để tránh rò rỉ dữ liệu, gọi API bừa bãi hoặc gây tăng đột biến chi phí LLM.
3. **Người dùng cuối (End-user)**:
   - Bất kỳ thành viên nào trong tổ chức đã đăng nhập đều có thể chat với các agent đã được duyệt và deploy. Phiên bản MVP sẽ **không phân quyền chi tiết** cho người dùng cuối (không chia group/phòng ban nào được dùng agent nào).

---

## 2. Phạm vi chức năng (Scope) của MVP

Dựa trên các quyết định đã thống nhất qua phỏng vấn, phạm vi hệ thống được xác định cụ thể như sau:

### A. Độ phức tạp của luồng (Workflow Topology)
- **Hỗ trợ Đồ thị tùy ý (Arbitrary DAG)**: Meta-agent có thể thiết kế các cấu trúc đồ thị phức tạp với vòng lặp và rẽ nhánh tùy ý dựa trên mô tả nghiệp vụ của người tạo.
- **Cơ chế điều hướng (Routing)**: Sử dụng **Router Node/Edge bằng LLM (prompt-based routing)**. Các điều kiện rẽ nhánh được định nghĩa bằng ngôn ngữ tự nhiên trong `AgentSpec` (ví dụ: *"Nếu khách hàng đồng ý -> Đi tới bước A"*). Khi chạy, một LLM Router nhỏ sẽ quyết định hướng đi mà không cần viết code logic.

### B. Kiểm soát an toàn đồ thị (Validation & Safety)
- **Kiểm tra tĩnh (Static Validation)**: Thực hiện ngay khi sinh/sửa Spec để phát hiện sớm các lỗi đồ thị như node mồ côi (unreachable), hoặc cạnh trỏ vào node không tồn tại (dangling edges).
- **Cưỡng chế động (Dynamic Limit)**: Giới hạn tổng số node tối đa mà một phiên chạy được phép đi qua (ví dụ: tối đa 20 node) để ngăn chặn vòng lặp vô hạn (infinite loop) ở runtime.

### C. Đăng ký & Mở rộng Công cụ (Tools)
- **Tool Registry khai báo cứng (Code-defined)**: Các tool nền tảng (Search, DB read-only, Email) được Dev viết bằng code Python (gồm bản thực tế và bản mô phỏng).
- **Cửa thoát `http_request`**: Để các đội sản phẩm tự kết nối với API nghiệp vụ riêng của họ mà không cần chờ Dev code thêm tool mới. Creator cấu hình endpoint trong Spec, và Admin duyệt dựa trên **allowlist domain** được khai báo.

### D. Cơ chế chạy thử & Mô phỏng (Sandbox Mocking)
- **Mô phỏng tĩnh/động cho từng tool**: Lập trình viên khi viết tool có tác dụng phụ (side-effects) bắt buộc phải viết kèm hàm mô phỏng (mock). Hàm mock trả về dữ liệu mẫu hợp lệ để các bước tiếp theo trong DAG vẫn có dữ liệu đầu vào chạy bình thường.

### E. Tinh chỉnh Đồ thị qua Chat (DAG Modification)
- **Tự động kết nối lại các cạnh (Auto Re-routing)**: Khi người tạo yêu cầu chỉnh sửa cấu trúc qua chat (ví dụ: *"bỏ bước 3"*), meta-agent sẽ tự động tính toán lại các cạnh để nối lại đồ thị bị đứt gãy và giải thích rõ ràng cho người dùng.

### F. Đánh giá chất lượng (Evaluation)
- **Đánh giá bằng LLM Judge định tính**: Test case gồm `input` và `expected_criteria` dạng ngôn ngữ tự nhiên. Một LLM độc lập (Judge) sẽ đánh giá đầu ra của agent có thỏa mãn tiêu chí hay không.

### G. Kiến trúc LLM Providers
- **Đa provider qua LangChain**: Thiết kế hệ thống linh hoạt hỗ trợ nhiều provider qua `init_chat_model` và lưu cấu hình API keys tập trung trong file `.env` của backend.

---

## 3. Bản đồ Quyết định (Decision Summary)

| Trục quyết định | Lựa chọn đã thống nhất | Ý nghĩa kiến trúc |
| :--- | :--- | :--- |
| **Target Creator** | BA / PM (Tech-aware) | Giao diện hiển thị trực quan (sơ đồ đồ thị chỉ đọc), cho phép xem/sửa prompt và quyền hạn của từng bước. |
| **Workflow Topology** | Arbitrary DAG (Đồ thị tùy ý) | Cần sử dụng thuật toán tự động dàn trang (Auto-layout) như `dagre` hoặc `@elkjs/elk` ở frontend để tự vẽ đồ thị. |
| **Routing Mechanism** | Prompt-based Routing (LLM Router) | Giữ `AgentSpec` thuần dữ liệu JSON, không chứa code Python thực thi, an toàn cho hệ thống. |
| **Validation & Safety** | Static Validation + Runtime Node Limit | Ngăn chặn lỗi thiết kế đồ thị của BA/PM và tránh vòng lặp vô tận đốt token. |
| **Tool Integration** | Code-defined Registry + `http_request` | Đạt cân bằng giữa bảo mật (Registry đóng) và khả năng tự do tích hợp của các đội (HTTP request whitelist). |
| **Evaluation Method** | LLM Judge với tiêu chí định tính | Phù hợp với tính biến thiên của ngôn ngữ tự nhiên, cấu hình test đơn giản cho BA/PM. |
