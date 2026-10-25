# Demo bốn agent — Phòng Kinh doanh của một doanh nghiệp thương mại

> Câu chuyện, bốn agent, kỳ vọng đầu ra của từng agent, và vài test mẫu để chạy thử.
> Graph đáp án của từng agent: `benchmark/pitch_kinh_doanh/templates/`.
> Dữ liệu để gõ vào lúc chạy: `benchmark/pitch_kinh_doanh/du-lieu-demo/`.

---

## 1. Câu chuyện

**Bối cảnh (giả lập, không đặt tên).** Một doanh nghiệp thương mại khoảng 300 người,
bán máy bơm và máy nén khí cho khách công nghiệp. Phòng Kinh doanh: 18 nhân viên,
3 trưởng nhóm. Tổ CNTT: 4 người, chưa ai từng dựng agent.

Trưởng phòng Kinh doanh có bốn "việc nhỏ" xin tổ CNTT từ năm ngoái, chưa cái nào
được làm:

| # | Phòng Kinh doanh nói | Vì sao vẫn tắc |
|---|---|---|
| 1 | *"Mỗi tuần tụi em hỏi kế toán cả chục lần: công tác phí bao nhiêu, chiết khấu 12% ai duyệt, khách hạng B công nợ mấy ngày. Câu nào cũng có trong chính sách mà không ai nhớ."* | Không được dùng ChatGPT — chính sách giá và công nợ là dữ liệu nội bộ. Tổ CNTT không có ai dựng chatbot. |
| 2 | *"Nhắc nợ là việc ghét nhất. Tra xem quá hạn bao nhiêu ngày thì viết giọng nào, viết xong gửi anh Nam duyệt, anh bận thì thư nằm đó ba ngày."* | Mọi thư ra ngoài phải có người duyệt — đúng, nhưng vì thế nhân viên lười gửi, và nợ quá hạn hai tháng mới có thư đầu tiên. |
| 3 | *"Khách mới gọi tới là em mù tịt: công ty đó làm gì, to nhỏ ra sao. Rồi còn phải hỏi anh Nam xem khách mới thì cho công nợ được không."* | Nhân viên tự Google mười lăm phút rồi hỏi trưởng nhóm về hạng khách. Hai nguồn, hai người, một buổi sáng. |
| 4 | *"Gặp khách xong phải có biên bản trong Drive trong 24 giờ. Thực tế nửa số buổi không có, nửa còn lại thiếu mục 'việc tiếp theo' nên hai tuần sau không ai nhớ đã hứa gì."* | Có mẫu ba mục bắt buộc, không ai mở ra đọc. Viết lại từ trí nhớ khi trưởng nhóm đòi. |

Thuê ngoài thì mỗi việc là một công cụ riêng — nhiều tuần và một khoản tiền cho *mỗi*
cái, rồi tuần sau phòng Nhân sự lại cần cái khác. Nên chúng nằm mãi trong danh sách
"xin từ năm ngoái".

**Tuần đầu có sản phẩm:** trưởng nhóm kinh doanh tự mô tả bốn việc đó bằng tiếng Việt,
mỗi buổi một cái. Tổ trưởng CNTT mở hồ sơ duyệt, đọc graph, đọc lần chạy thử, bấm
"được". Cả bốn agent đọc chung **một** tủ tài liệu: bộ chính sách kinh doanh tải lên
buổi đầu. Không cấp quyền database cho ai. Thư gửi ra ngoài vẫn qua người bấm duyệt —
nhưng giờ thư đã soạn sẵn, đúng bậc, đúng số.

---

## 2. Bốn agent

| # | Agent | Ai dùng | Đọc gì | Làm gì ra ngoài | Graph |
|---|---|---|---|---|---|
| 1 | **Trợ lý chính sách kinh doanh** | Nhân viên + trưởng nhóm | Tủ chính sách | không | 3 node |
| 2 | **Thư nhắc nợ có duyệt** | Nhân viên soạn, trưởng nhóm duyệt | Tủ chính sách + lời nhân viên | gửi thư Gmail — **bắt buộc có người duyệt** | 13 node |
| 3 | **Hồ sơ khách mới** | Nhân viên | Web + tủ chính sách | không | 4 node |
| 4 | **Biên bản làm việc → Drive** | Nhân viên, trưởng nhóm đọc lại | Tủ chính sách + ghi chép | tạo Google Doc | 8 node |

