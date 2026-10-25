Bạn chỉ cần tập trung làm **8 phần chính** sau:

## **1\. AI Solution Designer**

Cho Nhân viên văn phòng mô tả bài toán bằng ngôn ngữ tự nhiên.

Hệ thống cần:

●       Trích xuất mục tiêu, người dùng, dữ liệu, hành động và kết quả mong muốn.  
●       Phát hiện thông tin còn thiếu hoặc mâu thuẫn.  
●       Hỏi từng câu để làm rõ.  
●       Tạo **Agent Solution Specification** để người dùng xác nhận.

## **2\. Workflow Generator**

Chuyển specification thành workflow nháp.

Ví dụ:

Nhận yêu cầu  
→ Tìm tài liệu  
→ Kiểm tra điều kiện  
→ Yêu cầu phê duyệt  
→ Thực hiện hành động  
→ Trả kết quả  
   
Không nên để LLM tự sinh workflow hoàn toàn tự do. Hãy dùng một số workflow template rồi để AI lựa chọn và cấu hình.

## **3\. Workflow Canvas**

Cho người dùng chỉnh workflow bằng kéo thả hoặc mô tả ngôn ngữ tự nhiên tiếp.

MVP chỉ cần các node:

●       Agent hoặc LLM.  
●       End  
●       Knowledge Search.  
●       MCP  
●       Web search  
●       If/ else  
●       While  
●       Human Approval.

Canvas cần có thêm lưu, xóa, nối node, chỉnh thuộc tính và kiểm tra lỗi.

## **4\. Workflow Runtime**

Chuyển workflow trên canvas thành chương trình có thể chạy.

Bạn cần:

●       Định nghĩa Workflow JSON.  
●       Ánh xạ mỗi node với một hàm xử lý.  
●       Dùng LangGraph để chạy workflow.  
●       Quản lý state.  
●       Hỗ trợ condition.  
●       Hỗ trợ pause/resume cho Human Approval.  
●       Ghi lại execution trace.

## **5\. Các tool cốt lõi**

MVP chỉ cần làm tốt:

1. Knowledge Search từ PDF/DOCX.  
2. MCP: gồm các dịch vụ: Gmail, Drive, Outlook, Teams  
3. Web search

Mỗi tool phải có input, output, credential, quyền sử dụng, chế độ sandbox và mức độ rủi ro.

## **6\. Sandbox**

Cho phép người dùng chạy thử agent mà không ảnh hưởng dữ liệu thật.

Sandbox cần hiển thị:

●       Câu trả lời của agent.  
●       Node đã chạy.  
●       Tool đã gọi.  
●       Input/output của từng bước.  
●       Lỗi.  
●       Token, thời gian và chi phí ước tính.

Tool trong sandbox mặc định dùng **mock** hoặc **read-only**.

## **7\. Automatic Evaluation**

Hệ thống tự sinh test case từ specification và business rules.

Evaluation nên gồm:

●       Workflow có chạy thành công không.  
●       Agent có gọi đúng tool không.  
●       Có gọi tool bị cấm không.  
●       Có tuân thủ Human Approval không.  
●       Câu trả lời có đúng nguồn không.  
●       LLM chấm correctness, relevance và groundedness.

Không để LLM tự quyết định toàn bộ; phải kết hợp rule-based evaluation.

## **8\. Quản lý, Publish và triển khai Agent**

Cuối cùng cần:

●       Dashboard quản lý agent.  
●       Lưu nhiều version.  
●       So sánh hoặc rollback version.  
●       Publish agent thành một Deployment để end-user có thể sử dụng.  
●       Bật/tắt deployment.  
●       Lưu runtime log.

---

### **Cách publish cho end-user (MVP)**

Publish không nên chỉ tạo ra một API endpoint. Hệ thống nên tạo một Deployment đại diện cho phiên bản Agent đang được đưa vào sử dụng thực tế.  
●       Mỗi Deployment phải gắn cố định với một Agent Version cụ thể để các chỉnh sửa ở bản draft không làm thay đổi Agent đang chạy.  
●       MVP nên hỗ trợ hai cách sử dụng: Web Chat URL cho người dùng cuối và API endpoint cho hệ thống khác tích hợp.  
●       Không expose trực tiếp LangGraph/Langflow hoặc credential của tool cho end-user; mọi request phải đi qua backend/runtime gateway của hệ thống.

### **Cấu hình Deployment**

