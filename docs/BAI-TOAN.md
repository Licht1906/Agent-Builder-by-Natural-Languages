# Agent Builder by Natural Languages — Bài toán và pain point, viết lại cho rõ

> Tài liệu này không thêm thông tin mới. Nó **sửa khung diễn đạt** của thứ đã có
> trong `TOPIC.md`, `docs/specs/agent-builder.md`, `docs/usecase_spec/uc1-ar-assistant/spec.md`
> và `docs/presentation-prompt.md` — vì đọc bốn file đó xong vẫn khó hình dung sản
> phẩm bán gì cho ai.

---

## 1. Vì sao bản hiện tại khó hình dung

Bốn lỗi khung, không phải lỗi nội dung:

| # | Lỗi | Hệ quả khi đọc |
|---|---|---|
| 1 | **Hai tầng pain point đặt cạnh nhau mà không nối** — "tầng đề bài" (đội thiếu kỹ sư AI) và "tầng khách hàng" (P1–P4 công nợ) | Không biết bán cho ai: bán cho tổ CNTT cái máy dựng agent, hay bán cho phòng Kinh doanh cái trợ lý công nợ? |
| 2 | **Tầng đề bài không có số** — "mất nhiều tuần", "thiếu kỹ sư AI" | Não bám vào tầng có số (3–4 giờ, 20–30 request/tuần) rồi quên mất tầng kia mới là sản phẩm |
| 3 | **Sáu actor liệt kê ngang hàng** (`presentation-prompt.md`) | Đó là danh sách vai diễn, không phải một nhân vật chính. Không có ai để đồng cảm |
| 4 | **Tên sản phẩm mô tả cơ chế, không mô tả kết quả** — "AI Agent Builder" | Khách không mua "builder". Khách mua việc hết tắc |

---

## 2. Mảnh ghép còn thiếu: hai tầng pain point là **cùng một pain**

Đọc kỹ hai câu này, viết ở hai file khác nhau, cho hai đối tượng khác nhau:

> **Tầng đề bài** — `docs/specs/agent-builder.md`
> *"Muốn có agent, họ phải **xếp hàng** chờ đội nền tảng, và mỗi lần dựng một agent mới mất nhiều tuần."*

> **Tầng khách hàng** — `uc1-ar-assistant/spec.md` §2.2, pain point P2
> *"Nhân viên kinh doanh không có quyền query database, **phải nhờ tổ CNTT** — 20–30 request ad-hoc/tuần, mỗi request 30–60 phút của IT."*

Hai câu đó **không phải hai vấn đề**. Chúng là **một nút cổ chai duy nhất, nhìn ở hai độ phóng đại**:

```
                    NÚT CỔ CHAI: tổ CNTT 4 người

   Cấp CÂU HỎI                          Cấp CÔNG CỤ
   ───────────                          ───────────
   Sales muốn một con số                Phòng ban muốn một trợ lý
        ↓ phải nhờ IT                        ↓ phải nhờ kỹ sư AI
   30–60 phút IT × 20–30 lần/tuần       nhiều tuần × mỗi phòng ban
        ↓                                    ↓
   Sales chờ, hoặc thôi không hỏi       Nhu cầu tự động hoá dồn ứ
```

**Cùng một cơ chế đau:** người có nhu cầu không có quyền và không có kỹ năng, nên
mọi thứ phải đi qua một nhóm rất nhỏ người có cả hai. Nhóm đó ở UC-1 là **4 người**.

Và sản phẩm tấn công **cùng nút thắt đó ở cả hai cấp**:

| Cấp | Ai bị gỡ khỏi hàng đợi | Bằng cái gì |
|---|---|---|
| Câu hỏi | Sales tự hỏi, không cần IT | **Agent con** — mượn danh tính người hỏi (delegated authority), chỉ thấy dữ liệu trong `data_scope` của chính họ |
| Công cụ | Tổ CNTT tự dựng agent, không cần kỹ sư AI | **Agent mẹ (meta-agent)** — mô tả bằng tiếng Việt → `AgentSpec` → workflow chạy được |

> Đây là câu nối mà cả bốn tài liệu cũ đều thiếu. Có nó thì "vì sao lại là một
> *builder* chứ không phải một trợ lý công nợ" trả lời được trong một câu.

---

## 3. Nhân vật chính: **tổ trưởng CNTT**, không phải sales, không phải CFO

Bản cũ liệt kê 6 actor ngang hàng nên không ai nổi lên. Nhưng chỉ một người thoả
đồng thời ba điều kiện:

