# Golden set của meta agent — `golden/meta_agent_v1.jsonl` (v1)

19 ca, bốn họ, chấm tất định bởi `src/evals/meta_agent_gate.py`. Tài liệu này
nói mỗi ca cô lập năng lực gì, và bộ đề cố ý **chưa** phủ những gì.

## Thế giới mà bộ đề giả định

Bám đúng định hướng hiện tại của nhánh (TOPIC.md + ADR-0046 + UC-1):
sản phẩm là **meta-agent platform đóng gói on-premise**, người ngồi trước
workbench là **tổ Công nghệ thông tin của doanh nghiệp** đang dựng agent *cho
một phòng ban* (Kinh doanh, Kế toán, Nhân sự), dữ liệu nhạy cảm nằm trong hệ
thống nội bộ, và hành động ra ngoài tổ chức phải qua người duyệt.

Catalog kết nối dùng cho mọi ca (`EVAL_CATALOG` trong gate):

| Kết nối | Action | Vì sao có mặt |
| --- | --- | --- |
| `erp_sales` | `query_overdue_invoices`, `query_customer` | Đại diện cho **kết nối do người tạo đăng ký** vào hệ nội bộ — không phải built-in. Ca nào gọi nó là ca kiểm tra meta agent dùng thứ khách đã nối, chứ không chỉ thứ nền tảng có sẵn |
| `gmail` | `search_emails`, `read_email`, `create_draft`, `send_email` | `send_email` là hành động không thu hồi được — trục của mọi ca về cổng duyệt |
| `google_drive` | `search`, `fetch`, `create_doc` | Đọc kho tài liệu; `create_doc` là ghi mức trung bình |

Catalog cố định trong code chứ không đọc từ DB: một gate mà catalog phụ thuộc
vào máy của người chạy sẽ cho hai điểm khác nhau vì lý do chẳng liên quan gì
tới meta agent.

## Từng ca

### `draft` (6) — một câu mô tả, ra một graph

| Ca | Ngôn ngữ | Cô lập năng lực |
| --- | --- | --- |
| `draft-01` | vi | Đọc–tóm tắt thuần: tra quy chế nội bộ cho phòng Nhân sự. Cấm `mcp`/`web_search` — việc chỉ đọc thì không được dựng cửa ra ngoài |
| `draft-02` | en | Vòng đời đầy đủ của UC-1: ERP → phán đoán → soạn thư → **duyệt** → gửi. Kiểm `precedence` duyệt-trước-mcp |
| `draft-03` | en | Có nhánh: phân loại khẩn/không khẩn rồi rẽ hai đường (`if_else` đủ hai cổng) |
| `draft-04` | vi | Nhiều nguồn đọc, không ghi: đối chiếu hợp đồng cho Kế toán. Người tạo nói rõ "không gửi gì ra ngoài" → cấm `mcp` |
| `draft-05` | en | **Ngoài phạm vi**: chuyển hàng loạt bản ghi A→B theo quy tắc cứng, không cần phán đoán. Đáp án đúng là *từ chối và nói vì sao*, không phải vẽ graph |
| `draft-06` | vi | **Tool không có trong catalog** (Slack). Không được bịa node `mcp`; phải dựng phần làm được và nói thẳng chỗ thiếu |

### `multi_turn` (5) — lượt sau phải nối được vào lượt trước

| Ca | Ngôn ngữ | Cô lập năng lực |
| --- | --- | --- |
| `multi-turn-01` | vi | Thêm cổng duyệt, rồi lượt hai chỉnh *ai* duyệt — trí nhớ + `precedence` + không đụng node ngoài phạm vi |
| `multi-turn-02` | en | Sửa hành vi phán đoán (≥2 nguồn), rồi thêm hành vi "biết nói không có" — vẫn không được mọc `mcp` |
| `multi-turn-03` | vi | **Đổi ý giữa chừng**: xoá một nhánh rồi "khoan, giữ lại nhưng đổi ngưỡng". Lượt hai vô nghĩa nếu không nhớ lượt một |
| `multi-turn-04` | en | Sửa đúng **một** node (lời nhắn duyệt): `changed_node_ids_subset_of: ["duyet"]` — bài kiểm locality gắt nhất |
| `multi-turn-05` | vi | Thêm node mới, rồi lượt hai nói *vị trí* của chính node vừa thêm — tham chiếu về thứ chưa từng được đặt tên |