●       deployment\_id và agent\_version\_id.  
●       Trạng thái: active hoặc disabled.  
●       Quyền truy cập: private, organization/selected users; public link có thể để ngoài phạm vi MVP.  
●       Cơ chế xác thực: đăng nhập/JWT cho Web Chat và API key cho API.  
●       Rate limit để giới hạn số request và kiểm soát chi phí.  
●       created\_by, created\_at và runtime log để phục vụ audit.

### **Luồng end-user sử dụng Agent**

●       Web Chat: User → Published URL → Authentication → Permission Check → Load Deployment/Agent Version → Tạo hoặc tải Session → Workflow Runtime → Response.  
●       API: Client System → API Gateway → API Key/Auth → Permission Check → Load Deployment/Agent Version → Workflow Runtime → Response.

### **Hỗ trợ nhiều người dùng**

●       Không cần tạo một bản Agent riêng cho mỗi người dùng. Nhiều người có thể dùng chung một Deployment, nhưng runtime phải tách dữ liệu theo user\_id và session\_id.  
●       Conversation, state và execution trace của người dùng này không được trộn với người dùng khác.  
●       Nếu có Workspace/Organization, dữ liệu nên tiếp tục được ràng buộc theo organization\_id hoặc tenant\_id.

### **Bảo mật khi Publish**

●       Xác thực người dùng trước khi chạy Agent đối với deployment không public.  
●       Kiểm tra quyền truy cập Agent và quyền gọi tool ở mỗi execution.  
●       Credential của Gmail, Drive, model API hoặc dịch vụ bên thứ ba chỉ lưu ở backend/secret store và không gửi xuống trình duyệt.  
●       Nếu tool sử dụng dữ liệu cá nhân của từng end-user, lưu credential/OAuth token theo user thay vì dùng chung credential của người tạo Agent.  
●       Áp dụng rate limit, timeout và giới hạn chi phí/token cho mỗi deployment hoặc user.  
●       Ghi audit log gồm user, agent version, tool đã gọi, thời gian, trạng thái và lỗi.

### **Human Approval trong môi trường đã Publish**

●       Khi workflow đến Human Approval, execution phải pause và lưu state.  
●       Hệ thống tạo approval request cho đúng người hoặc role có quyền phê duyệt.  
●       Chỉ khi approval hợp lệ thì runtime mới resume đúng execution đang chờ; reject thì đi theo nhánh từ chối.

### **Quản lý sau khi Publish**

●       Bật/tắt Deployment mà không xóa Agent.  
●       Publish Agent Version mới thành Deployment mới hoặc cập nhật Deployment hiện tại theo cơ chế có kiểm soát.  
●       Rollback nhanh về Agent Version trước nếu bản mới có lỗi.  
●       Dashboard hiển thị số lượt chạy, lỗi, token/chi phí và các execution gần nhất.  
Phạm vi đồ án: chỉ cần làm tốt Web Chat URL \+ API, quyền truy cập cơ bản, session isolation, runtime log và bật/tắt deployment. Embed widget, Slack/Teams bot, marketplace và public sharing nâng cao có thể để sau.

## **Năm cấu trúc phải thiết kế trước khi code**

1\. Agent Solution Specification  
2\. Workflow JSON  
3\. Node Definition  
4\. Tool Definition  
5\. Deployment Definition  
Deployment Definition tối thiểu cần mô tả: deployment\_id, agent\_version\_id, status, access\_mode, auth\_mode, allowed users/roles, rate\_limit và thông tin audit.  
   
Đây là phần quan trọng nhất. AI Solution Designer, canvas, LangGraph, sandbox, evaluation và publish đều dựa trên năm cấu trúc này.

# Luồng sản phẩm cuối cùng

Mô tả bài toán  
→ AI Solution Designer phỏng vấn  
→ Duyệt specification  
→ Sinh workflow  
→ Chỉnh bằng kéo thả  
→ Chạy sandbox  
→ Automatic evaluation  
→ Chỉnh sửa  
→ Publish  
→ Tạo Deployment gắn với Agent Version  
→ Chọn quyền truy cập và kênh sử dụng  
→ End-user sử dụng qua Web Chat hoặc API  
   
Với phạm vi đồ án, hãy ưu tiên hoàn thành xuyên suốt luồng này cho **hai use case demo: hỏi đáp tài liệu và thực hiện hành động có Human Approval**. Không cần làm multi-agent, marketplace, MCP hay hàng chục integration.

 

