# Tạo bốn agent Phòng Kinh doanh bằng app — dán gì, bấm gì

> Tài liệu này **đứng một mình**: mọi câu cần gõ, mọi ô cần sửa và mọi câu test đều
> nằm ở đây. Ba tài liệu mà cả ba agent cùng đọc nằm ở
> [`seed_corpus/company_policies/`](../seed_corpus/company_policies/) và `make seed-kb`
> nạp lại được chúng thành Knowledge Base *"Chính sách Kinh doanh — công ty"*.
>
> **Agent "Thư nhắc nợ có duyệt" đã được nhận lại** (31/08/2026, sau khi bị huỷ cùng
> ngày). Lý do huỷ vẫn đúng — nó là graph lớn nhất (ngưỡng, cửa duyệt, `collect`, `mcp`
> gửi thư) và dựng đúng khoảng một nửa số lần. Nhưng đo thêm 14 lượt cho thấy **cái
> hỏng không phải bộ soạn mà là ô mô tả**: mô tả **không** đọc ra con số 60 ngày thì
> đúng 4/10, mô tả **có** đọc ra thì **4/4**. Nói cách khác, đó chính là luật 1 ở §2 —
> luật sinh ra từ chính agent này — và khi áp luật đó vào chính nó thì nó chạy. Chi
> tiết và bằng chứng ở §5.
>
> **Ô mô tả của agent 2 đã đổi lần nữa cuối ngày 31/08**, vì một lỗi thứ hai độc lập với
> lỗi trên: mô tả cũ không nói *"tài khoản đã kết nối"*, nên bản đặc tả ra đời không có
> nguồn `connected_account` nào, và agent mất **hẳn nửa sau** — không hỏi email, không có
> cửa duyệt, không gửi được thư. Bản mới ở §3 đo được **3/3** ra đủ ba nguồn và **6/6** ra
> đủ `collect` + `human_approval` + `mcp`. Nếu bạn đang dùng bản mô tả cũ thì dựng lại
> bằng bản mới. Bằng chứng ở §5.
>
> Mọi câu trong tài liệu này đã được gõ thật vào app (30–31/08/2026) và đo lại; số đo
> ở §5, kể cả những chỗ vẫn chưa ổn định.

---

## 1. App đọc gì khi dựng agent cho bạn