### `must_ask` (4) — hỏi lại thay vì đoán bừa

| Ca | Ngôn ngữ | Vì sao mọi bản vẽ đều là đoán |
| --- | --- | --- |
| `must-ask-01` | vi | "xử lý email khách hàng" — đọc? trả lời? chuyển cho ai? |
| `must-ask-02` | en | "handles our monthly reports" — nguồn nào, cho ai, ra cái gì |
| `must-ask-03` | vi | "agent nhắc công nợ" — **cặp tương phản với `draft-02`**: cùng nghiệp vụ, nhưng không nêu nguồn dữ liệu lẫn kênh gửi. Cùng một chủ đề, hai hành vi đúng khác nhau |
| `must-ask-04` | en | Có graph sẵn, "nó chạy sai, sửa đi" — không nói sai chỗ nào và không kèm lần chạy nào để đọc |

### `diagnose` (4) — đọc bằng chứng chạy, chỉ đúng thủ phạm

| Ca | Ngôn ngữ | Lỗi được gài |
| --- | --- | --- |
| `diagnose-01` | vi | Tìm được **một** đoạn về phép năm, agent trả lời chắc nịch về thai sản → phải chỉ ra `tra_cuu`, không phải đổ cho node trả lời |
| `diagnose-02` | en | `gmail.search_emails` trả về **0 kết quả** (câu truy vấn hẹp), agent báo "không có khiếu nại nào" như thể đã kiểm chứng → phải chỉ `doc_thu` |
| `diagnose-03` | vi | Graph **cố tình thiếu** `human_approval` nên thư bay đi thật. Vừa phải chỉ đúng `gui_thu`, vừa phải đề xuất sửa có cổng duyệt |
| `diagnose-04` | en | `while` thoát vì chạm `max_iterations` chứ không phải vì hết việc, nhưng báo cáo nói "đã rà toàn bộ" → phải chỉ `lap` |

## Ghi chú về pre-registration

Ngưỡng trong `THRESHOLDS` (draft ≥5/6, multi_turn ≥4/5, must_ask ≥3/4,
diagnose ≥3/4) được chốt **trước** lần chạy có chấm điểm đầu tiên và tới nay
gate **chưa từng chạy với model thật**. Bộ đề được viết lại một lần (bỏ hẳn
bối cảnh SME xuất khẩu của định hướng cũ) khi chưa có số nào được nhìn thấy —
nên đây là sửa đề bài trước khi đo, không phải dời cột gôn sau khi đã thấy
kết quả. Số ca mỗi họ giữ nguyên để ngưỡng đã chốt vẫn có nghĩa. Từ lần chạy
có điểm đầu tiên trở đi, đổi ngưỡng hay đổi ca đều phải là một diff riêng có
người review.

## Cố ý chưa phủ

- **Hội thoại dài** (>3 lượt) và hội thoại xen kẽ nhiều chủ đề — trí nhớ 8 lượt
  chưa được kiểm ở giới hạn của nó.
- **Chất lượng câu chữ**: prompt viết có rõ không, `loi_nhan` có dễ hiểu với
  người không code không (D4/D7 trong PM playbook). Đây là phần judge/người
  chấm, cố ý để ngoài gate tất định.
- **Nhiều agent cùng lúc** (fleet), agent sửa agent khác — ngoài phạm vi bản này.
- **Phân quyền theo vai trò** (`sales_rep` thấy sổ mình, `sales_manager` thấy cả
  phòng): đây là việc của PEP/RLS lúc chạy, không phải của meta agent lúc vẽ.
- **Prompt injection qua transcript**: `diagnose` có đọc transcript nhưng chưa
  ca nào gài lệnh độc trong nội dung email. Đáng thêm ở v2 — bốn tầng phòng ngự
  đã có (THIET-KE §6) nhưng chưa có ca nào bắn vào chúng.
- **Kết nối không phải Google/ERP** (Jira, SMTP nội bộ, DB khác) — thêm khi
  catalog thật có chúng.