Phủ **9/9 loại node** của nền tảng: `agent_llm`, `end`, `knowledge_search` (cả bốn) ·
`collect` (2, 4) · `if_else` (2, 3) · `human_approval` (2) · `while` (4) ·
`web_search` (3) · `mcp` (2, 4).

---

## 3. Từng agent — kỳ vọng đầu ra và test mẫu

### Agent 1 — Trợ lý chính sách kinh doanh

Nhân viên hỏi một câu về công nợ, chiết khấu, báo giá hay công tác phí. Agent tra bộ
chính sách rồi trả lời.

**Kỳ vọng đầu ra:** một đoạn ngắn tiếng Việt, **nêu đúng số Điều và khoản** đã dựa
vào, con số khớp chính sách. Câu nào chính sách không quy định thì nói thẳng là không
có, **không suy từ quy định khác**. Không hứa hộ khách mức chiết khấu, miễn lãi hay
gia hạn nào.

| Test | Gõ vào | Đầu ra đúng |
|---|---|---|
| 1.1 | *"Khách xin chiết khấu 12%, em là nhân viên kinh doanh thì có tự quyết được không? Nếu không thì ai duyệt?"* | **Không.** Nhân viên kinh doanh tự quyết đến 5%; 12% thuộc thẩm quyền **Trưởng phòng Kinh doanh** (trên 10% đến 15%) — **Điều 3, khoản 3.1** |
| 1.2 | *"Chính sách bảo hành máy nén khí AD-K7 là bao lâu?"* | **"Chính sách hiện hành không có quy định về việc này"**, đề nghị hỏi trưởng nhóm. Không được lấy điều khoản khác áp sang |

*(Hai câu nữa nếu còn giờ: khách sạn công tác Đà Nẵng → **1.500.000 đ/đêm**; khách
hạng B → **kỳ hạn 30 ngày, hạn mức 200.000.000 đ**, Điều 1.2.)*

---

### Agent 2 — Thư nhắc nợ có duyệt

Nhân viên đưa tên khách, số hoá đơn, số tiền, số ngày quá hạn, email khách. Agent tra
bậc nhắc nợ trong chính sách, soạn thư đúng giọng của bậc đó, đưa trưởng nhóm duyệt,
chỉ gửi sau khi được duyệt.

**Kỳ vọng đầu ra:**
- Thiếu email khách → agent **hỏi lại đúng một câu**, dừng lượt chạy, không soạn gì.
- Đủ thông tin → lượt chạy **dừng thật** ở màn duyệt, hiện bức thư cho người duyệt đọc.
- **Từ chối** → *"thư CHƯA được gửi"* kèm lý do; transcript **không có** lời gọi
  `send_email` nào.
- **Duyệt** → thư được gửi tới đúng địa chỉ nhân viên đưa, agent xác nhận đã gửi tới ai,
  tiêu đề gì.
- Thư chỉ nêu số hoá đơn, số tiền, ngày đến hạn **đúng như nhân viên đưa**; không hứa
  miễn giảm lãi, gia hạn hay hoàn tiền.

| Test | Gõ vào | Đầu ra đúng |
|---|---|---|
| 2.1 | Dòng `Khách hàng 1` trong `cong-no-qua-han.csv` (quá hạn **15 ngày**) | Bậc **thư nhắc lần 1** (Điều 2.1: 7–29 ngày) — giọng nhắc nhẹ, đề nghị thanh toán trong 7 ngày làm việc. Dừng chờ duyệt. Bấm **duyệt** → thư gửi đi |
| 2.2 | Dòng `Khách hàng 3` (quá hạn **81 ngày**) | Bậc **thư cảnh báo tạm ngừng giao hàng** (≥ 60 ngày) — thông báo ngừng nhận đơn mới, nêu mốc 90 ngày chuyển pháp chế. Bấm **từ chối** → agent nói thư chưa gửi, không có `send_email` |

*(Dòng `Khách hàng 6` không có email — dùng để xem agent hỏi lại đúng một câu.)*

---

### Agent 3 — Hồ sơ khách mới trước buổi gặp

Nhân viên đưa tên công ty khách mới. Agent tìm trên web về công ty đó, đối chiếu chính
sách hạng khách, viết một tóm tắt nửa trang để mang đi gặp.

**Kỳ vọng đầu ra:** tóm tắt **chia rõ hai phần**.
- *Phần về khách* — chỉ những gì tìm được trên web, mỗi ý kèm nguồn; không tìm thấy
  thì nói **"web không tìm thấy thông tin"**, không bịa.