| Điều kiện | Ai thoả |
|---|---|
| **Người ĐAU** — chịu trực tiếp hậu quả | Tổ trưởng CNTT: 20–30 request/tuần đâm vào đội 4 người (P2); phải export Excel gửi sales rồi mất kiểm soát file (P4) |
| **Người QUYẾT** — có quyền làm gì đó | Tổ trưởng CNTT: là `agent_admin` — vai duy nhất được tạo, duyệt và deploy agent (C6, FR-30) |
| **Người ĐỀ XUẤT MUA** | Tổ trưởng CNTT đề xuất; Giám đốc Tài chính ký |

Sales là **người hưởng lợi**. CFO là **người ký**. Nhưng người vừa đau vừa có
quyền hành động là **tổ trưởng CNTT** — và đó là người sản phẩm phải nói chuyện.

*Hệ quả cho Day 26:* pain moment tôi chọn trước đó (7h30 sáng thứ Hai của trưởng
nhóm kinh doanh) là pain của **người hưởng lợi**. Pain của **người mua** nằm ở
mục 4 dưới đây và sắc hơn.

---

## 4. Pain moment — có giờ, có người, có màn hình

> **9h05 sáng thứ Hai.** Tổ trưởng CNTT mở hộp thư. Sáu yêu cầu trích số công nợ
> từ phòng Kinh doanh, gửi rải rác từ tối Chủ nhật. Mỗi cái 30–60 phút. Cộng lại
> hơn nửa ngày của một người trong đội bốn người — và hôm nay còn hai việc hạ tầng
> đã hứa.
>
> Anh ta biết cả sáu yêu cầu chỉ là **biến thể của cùng một câu query**.
> Anh ta cũng biết: dựng một công cụ tự phục vụ là hết chuyện. Nhưng đội anh
> **không có ai từng dựng agent**, và lần trước dựng dashboard BI thì đúng một
> tuần sau đã có câu hỏi ad-hoc nằm ngoài dashboard.
>
> Nên anh mở Excel, và bắt đầu làm yêu cầu thứ nhất.

Khoảnh khắc này chứa cả ba thứ: **P2** (hàng đợi), **lý do cách thường không giải
được** (BI chết ở ad-hoc), và **"thiếu kỹ sư AI"** — trong một cảnh.

---

## 5. Bốn lối thoát thông thường, và vì sao tắc cả bốn

Ba lối đầu có trong `spec.md` §2.3; lối thứ tư là cái ai cũng nghĩ tới nhưng tài
liệu cũ không nêu:

| Lối thoát | Giải được gì | Tắc ở đâu |
|---|---|---|
| **Cấp quyền ERP thẳng cho sales** | P2 | Vi phạm least privilege, và ERP không phân quyền theo dòng dữ liệu ở mức nhân viên → **C2 không thoả** |
| **Dựng dashboard BI** | P1 (báo cáo định kỳ) | Chết ở **P2** — câu hỏi ad-hoc luôn nằm ngoài dashboard đã dựng; và **không có hành động** nên không giải P3 |
| **Chatbot SaaS** | P1 + P2 | Dữ liệu công nợ rời khỏi doanh nghiệp → **C1 không thoả**, không qua được vòng duyệt |
| **Thuê ngoài viết một tool riêng** | P1 + P2 cho *một* phòng ban | Mất nhiều tuần, tốn tiền một lần; **tuần sau phòng Nhân sự cần cái khác** → quay lại đúng hàng đợi cũ |

Lối thứ tư quan trọng nhất, vì nó cho thấy **vì sao một sản phẩm bán sẵn không đủ**.

---

## 6. Vì sao bắt buộc phải là *builder* — và kinh tế học ép nó

Nếu bán **một agent công nợ bán sẵn**, ta giải P1–P4 cho **một** phòng ban rồi
dừng. Tuần sau phòng Nhân sự hỏi chính sách nghỉ phép, phòng Mua hàng hỏi đơn nào
chưa về — lại tắc, lại hàng đợi bốn người. **Cái đau không phải "thiếu trợ lý
công nợ". Cái đau là "mọi nhu cầu đều phải xếp hàng qua bốn người".**

Điều đáng chú ý: **mô hình tài chính Day 25 tìm ra đúng kết luận đó bằng một con
đường hoàn toàn khác** — không qua lập luận sản phẩm, chỉ qua cấu trúc chi phí:

| Số lượt/tháng trên một bản cài | Gross Margin | |
|---:|---:|---|
| 1.200 — chỉ agent đầu tiên | **48,9%** | 🟥 lỗ |
| 1.803 — ngưỡng hoà vốn | 60,0% | 🟨 |
| 3.600 — ba agent | 71,0% | 🟩 |

38% Cost/Job là chi phí **cố định trên mỗi bản cài** (node suy luận + hạ tầng), nên
**một bản cài chạy một agent duy nhất thì lỗ**. Mô hình chỉ lành mạnh **từ agent
thứ hai trở đi** (`Day25_pricing_model.xlsx`, `2_Pricing!B71`).