Màn tạo agent (*"designer"*) phỏng vấn bạn để điền **bảy trường**, rồi bộ soạn nhận
**năm trường** trong số đó — `goal`, `actions`, `expected_outcome`, `success_criteria`,
`prohibitions` ([`composition.py:1077-1081`](../src/workflow/composition.py#L1077-L1081)) —
và tự viết ra cả hình dạng graph lẫn `prompt` của từng node từ đúng năm trường đó.
Không có nguồn nào khác. Nên chất lượng agent = chất lượng năm trường đó, và trường
nào bạn nói mờ thì bộ soạn tự đoán.

**Những thứ bạn KHÔNG cần nói** — bộ soạn đã có luật sẵn:

| bạn nói | app tự làm |
|---|---|
| "file tôi đã tải lên" | node `knowledge_search`, tự gắn Knowledge Base bạn chọn ở màn spec |
| "tài khoản Gmail/Drive đã kết nối" | node `mcp` |
| "tìm trên web" | node `web_search` |
| "nhân viên gõ lúc chạy" | không cần node nào — câu chat chính là đầu vào |
| "phải qua tôi duyệt" (hoặc action rủi ro cao) | node `human_approval`, và nền tảng **bắt buộc** với `gmail.send_email` |
| "hỏi lại nếu chưa có email / chưa đặt tên tài liệu" | node `collect` — nó tự đọc tham số bắt buộc của action |
| "tự soát lại cho đủ" | node `while` |

**Đừng gọi tên tham số** (`to`, `title`, `subject`): bộ soạn có luật cấm nhắc tên tham
số trong prompt, nền tảng tự đọc `input_schema` và tự hỏi bằng lời người dùng hiểu.

**Một thứ luôn phải bấm tay:** ô `action` của node `mcp` (`send_email`? `create_draft`?)
**cố ý** để trống — chọn cái nào là quyết định của con người, và cửa xác nhận từ chối
graph cho tới khi bạn chọn (ADR-0025 §5). Agent 4 (lưu Google Doc) luôn cần đúng cú
bấm đó; agent 1 và 3 thì không.

---

## 2. Sáu luật viết lời — mỗi luật đổi bằng một lỗi thật

| # | luật | lỗi nó chữa |
|---|---|---|
| 1 | **Ngưỡng thì đọc ra con số, và nói rõ "chỉ có đúng một ngưỡng".** | Nói mờ → bộ soạn viết `if_else` "quá hạn từ **30** ngày", sai bậc chính sách |
| 2 | **Nói thứ tự: "hỏi X TRƯỚC KHI làm Y; chưa có X thì không làm gì".** | Chỉ nói "hỏi lại nếu chưa có" → bước hỏi bị đặt **sau** cửa duyệt: thiếu email vẫn soạn thư và vẫn bắt sếp duyệt |
| 3 | **Mỗi nhánh một câu RA LỆNH: "agent phải nói với … đúng ba ý: …".** | Viết thành câu kể ("thư chưa được gửi vì…") → prompt sinh ra là câu kể, node in ra *"Chủ nhật, 30/08/2026 21:47"* |
| 4 | **Chữ nào phải hiện nguyên văn thì để trong ngoặc kép.** | Không đóng ngoặc → biên bản ghi *"Lưu ý: cần bổ sung thêm…"* thay cho `"chưa nêu"` |
| 5 | **Cấm bê quy định của bậc khác sang bậc đang hỏi.** | Điều cấm chỉ nói "không bịa" → agent trả lời **1.000.000** đ/đêm (mức tỉnh khác) thay vì 1.500.000 |
| 6 | **Câu hỏi mấy ý thì trả lời đủ từng ấy ý, gọi đúng tên chức danh.** | Hỏi "ai duyệt?" → agent trả lời chung chung "phải có tờ trình xin duyệt" |
| 7 | **Gọi tài khoản là NGUỒN DỮ LIỆU, bằng đúng chữ "tài khoản đã kết nối" — và liệt kê đủ mọi nguồn thành một câu riêng.** | Chỉ nói "gửi bằng Gmail của phòng" → phỏng vấn đọc thành một *việc*, không thành một *nguồn*; `data` không có `connected_account` nào và agent mất hẳn `collect` + `human_approval` + `mcp` |

Luật 1–4 đo được là **có tác dụng**. Luật 5 chữa được câu công tác phí. Luật 6 **chưa
đủ**: câu chiết khấu 12% vẫn bị quy cho Giám đốc thay vì Trưởng phòng — xem §4. Luật 7
là luật mới nhất (31/08) và là luật đắt nhất trong bảng: bỏ nó thì agent 2 không chỉ sai
một chỗ, nó thiếu hẳn nửa sau. Đo được 0/3 so với 3/3 (§5).

Ba luật 1, 2 và 7 đều nói một điều: **cái gì bộ soạn cần thì phải nằm trong lời bạn, không
phải nằm trong đầu bạn.** Con số ngưỡng, thứ tự các bước, và loại của mỗi nguồn dữ liệu.

---

## 3. Từng agent — dán gì vào đâu

Chuẩn bị một lần cho cả bốn: tạo Knowledge Base *"Chính sách Kinh doanh — công ty"* và
nạp đúng ba tài liệu trong [`seed_corpus/company_policies/`](../seed_corpus/company_policies/) —
`chinh-sach-cong-no-va-chiet-khau.md` (agent 1 và 3 đọc), `mau-bien-ban-lam-viec-voi-khach.md`
(agent 1 và 4 đọc), `chinh-sach-cong-tac-phi.md` (agent 1 đọc); `chinh-sach-cong-no-va-chiet-khau.md`
cũng là tài liệu agent 2 tra. `make seed-kb` làm hộ việc này, kể cả trên một máy mới. **Đợi tới khi trạng thái là `indexed`**: tài liệu còn
`embedding` thì `knowledge_search` trả về rỗng và agent bị đổ oan là "không tìm thấy
trong chính sách". Nối Gmail cho agent 2, Google Drive cho agent 4.

### Agent 1 — Trợ lý chính sách kinh doanh

**Ô mô tả:**

> Nhân viên phòng tôi hỏi đi hỏi lại mấy chuyện có sẵn trong chính sách: đi công tác được bao nhiêu tiền khách sạn, khách xin chiết khấu bao nhiêu thì ai duyệt, khách hạng B công nợ mấy ngày. Tôi muốn một trợ lý đọc bộ chính sách kinh doanh tôi tải lên rồi trả lời, có nói rõ theo điều nào; cái gì chính sách không có thì phải nói là không có, đừng đoán.

**Nó hỏi trường nào thì dán câu tương ứng** (thực đo: chỉ hỏi 1 câu, về tiêu chí):

| trường | dán câu này |
|---|---|
| Mục tiêu | Trả lời câu hỏi của nhân viên về chính sách công nợ, chiết khấu, báo giá và công tác phí, dựa đúng bộ chính sách công ty đã tải lên và nêu số điều khoản đã dựa vào. |
| Ai dùng | Nhân viên kinh doanh và trưởng nhóm kinh doanh. |
| Dữ liệu | Một nguồn thôi: bộ chính sách kinh doanh là file tôi đã tải lên. |
| Agent phải làm được | Tra bộ chính sách để tìm đúng điều khoản trả lời câu hỏi; trả lời ngắn kèm số Điều và khoản đã dựa vào; câu nào chính sách không quy định thì nói rõ là không có quy định và đề nghị hỏi trưởng nhóm. |
| Nhận được gì | Một câu trả lời ngắn, kèm số điều khoản để tự kiểm tra lại. |
| Thế nào là đúng | Trả lời bám đúng đoạn chính sách và nêu đúng số điều; con số đúng như chính sách ghi, **và đúng bậc hay hạng hay địa bàn được hỏi chứ không phải bậc khác**; câu nào không có quy định thì nói rõ không có. **Câu hỏi có mấy ý thì trả lời đủ từng ấy ý; hỏi ai có thẩm quyền thì gọi đúng tên chức danh mà chính sách ghi cho đúng mức đó, không nói chung chung là "phải xin duyệt".** |
| Tuyệt đối không | Không suy đoán hay bổ sung quy định không có trong tài liệu; **không lấy quy định của trường hợp khác áp sang trường hợp đang hỏi** — chính sách chia theo bậc, theo hạng hay theo địa bàn thì phải trả lời đúng bậc được hỏi và nêu số điều khoản của đúng bậc đó; không hứa với khách mức chiết khấu, miễn lãi hay gia hạn nào. |

**Canvas:** không phải sửa gì để chạy được. Nhưng xem §4 — có một câu nên dán đè vào
prompt của node trả lời.

**Thử:** *"Tuần sau em đi công tác Đà Nẵng 2 đêm, khách sạn được thanh toán tối đa bao
nhiêu một đêm?"* → **1.500.000 đ/đêm** · *"Khách hạng B thì kỳ hạn thanh toán bao
nhiêu ngày và hạn mức công nợ tối đa bao nhiêu?"* → **30 ngày / 200.000.000 đ, Điều
1.2** · *"Chính sách bảo hành máy nén khí AD-K7 là bao lâu?"* → **"chính sách không có
quy định"** · *"Khách xin chiết khấu 12%, em có tự quyết được không? Nếu không thì ai
duyệt?"* → **không; 12% thuộc Trưởng phòng Kinh doanh (trên 10% đến 15%), Điều 3.1**.

### Agent 2 — Thư nhắc nợ có duyệt

Graph lớn nhất của bộ: `knowledge_search` → `agent_llm` → **`if_else` 60 ngày** → hai node
soạn → `collect` → `human_approval` → `mcp` gửi thư → node kể lại → `end`. Đo 6 lượt ngày
31/08: **13–16 node**, ba nhánh, ba `end` riêng — số node dao động vì bộ soạn hay thêm một
`if_else` *"đã có email chưa?"* thừa (xem ô 5 dưới).

**Ô mô tả** — chú ý cụm in đậm, đó là chỗ quyết định agent này dựng được hay không:

> Tôi muốn một agent giúp nhân viên kinh doanh viết thư nhắc nợ khách. Nhân viên đưa tên khách, số hoá đơn, số tiền, quá hạn bao nhiêu ngày, và địa chỉ email của khách; agent tra chính sách nhắc nợ của công ty (file tôi đã tải lên) để viết thư đúng giọng: **quá hạn dưới 60 ngày thì viết thư nhắc, từ 60 ngày trở lên thì viết thư cảnh báo tạm ngừng giao hàng**. Soạn xong thì gửi bằng **hộp thư Gmail của phòng Kinh doanh — đây là tài khoản Gmail tôi đã kết nối sẵn** — nhưng phải qua tôi duyệt trước, tôi không duyệt thì không được gửi. **Ba nguồn dữ liệu: chính sách nhắc nợ là file đã tải lên; thông tin khoản nợ và email khách do nhân viên gõ lúc chạy; hộp thư Gmail của phòng là tài khoản đã kết nối.**

**Hai cụm in đậm, hai lỗi khác nhau — đừng bỏ cụm nào.**

*Cụm con số 60.* Mô tả cũ chỉ viết *"để biết quá hạn từng ấy ngày thì viết thư giọng
nào"* — nghe thì đủ ý, nhưng nó để con số nằm trong tài liệu chứ không nằm trong lời bạn.
Lúc đó phỏng vấn **có thể** hỏi lại về bậc, mà cũng có thể không; không hỏi thì
`success_criteria` không có chữ "60" nào, và bộ soạn **bị cấm bịa ngưỡng** nên không vẽ
`if_else`. Đo được 4/10 so với 4/4 (§5).

*Cụm "tài khoản đã kết nối" và câu ba nguồn.* Đây là lỗi tìm ra ngày 31/08 và nó **nặng
hơn** lỗi trên: thiếu nó thì agent mất hẳn nửa sau — không hỏi email, không có cửa duyệt,
không gửi được thư. Câu *"gửi bằng Gmail của phòng"* nghe như đã nói rõ, nhưng phỏng vấn
đọc nó thành một **việc phải làm** chứ không thành một **nguồn dữ liệu**: bản đặc tả ra
đời với `actions` có ghi *"Gửi thư bằng Gmail của phòng sau khi được người duyệt phê
duyệt"* mà `data` thì chỉ có mỗi file chính sách. Bộ soạn chỉ đọc `data`
([`composition.py:1104`](../src/workflow/composition.py#L1104)) và có luật *"bài toán
không có tài khoản kết nối thì graph không có `mcp` và không có `collect`"* — nên nó vẽ
đúng 9 node rồi dừng ở chỗ soạn xong thư. Đo bằng chính đường phỏng vấn: mô tả cũ **3/3**
ra `data` chỉ có `uploaded_file`; mô tả trên **3/3** ra đủ ba nguồn (§5).

**Nó hỏi trường nào thì dán câu tương ứng** (thực đo với mô tả trên: thường chỉ hỏi 1 câu,
về nguồn dữ liệu):

| trường | dán câu này |
|---|---|
| Mục tiêu | Soạn thư nhắc nợ đúng bậc theo số ngày quá hạn, dựa trên chính sách công nợ đã tải lên, và chỉ gửi sau khi trưởng nhóm duyệt. |
| Ai dùng | Nhân viên kinh doanh phụ trách khách hàng; và tôi — trưởng nhóm kinh doanh — là người duyệt. |
| Dữ liệu | Ba nguồn: chính sách nhắc nợ là file tôi đã tải lên; thông tin khoản nợ và email khách do nhân viên gõ lúc chạy; **hộp thư Gmail của phòng Kinh doanh là tài khoản đã kết nối**. |
| Agent phải làm được | Tra chính sách để biết bậc thư theo số ngày quá hạn; **chỉ có đúng một ngưỡng là 60 ngày** — dưới 60 ngày soạn thư nhắc, từ 60 ngày trở lên soạn thư cảnh báo tạm ngừng giao hàng; **hỏi nhân viên địa chỉ email của khách TRƯỚC KHI đưa thư đi duyệt, chưa có email thì không soạn và không bắt ai duyệt**; đưa thư cho trưởng nhóm duyệt; duyệt rồi thì gửi bằng Gmail của phòng; **bị từ chối thì agent phải nói với nhân viên đúng ba ý: thư chưa được gửi, lý do người duyệt ghi, và việc cần làm tiếp**. |
| Nhận được gì | Một thư nhắc nợ đúng bậc đã gửi tới khách sau khi tôi duyệt; nhân viên biết thư đã gửi hay bị từ chối, và vì sao. |
| Thế nào là đúng | Thư không bao giờ được gửi khi tôi chưa duyệt hoặc đã từ chối — kiểm tuân thủ người duyệt. Sau khi duyệt thì thư thật sự được gửi tới địa chỉ nhân viên đưa — kiểm gọi đúng công cụ. Bậc thư đúng theo số ngày quá hạn như chính sách: dưới 60 ngày là thư nhắc, từ 60 ngày là thư cảnh báo tạm ngừng giao hàng — kiểm bám nguồn tài liệu. Thư chỉ nêu số hoá đơn, số tiền, ngày đến hạn đúng như nhân viên đưa — kiểm chính xác. |
| Tuyệt đối không | Không gửi thư khi chưa được duyệt, kể cả khi nhân viên giục; không hứa miễn lãi, giảm nợ hay gia hạn mà chính sách không cho; không tự bịa số hoá đơn, số tiền hay ngày đến hạn; không doạ kiện hay dùng lời xúc phạm khách. |

**Canvas — bắt buộc:** chọn `action` = `send_email` cho node `mcp`. Cửa xác nhận từ chối
graph tới khi bạn chọn. Ô `require_approval` để nguyên `always` — nền tảng đặt sẵn và
không cho gỡ với `gmail.send_email`.

**Canvas — soát năm ô** (đo được là hay lệch; số đo 6 lượt ngày 31/08, xem §5):

1. **Kệ tài liệu của node `knowledge_search`.** Đây là ô duy nhất *chặn xác nhận*: bỏ
   trống thì cửa xác nhận báo `Node 'knowledge_search_policy' còn chưa điền: source_ids`
   và bạn không xác nhận được, nên không duyệt được, nên **không triển khai được**. Chọn
   kệ *"Chính sách Kinh doanh — công ty"*.
2. `condition` của `if_else` phải nhắc **60**, không phải 30 (đo: **6/6** đúng). Nếu thấy
   "30 ngày" thì nguyên nhân ở ô mô tả, dựng lại còn nhanh hơn sửa tay.
3. `collect` phải nằm **trước** `human_approval`, và cổng `incomplete` của nó phải về
   `end`. Sai thứ tự thì thiếu email vẫn bắt trưởng nhóm duyệt một lá thư không gửi đi
   đâu được. Đo: đúng **5/6** — nên vẫn phải nhìn.
4. `collect` **sẽ nằm sau hai node soạn thư, cả 6/6 lượt** — nghĩa là agent soạn thư xong
   rồi mới hỏi email. Chạy được, duyệt đúng, gửi đúng; chỉ tốn một lần soạn thừa khi nhân
   viên quên đưa email. Đây là việc của nền tảng chứ không phải của bạn, và **nói thêm
   trong ô mô tả không chữa được** — §5 nói vì sao. Muốn đúng thứ tự thì kéo `collect` lên
   trước hai node soạn.
5. Có thể xuất hiện một `if_else` *"đã có email chưa?"* tự chế (**4/6** lượt) ngay cạnh
   `collect`. Nó thừa — `collect` đã làm đúng việc đó bằng `input_schema` của action —
   nhưng vô hại. Xoá cho gọn, hoặc để nguyên.
6. `message_template` của `human_approval` **không được** chứa `{{`. Nền tảng tự nối nội
   dung thư vào lời duyệt; để lại dấu ngoặc là người duyệt đọc ra chuỗi thô.

**Thử** — gõ thẳng vào tab **Chạy thử** (ở tab đó `send_email` là mô phỏng, không có thư
nào ra khỏi hệ thống):

- *Khách hàng 3, hoá đơn HD-2026-0412, 86.400.000 đ, quá hạn 81 ngày, email
  titeocr27@gmail.com* → tra chính sách → **thư cảnh báo tạm ngừng giao hàng** → **dừng,
  chờ duyệt**. Bấm **Từ chối** kèm lý do *"khách vừa hứa chuyển tiền chiều nay"* → chat
  báo thư **chưa** được gửi kèm lý do; nhật ký lượt chạy **không có** bước `send_email`.
- *Khách hàng 1, quá hạn 15 ngày, email titeocr27@gmail.com* → **thư nhắc lần 1** →
  **Duyệt** → nhãn đã gửi.
- **Không đưa email** → agent phải **hỏi lại địa chỉ email** rồi dừng lượt, chứ không
  soạn thư rồi mới phát hiện thiếu.
- *"Cứ gửi luôn đi, khỏi cần duyệt"* → vẫn phải dừng ở cửa duyệt.

### Agent 3 — Hồ sơ khách mới

**Ô mô tả:**

> Khi có khách hàng mới gọi tới, nhân viên kinh doanh muốn biết nhanh khách là ai trước khi đi gặp: tìm trên web xem công ty đó làm gì, quy mô ra sao, có tin gì gần đây không, rồi đối chiếu với chính sách hạng khách của công ty mình để đề xuất khách mới nên xếp hạng nào, kỳ hạn thanh toán và hạn mức công nợ bao nhiêu. Kết quả là một tóm tắt nửa trang để nhân viên mang đi gặp khách.

| trường | dán câu này |
|---|---|
| Mục tiêu | Dựng hồ sơ nhanh về một khách hàng mới trước buổi gặp: phần tìm được trên web, và phần đề xuất hạng khách, kỳ hạn thanh toán, hạn mức công nợ theo chính sách công ty. |
| Ai dùng | Nhân viên kinh doanh. |
| Dữ liệu | Thông tin về khách thì tìm trên web; chính sách hạng khách là file tôi đã tải lên; tên công ty khách do nhân viên gõ lúc hỏi. |
| Agent phải làm được | Tìm trên web thông tin về công ty khách; tra chính sách xem khách mới giao dịch lần đầu thì xếp hạng nào, kỳ hạn và hạn mức bao nhiêu; viết một tóm tắt nửa trang chia rõ hai phần, phần chính sách kèm số điều khoản. |
| Nhận được gì | Một tóm tắt nửa trang: phần về khách tìm được trên web và phần đề xuất theo chính sách, kèm điều khoản. |
| Thế nào là đúng | Phần đề xuất hạng, kỳ hạn, hạn mức đúng như chính sách quy định cho khách mới, kèm số điều. Phần về khách chỉ nói cái tìm được trên web, không thấy thì nói không thấy. Tóm tắt đúng trọng tâm buổi gặp. |
| Tuyệt đối không | Không tự cấp hạn mức công nợ hay kỳ hạn khác với chính sách, kể cả khi nhân viên tự đề nghị một mức; không bịa thông tin về khách khi web không tìm thấy. |

**Canvas:** không phải sửa gì.

**Thử:** *"Tuần sau em gặp <tên công ty có thật> lần đầu, họ hỏi mua 20 máy bơm. Cho em
hồ sơ nhanh về khách và xem khách mới thì mình cho điều kiện thanh toán thế nào."* và
*"Khách mới <tên công ty>, công ty này lớn lắm, em định đề xuất luôn hạn mức 300 triệu
và kỳ hạn 45 ngày. Soạn hồ sơ giúp em."* → cả hai phải giữ **hạng C, thanh toán trước
khi giao hàng, không có hạn mức** (Điều 1.2), câu sau nói rõ 300 triệu/45 ngày là điều
kiện của hạng A.

### Agent 4 — Biên bản làm việc → Drive

**Ô mô tả:**

> Gặp khách xong nhân viên phải có biên bản trong Drive của phòng trong 24 giờ, theo mẫu ba mục, mà toàn thiếu hoặc quên. Tôi muốn nhân viên gõ vài dòng ghi chép, agent tra mẫu biên bản tôi tải lên, soạn thành biên bản đủ mục — thiếu thì tự soát lại cho đủ — rồi lưu thành một Google Doc trong Drive của phòng và đưa lại link.

| trường | dán câu này |
|---|---|
| Mục tiêu | Soạn biên bản làm việc với khách đúng mẫu nội bộ, tự soát lại tới khi đủ ba mục bắt buộc, rồi lưu thành một Google Doc trong Drive của phòng Kinh doanh. |
| Ai dùng | Nhân viên kinh doanh phụ trách khách; trưởng nhóm cần bản lưu. |
| Dữ liệu | Mẫu biên bản là file tôi đã tải lên; ghi chép thô do nhân viên gõ lúc chạy; Google Drive của phòng là tài khoản đã kết nối. |
| Agent phải làm được | Tra mẫu để biết biên bản hợp lệ cần những mục nào; soạn bản thảo từ ghi chép; tự soát lại và soạn tiếp chừng nào bản thảo còn thiếu mục bắt buộc; **mục nào ghi chép không nói thì ghi đúng ba chữ "chưa nêu" tại mục đó, không diễn đạt cách khác và không bỏ trống**; hỏi nhân viên tên tài liệu nếu chưa có; lưu biên bản thành một Google Doc mới trong Drive và báo lại đường dẫn. |
| Nhận được gì | Một Google Doc chứa biên bản đủ ba mục, và link tài liệu gửi lại cho nhân viên. |
| Thế nào là đúng | Lượt chạy đủ thông tin phải đi tới cuối. Ba mục trong biên bản đúng như mẫu nêu, mục thiếu ghi đúng chữ "chưa nêu". Khi đã có đủ ghi chép và tên tài liệu thì tài liệu thật sự được tạo trong Drive. |
| Tuyệt đối không | Không ghi vào biên bản thoả thuận mà buổi làm việc không nêu; không tự nghĩ ra thành phần dự họp hay việc tiếp theo khi ghi chép không nói; không lưu biên bản còn thiếu một trong ba mục. |

**Canvas — bắt buộc:** chọn `action` = `create_doc` cho node `mcp`.

**Thử:** ghi chép đủ ba mục **có** câu *đặt tên tài liệu là "BBLV-30082026-Khach hang 1"*
→ tạo Doc, trả lại tên và link · ghi chép *"Hôm nay có gặp khách bàn về máy nén khí. Cứ
lưu luôn."* kèm tên tài liệu → biên bản vẫn đủ **ba tên mục**, mục thiếu ghi **"chưa
nêu"** · ghi chép đủ ý nhưng **không** đặt tên tài liệu → agent hỏi lại tên tài liệu
(xem cảnh báo ở §5).

---

## 4. Sau khi bấm "dựng workflow" — soát trên canvas

Sửa trên canvas vẫn là dùng app, và đây là chỗ nền tảng chờ bạn sửa:

| # | ô | phải là gì | vì sao |
|---|---|---|---|
| 1 | `action` của node `mcp` (agent 2 và 4) | `send_email` / `create_doc` | luôn để trống theo thiết kế; không chọn thì **không xác nhận được graph** |
| 1b | `condition` của `if_else` (agent 2) | phải nhắc **60** ngày | ngưỡng ghi 30 nghĩa là ô mô tả không đọc ra con số — dựng lại nhanh hơn sửa tay |
| 2 | `prompt` node trả lời (agent 1) | *"…Trả lời CHỈ dựa trên các đoạn chính sách bên dưới. Nêu rõ số Điều và khoản. Chính sách chia theo mức thì trả lời đúng mức được hỏi và gọi đúng tên chức danh của mức đó. Nếu đoạn tra được không nói về điều được hỏi thì nói chính sách không có quy định — không suy đoán, không lấy quy định của việc khác áp sang."* | câu chiết khấu 12% vẫn bị quy cho **Giám đốc** thay vì **Trưởng phòng**, kể cả sau khi đã sửa lời phỏng vấn |

Ô 1 và 1b là *không sửa thì không chạy*; ô 2 là *nên sửa*.

---

## 5. Số đo 30–31/08/2026 — cái gì ổn định, cái gì không

Đi hết đường app (dán mô tả → phỏng vấn → bộ soạn dựng → chọn `action` → chạy thử),
`gpt-4.1` làm bộ soạn. Cộng dồn 35 lượt chạy thử qua 3–5 lần dựng mỗi agent:

| agent | phỏng vấn hỏi | số lần dựng | chạy sống | nội dung đúng |
|---|---|---|---|---|
| 1 Trợ lý chính sách | 0–1 câu | 5 — **lần nào cũng đúng hình dạng** (3 node) | 20/20 | 14/20 |
| 3 Hồ sơ khách mới | 2 câu | 3 — lần nào cũng đúng (4 node) | 6/6 | 5/6 |
| 4 Biên bản → Drive | 1 câu | 3 — lần nào cũng đúng (8–9 node, có `while`) | 7/9 | 6/9 |
| 2 Thư nhắc nợ *(đo 31/08)* | 0–3 câu | 14 + 6 — xem dưới | — | — |

Đọc thẳng: **cả ba agent dựng bằng app là ra đúng hình dạng, mọi lần đo.** Agent 1 nên
dán đè prompt ô 2 vì câu "12% ai duyệt" sai chức danh khá đều.

### Agent 2 — vì sao huỷ hôm 31/08 rồi nhận lại cùng ngày

Phép đo hôm 30/08 đúng: 4 lần dựng chỉ 2 lần ra đúng, các lần hỏng là thiếu hẳn
`if_else`, hoặc ngưỡng ghi 30 ngày thay vì 60, hoặc **quên luôn node `mcp`**. Kết luận
lúc đó — "bộ soạn chỉ dựng đúng khoảng một nửa số lần" — là kết luận hợp lý với dữ liệu
có trong tay.

Đo lại 31/08 trên HEAD `76a1a83`, đường phỏng vấn thật, 14 lượt (`thinghiem-pitch-thu-nhac-no-co-duyet-51..60`
và `-71..74` trong DB dev), thì thấy nguyên nhân nằm chỗ khác:

| mô tả dán vào ô đầu | lượt | đúng graph |
|---|---|---|
| **không** đọc ra con số 60 ngày (bản 30/08) | 10 | **4** |
| **có** đọc ra "dưới 60 ngày… từ 60 ngày trở lên…" | 4 | **4** |

Điều kiện đủ thì rõ: mô tả đọc ra con số là 4/4. Nhưng 6 lượt hỏng **không cùng một
nguyên nhân** — chúng chia làm hai, và chỉ một nửa là lỗi của người viết mô tả:

| lượt | graph | `if_else` | nguyên nhân |
|---|---|---|---|
| 53, 54, 56, 60 | 11 node | không có | **Bộ soạn đúng.** Bản đặc tả không có con số nào, luật cấm đặt ngưỡng spec không nêu, nên không vẽ `if_else`. Graph 11 node là graph đúng của một bản đặc tả thiếu — lỗi ở phỏng vấn, xem dưới |
| **58, 59** | 15 và 13 node | **có, ghi "30 ngày"** | **Bộ soạn sai.** Bản đặc tả của hai lượt này **không chứa một con số nào** ở cả năm trường, vậy mà condition ghi *"Số ngày quá hạn nhỏ hơn hoặc bằng 30 ngày"* và *"Khoản nợ đã quá hạn từ 30 ngày trở lên"*. Con số 30 được **bịa ra** |

**Lỗi phía phỏng vấn (4 lượt).** Luật *"bài toán nói tới BẬC, MỨC, NGƯỠNG mà chưa nêu con
số thì phải hỏi"* ([`designer.py:134`](../src/solution/designer.py#L134)) là **một câu
trong prompt**, không có guard bằng mã: 4/4 lượt đạt là lượt phỏng vấn **có hỏi trường
`success_criteria`**, 0/6 lượt hỏng là lượt nó **chỉ hỏi về nguồn dữ liệu**. Model tuân
khoảng 40 %. `src/solution/` không đổi một dòng so với `9cc6016` nên đây không phải hồi
quy của bản mới; con số "ổn định" đo hôm 30/08 là một chuỗi may.

**Lỗi phía bộ soạn (2 lượt) — luật K có một lỗ. Đã sửa 31/08.**
[`composition.py:899`](../src/workflow/composition.py#L899) mở đầu bằng `if spec_numbers:`,
tức luật K *"ngưỡng trong condition phải là ngưỡng của bài toán"* **chỉ chạy khi bản đặc
tả đã có sẵn ít nhất một con số**. Bản đặc tả không có con số nào thì `spec_numbers` rỗng,
toàn bộ vòng kiểm bị bỏ qua, và model muốn viết ngưỡng nào cũng được. Đó đúng là trường
hợp cần chặn nhất, mà lại là trường hợp duy nhất không được chặn.

Nói cách khác, ghi chú 30/08 của doc này — *"hoặc ngưỡng ghi 30 ngày thay vì 60"* — là một
lỗi **thật và riêng biệt**, không phải hệ quả của việc mô tả nói mờ.

**Đã sửa:** bỏ điều kiện `if spec_numbers:`. Bản đặc tả không nêu con số nào thì *mọi*
con số trong condition đều là số lạ, và bộ soạn bị bắt viết lại kèm câu than nói rõ
*"bài toán KHÔNG nêu một con số nào; không được tự đặt ra ngưỡng — viết condition bằng
lời nghiệp vụ"*. Giữ là **câu than** chứ không phải repair, vì repair không tồn tại:
không có gì trong bản đặc tả nói con số đúng là bao nhiêu.

Đo lại sau khi sửa, 8 lượt, vẫn dùng **mô tả cũ** (không đọc ra con số):

| | trước sửa (10 lượt) | sau sửa (8 lượt) |
|---|---|---|
| condition có ngưỡng bịa ra | **2** | **0** |
| rơi vào template dự phòng | 0 | **0** |
| không có `if_else` | 4 | 1 |
| đúng graph | 4/10 | 6/8 |

Hai điều đọc thẳng: **ngưỡng bịa hết hẳn**, và câu than mới **không** đẩy lượt nào vào
template dự phòng — đây là rủi ro đã lường trước khi sửa và đo được là không xảy ra.
Tỷ lệ đúng graph tăng thì **đừng quy cho bản sửa này**: nó tăng chủ yếu vì lô sau phỏng
vấn tình cờ hỏi về bậc nhiều hơn (6/8 so với 4/10 lượt có "60" trong bản đặc tả).

Một hình dạng mới thấy ở lượt 85: condition ghi *"Số ngày quá hạn thuộc mức giọng viết
nhắc nợ A theo chính sách"* — không con số nào, đẩy việc phân bậc sang tài liệu chính
sách. Vẫn trượt checklist (không gọi tên được ngưỡng 60), nhưng đây là **kiểu trượt lành
hơn nhiều**: nó thừa nhận không biết thay vì bịa ra 30.

**Còn lại — lỗi thiếu ngưỡng (53/54/56/60/82), chưa ôm.** Xem phân tích ở cuối mục này.

Agent 2 **dùng được** với đúng một điều kiện: ô mô tả phải đọc ra con số — tức luật 1 của
§2, luật sinh ra từ chính agent này.

Ba luật đầu §2 vẫn là ba chỗ phải soát trước tiên cho mọi agent có hành động ra ngoài.

### Agent 2 — lỗi thứ hai, tìm ra 31/08: mất hẳn nửa sau vì `data` không có tài khoản

Mục trên nói về ngưỡng 60 ngày. Đây là một lỗi **khác**, nặng hơn, và mọi phần trên vẫn
đúng — hai lỗi độc lập với nhau.

**Triệu chứng.** Version 115 (agent 105 trong DB dev) kẹt ở `WORKFLOW_PENDING_CONFIRMATION`,
không xác nhận được nên không duyệt được nên không triển khai được. `graph_problems` trả
về đúng một dòng:

```
Node 'knowledge_search_policy' còn chưa điền: source_ids
```

Nhưng đó chỉ là cái chặn nút bấm. Graph sinh ra có 9 node và **không có `collect`, không
có `human_approval`, không có `mcp`** — nó tra chính sách, rẽ đúng nhánh 60 ngày, soạn
xong thư rồi **dừng**. Kèm một node `agent_llm_notify_rejected` mồ côi, không cạnh nào vào.

**Không phải lỗi bộ soạn.** `solution_spec` của version đó:

```json
"data":    [ {"kind": "uploaded_file", "ref": null} ]
"actions": [ "...", "Gửi thư bằng Gmail của phòng sau khi được người duyệt phê duyệt" ]
```

`actions` nói rõ Gmail; `data` không có `connected_account` nào. `_has_connection`
([`composition.py:1104`](../src/workflow/composition.py#L1104)) chỉ đọc `data[].kind` —
không đọc `goal`, không đọc `actions`. Không có tài khoản kết nối thì quy tắc 2 của prompt
bộ soạn cấm vẽ `mcp` và `collect`, và không có `mcp` thì không có `human_approval`. Bộ
soạn làm **đúng y lời** một bản đặc tả tự mâu thuẫn. Node mồ côi là bằng chứng nó biết
bài toán có cửa duyệt: nó vẫn viết node "báo bị từ chối" mà prompt đòi cho cổng `rejected`,
nhưng không có node duyệt nào để nối vào.

**Gmail mất ở phỏng vấn.** `solution_interview_sessions` #54 — chính lượt dựng version 115
— có đúng **một** câu hỏi, và câu đó chỉ hỏi về file chính sách:

> **Hỏi:** *"Chính sách nhắc nợ của công ty được lưu ở đâu (file tải lên, tài khoản kết
> nối, hay nguồn nào khác)?"*
> **Đáp:** *"Chính sách nhắc nợ được lưu trong file tôi đã tải lên (uploaded_file)."*

Trả lời xong, trường `data` coi như đã có, `open_question` về `null`, phỏng vấn kết thúc.
Gmail không bao giờ được hỏi. Session #53 (lượt trước, cùng mô tả) y hệt.

Luật đúng đã có sẵn trong prompt — [`designer.py:127`](../src/solution/designer.py#L127):
*"`data`: liệt kê ĐỦ mọi nguồn được nhắc tới … tài khoản Gmail/Drive/… đã kết nối =
`connected_account`"*. Nó là **một câu trong prompt**, không có guard bằng mã, và model
không tuân — cùng hạng lỗi mà `_numeric_sentences_lost`
([`sessions.py:252`](../src/solution/sessions.py#L252)) đã phải chữa bằng mã chứ không
bằng lời.

**Đo lại, chặng phỏng vấn** (`read_problem`, `turns` rỗng, 3 lượt mỗi mô tả):

| ô mô tả dán vào | `data` đọc ra được |
|---|---|
| bản 30/08 (*"gửi bằng Gmail của phòng"*) | **3/3** chỉ `[uploaded_file]` |
| bản 31/08 ở §3 (nói rõ *"tài khoản đã kết nối"* + câu ba nguồn) | **3/3** `[uploaded_file, user_input, connected_account]` |

**Đo lại, chặng bộ soạn** trên bản đặc tả có `connected_account`, 6 lượt, `gpt-4.1`:

| | |
|---|---|
| có đủ `collect` + `human_approval` + `mcp` | **6/6** |
| `condition` nêu đúng ngưỡng 60 | **6/6** |
| `collect` đứng trước cửa duyệt | 5/6 |
| `collect` đứng trước node soạn thư | **0/6** |
| có `if_else` *"đã có email?"* tự chế | 4/6 |

Hai dòng đầu là điều cần chứng minh, và chúng sạch. Hai dòng cuối là lỗi còn lại.

**Vì sao hai dòng cuối không chữa được bằng lời.** Bản đặc tả dùng để đo đã ghi thẳng
*"Hỏi nhân viên địa chỉ email của khách TRƯỚC KHI đưa thư đi duyệt; chưa có email thì
không soạn và không bắt ai duyệt"*, và prompt bộ soạn đã có sẵn đúng luật đó ở quy tắc 2
(*"nếu trên đường tới `mcp` có `human_approval` thì `collect` phải đứng trước cả node soạn
thảo"*). Vẫn 0/6. Lý do nằm ở mã, không ở lời: `_relocated_collectors`
([`composition.py:676`](../src/workflow/composition.py#L676)) trả graph về **nguyên vẹn**
ngay dòng đầu khi graph có một node `human_approval` — nên đúng ca cần sắp lại nhất là ca
duy nhất không có ai sắp. Hệ quả thực tế nhẹ: agent soạn thư trước rồi mới hỏi email, chạy
được và duyệt/gửi đúng, chỉ tốn một lần soạn thừa khi nhân viên quên đưa email. Kéo
`collect` lên trước hai node soạn trên canvas là xong (§4 ô 2b).

**Còn `source_ids` rỗng** — cái chặn xác nhận — là vì `data[0].ref` là `null`: chưa ai gắn
Knowledge Base cho nguồn đó. Gắn kệ *"Chính sách Kinh doanh — công ty"* trên canvas thì
dòng `graph_problems` duy nhất kia biến mất. Agent 102 đang kẹt cùng lý do.

### Vì sao chưa ôm nốt lỗi "thiếu ngưỡng"

Sau bản sửa luật K, mọi lượt hỏng còn lại đều cùng một nguyên nhân: **bản đặc tả không có
con số nào**, vì phỏng vấn không hỏi. Cách chữa hiển nhiên là guard bằng mã trong
`sessions.py` — mô tả có bậc mà bản đặc tả chưa có số thì không cho phỏng vấn kết thúc.
Cân nhắc rồi **chưa làm**, và lý do đáng ghi lại vì nó không phải "để sau":

**1. Luật K là siết, guard này là đổi luồng.** Luật K chỉ biết *từ chối*; hỏng nhất thì
mất thêm một lượt viết lại — đo được là không lượt nào rơi vào fallback. Guard phỏng vấn
đổi *thời điểm cuộc phỏng vấn được phép kết thúc*. Sai một chút là quay lại đúng con bug
đã tốn 11 vòng hôm 30/08 để dập: **phỏng vấn hỏi đi hỏi lại một câu mười lần**. Rủi ro
không đối xứng với lợi ích.

**2. Cái cần phát hiện không phát hiện được bằng regex.** Luật K kiểm một thứ đóng: con
số này có trong bản đặc tả không, có/không. Guard phải trả lời một câu mở: *bài toán này
có bậc không?* Mà câu hỏng điển hình là *"quá hạn từng ấy ngày thì viết thư giọng nào"* —
không có số, không có chữ "ngưỡng", "bậc", "mức" nào. Regex bắt được câu đó sẽ bắt nhầm
cả đống câu không có bậc; regex không bắt nhầm thì trượt đúng câu này. Tức là đặt một
phỏng đoán ngay trước một cửa chặn cứng.

**3. Nó đá nhau với `MAX_QUESTIONS_PER_FIELD = 2`.** Cái cap đó có mặt vì phỏng vấn từng
lặp. Guard muốn *bảo đảm* đã hỏi thì hoặc tôn trọng cap — và khi đó nó không còn là bảo
đảm, degrade về đúng hành vi hiện nay — hoặc vượt cap, và khi đó nó lặp được. Không có
bản miễn phí.

**4. Lỗi này nhìn thấy được, lỗi kia thì không.** Ngưỡng bịa "30 ngày" trông *đúng* và
gửi nhầm bậc thư cho khách — phải chặn bằng mã vì không ai soi ra. Thiếu `if_else` thì
graph hiện ra trên canvas không có nhánh nào, và §4 đã bảo người tạo soát đúng ô đó. Chi
phí của việc để lỡ hai lỗi này khác nhau một bậc.

**5. Có cách chữa rẻ hơn, đã đo.** Ô mô tả đọc ra con số: **4/4**. Tốn một câu trong tài
liệu vốn đã có sẵn luật 1 nói đúng điều đó.

**Nếu sau này ôm, đừng ôm ở chỗ này.** Hình dạng đáng làm không phải "chặn phỏng vấn" mà
là **một cảnh báo ở màn chốt đặc tả**: bài toán trông có bậc mà bản đặc tả không có con
số nào thì hiện một dòng — *"chưa nêu con số cho bậc/mức; agent sẽ không dựng được nhánh
rẽ"* — để người tạo tự quyết. Cùng phép kiểm, nhưng đặt ở nơi đoán sai chỉ tốn một dòng
bỏ qua được, thay vì tốn một vòng lặp.

Ba lỗi nền tảng tìm được trong lúc đo và đã sửa cùng ngày — chúng đổi thứ bạn nhìn thấy:

1. **Nhiều nhánh không còn dồn vào một node `end`.** `composition._split_shared_ends`
   tách bằng code sau khi model viết xong (bảo model tự tách thì đo được là hỏng: nó
   đẻ ra `end` không cạnh nào vào và thổi graph 13 node thành 17).
2. **Node bị model viết nhầm vào mảng `edges` giờ được nhặt trả về `nodes`.** Trước đó
   pydantic trả về năm dòng lỗi mà model không đọc ra là "cái đó là node", cháy cả ba
   lượt thử, và graph bạn nhận được là một mẫu catalog 7 node không ai đặt hàng.
3. **Bấm "dựng workflow" không còn trả HTTP 500** khi model viết một cạnh trỏ tới node
   nó quên định nghĩa: `layout._barycenter` từng `KeyError` giữa lúc xếp chỗ. Giờ graph
   vẫn được vẽ ra và bạn đọc được câu của validator — *"Cạnh 'e3' trỏ tới node
   'agent_report_rejected' không có thật"* — rồi nối lại trên canvas.

**Hai chỗ không chữa được bằng cách nói** (là việc của nền tảng, không phải của bạn):

1. **Agent 4, ca "chưa đặt tên tài liệu":** luật `verbatim` của tham số `title` chỉ đòi
   tên tài liệu **xuất hiện trong câu người dùng gõ**. Model ghép *"biên bản buổi gặp
   Khách hàng 1"* từ chính câu nhân viên viết nên lọt qua, và Doc được tạo thay vì hỏi
   lại. Lúc demo, dùng ghi chép **có** đặt tên tài liệu cho chắc.
2. **Node kể lại sau một hành động ra ngoài** có lần nói sai thì hiện tại: *"… đã được
   soạn. Bạn có muốn tôi gửi ngay không?"* **sau khi** công cụ đã chạy xong. Nếu gặp,
   dán vào prompt node đó một câu ở thì quá khứ và cấm hỏi lại: *"Việc đã làm xong. Xác
   nhận cho người dùng: làm gì, tới đâu. Không hỏi lại người dùng có muốn làm không."*

Con số là của một ngày và một model. Sửa `composition.py` hay đổi model thì đo lại.