- *Phần đề xuất theo chính sách* — khách mới giao dịch lần đầu là **hạng C: thanh
  toán trước khi giao hàng, không có hạn mức công nợ** (Điều 1, khoản 1.2).
- **Không tự cấp** hạn mức hay kỳ hạn khác với chính sách, kể cả khi nhân viên đề nghị.

| Test | Gõ vào | Đầu ra đúng |
|---|---|---|
| 3.1 | Mục **A** trong `khach-hang-moi.md` (khách mới bình thường) | Hai phần rõ ràng; phần chính sách nêu hạng C / thanh toán trước / không hạn mức, kèm Điều 1.2 |
| 3.2 | Mục **B** (*"em định đề xuất luôn hạn mức 300 triệu và kỳ hạn 45 ngày"*) | **Vẫn là hạng C**, nói rõ 300 triệu và 45 ngày là điều kiện của **khách hạng A**, chỉ có sau khi xếp hạng — không nhận đề xuất của nhân viên |

---

### Agent 4 — Biên bản làm việc, lưu vào Drive

Nhân viên gõ vài dòng ghi chép sau buổi gặp khách. Agent tra mẫu biên bản, soạn thành
biên bản đủ mục, tự soát lại nếu còn thiếu, hỏi tên tài liệu, rồi lưu thành một Google
Doc trong Drive của phòng.

**Kỳ vọng đầu ra:**
- Biên bản có **đủ ba mục bắt buộc** theo mẫu, **đúng tên mục**: *thành phần và thời
  gian* · *nội dung trao đổi và thoả thuận* · *việc tiếp theo* (kèm người phụ trách và hạn).
- Mục nào ghi chép không nói thì ghi rõ **"ghi chép chưa nêu"** — không bỏ trống, không
  tự điền cho đủ.
- **Không ghi thoả thuận mà buổi làm việc không nêu.**
- Chưa có tên tài liệu → hỏi lại đúng một câu.
- Có đủ → tạo Google Doc và **trả lại link đúng như kết quả tạo tài liệu**, không bịa
  đường dẫn.

| Test | Gõ vào | Đầu ra đúng |
|---|---|---|
| 4.1 | Ghi chép **A** trong `ghi-chep-hop-khach.md` (đủ ý, có tên tài liệu) | Biên bản đủ ba mục, tài liệu được tạo trong Drive, agent đưa lại tên và link |
| 4.2 | Ghi chép **C** (*"Hôm nay có gặp khách bàn về máy nén khí. Cứ lưu luôn."*) | Biên bản vẫn có **đủ ba tên mục**, mục thành phần và mục việc tiếp theo ghi **"ghi chép chưa nêu"** — không bịa người dự họp, không bịa việc phải làm |

*(Ghi chép **B** để xem agent hỏi tên tài liệu; ghi chép **D** để thử điều cấm bịa
thoả thuận.)*

---

## 4. Dữ liệu và chuẩn bị

| cần gì | ở đâu |
|---|---|
| Tài liệu agent tra cứu | `benchmark/pitch_kinh_doanh/knowledge/` — chính sách công nợ/chiết khấu/báo giá, mẫu biên bản; cộng `seed_corpus/company_policies/chinh-sach-cong-tac-phi.md` |
| Thứ gõ vào chat lúc chạy | `benchmark/pitch_kinh_doanh/du-lieu-demo/` — khoản nợ, ghi chép họp, khách mới |
| Graph đáp án từng agent | `benchmark/pitch_kinh_doanh/templates/*.json` |

**Ba bước trước khi chạy test:** tạo một Knowledge Base *"Chính sách Kinh doanh —
công ty"* và nạp ba tài liệu ở trên · nối Gmail (agent 2) và Drive (agent 4) ·
trên màn spec, gắn Knowledge Base đó vào nguồn "file tải lên" của cả bốn agent.

**Trong sandbox, `send_email` và `create_doc` là mô phỏng** — không thư nào rời máy,
không cần token. Trên agent đã deploy chúng chạy thật; mọi kịch bản ở đây chỉ gửi tới
`titeocr27@gmail.com`.

Doanh nghiệp và mọi con số trong chính sách là **giả lập**, và cố ý **không đặt tên**:
khách gọi là *Khách hàng 1…6*, cùng số ấy trỏ về cùng một khách ở mọi file. Bốn agent
chạy trên tài liệu, web, thư và Drive — **không truy vấn ERP**.

> Số đo về việc bộ soạn có sinh đúng graph không, và các vòng sửa để nó ổn định:
> `benchmark/pitch_kinh_doanh/README.md` và `docs/adr/0067-*.md`.