> **Sản phẩm học và kinh tế học nói cùng một câu.** Đó là dấu hiệu bài toán đã
> được đặt đúng: nếu tổ CNTT không tự dựng được agent thứ hai, thì sản phẩm vừa
> **không giải hết đau** vừa **không đủ tiền nuôi**. Một câu, hai lý do độc lập.

---

## 7. Nói lại trong một câu

**Bản đầy đủ:**

> Agent Builder by Natural Languages là một cái hộp đặt trong mạng nội bộ doanh nghiệp, để **tổ CNTT bốn người
> biến một yêu cầu nghiệp vụ mô tả bằng tiếng Việt thành một trợ lý chạy được
> trong vài ngày thay vì vài tuần** — mà không phải cấp quyền database cho ai,
> và không để một byte dữ liệu nào rời khỏi công ty.

**Bản ngắn:** *Trả lại cho tổ CNTT quyền nói "được".*

Nói theo người nghe:

| Người nghe | Câu mở |
|---|---|
| **Tổ trưởng CNTT** (người mua) | "Sáu yêu cầu trích số sáng thứ Hai — anh muốn chúng biến mất, hay muốn làm nhanh hơn?" |
| **Trưởng phòng Kinh doanh** (người hưởng lợi) | "Anh hỏi thẳng, có số ngay, chỉ thấy khách của mình, và không phải chờ IT." |
| **Giám đốc Tài chính** (người ký) | "81 giờ CNTT mỗi tháng, cộng phát hiện quá hạn sớm hơn 5–8 ngày. Chi phí bằng 2/3 lương một kỹ sư." |
| **Trưởng phòng bảo mật** (người chặn) | "Không byte nào rời hạ tầng. Agent mượn danh tính người hỏi, không có quyền riêng. Mọi email ra ngoài đều phải có người duyệt. Audit hash-chain." |

---

## 8. Cái chưa chứng minh được — nói ra thay vì giấu

Bốn khoảng trống, xếp theo mức độ chịu lực:

| # | Giả định | Trạng thái bằng chứng |
|---|---|---|
| 1 | **Tổ CNTT không phải kỹ sư AI tự dựng được agent thứ hai** | ⛔ **Chưa test với người thật lần nào.** Đây là giả định chịu lực của **cả sản phẩm lẫn mô hình kinh doanh** (mục 6). Toàn bộ bằng chứng hiện có là 5 test case do chính đội tự chạy (`docs/eval-manual-test-cases.md`) |
| 2 | "Dựng agent mất nhiều tuần" | ⚠️ Không có phép đo nào trong repo — không đo thời gian dựng thủ công, cũng không đo thời gian meta-agent dựng đầu-cuối cho khách thật |
| 3 | Chất lượng meta-agent | ⚠️ `docs/PM-PLAYBOOK.md`: *"chưa có một con số nào về chất lượng meta agent"*. Eval 80% (`eval/results/report.md`) là của **agent con** trên golden set 23 case của chính đội, không phải dữ liệu production |
| 4 | Judge tự động thay được người chấm | ⚠️ Spearman ρ = 0,59 với nhãn chuyên gia (n = 96) — chưa đủ tin, nên vẫn phải trả tiền cho QA người thật 6% số run |

**Việc đáng đo tiếp theo, và chỉ một việc:** đưa nền tảng cho một tổ CNTT thật,
bảo họ dựng agent thứ hai **một mình**, bấm giờ. Con số đó vừa là **TTFV** (đèn
bật trước của Day 26, `L-01`), vừa là thứ quyết định mô hình kinh doanh có đứng
được không. Nếu họ không dựng nổi, cả bài toán này phải viết lại.

---

## Nguồn

| Khẳng định | Ở đâu |
|---|---|
| "Xếp hàng chờ đội nền tảng", "mất nhiều tuần" | `docs/specs/agent-builder.md` |
| P1–P4, C1–C6, ba lối thoát tắc | `docs/usecase_spec/uc1-ar-assistant/spec.md` §2.2, §2.3, §2.4 |
| Quy mô: ~300 nhân sự, 18 + 3 kinh doanh, **tổ CNTT 4 người** | cùng file, §2.1 |
| `agent_admin` là vai duy nhất được deploy | cùng file, §3 và FR-30 |
| Delegated authority — agent con mượn danh tính người hỏi | cùng file, §3 và §8 |
| Kinh tế học: 38% chi phí cố định, ngưỡng 1.803 lượt | `Day25_pricing_model.xlsx` — `2_Pricing!B71`, khối `A55:E72` |
| Eval 80%, retrieval hit@6 91% | `eval/results/report.md` (2026-08-15) |
| ρ = 0,59, n = 96 | `tools/eval-calibration/results/summary.json` |
| "Chưa có con số nào về chất lượng meta agent" | `docs/PM-PLAYBOOK.md` §6 |
