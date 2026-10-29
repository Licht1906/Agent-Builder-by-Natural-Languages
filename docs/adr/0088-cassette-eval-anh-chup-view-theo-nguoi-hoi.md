---
status: accepted
---

# Cassette eval: ảnh chụp từng view theo người hỏi, truy vấn chạy trong bộ nhớ

ADR-0069 §2–3 muốn eval đọc từ cassette (dữ liệu thật đã đóng băng) thay vì DB đang sống, để Baseline và Candidate
được chấm trên cùng dữ liệu, và để đáp án của case `data_query` do code tính chứ không do LLM đặt. ADR đó khoá
cassette theo `(action hoặc source, tham số chuẩn hoá, run_as)`. EV-02 làm phần này cho Hệ CSDL.

## Quyết định

1. **Cassette là ảnh chụp cả một view, trong phạm vi của người hỏi, không phải kết quả của một truy vấn.**
   - Khoá: `(case, Hệ CSDL, view)`, kèm `run_as`, `schema_hash` (hash danh sách cột của view) và `recorded_at`.
     Bảng `eval_cassettes`.
   - Lý do: lúc chạy eval, model tự viết truy vấn của nó. Khoá theo tham số chuẩn hoá thì chỉ cần model thêm một
     cột hay đổi `limit` là lệch khoá, và case thành `ungrounded`. Truy vấn có cấu trúc lại là hàm thuần của các
     dòng trong view, nên chụp cả view thì truy vấn nào cũng có đáp án.
   - Đây là cách hiểu cụ thể cho "tham số" của ADR-0069 §2 khi đọc Hệ CSDL: tham số là view, và phạm vi đã nằm
     sẵn trong các dòng.
2. **Ghi khi case được viết, bằng đúng reader thật.**
   - Ghi bằng `PlatformDatabaseReader` với `run_as` của case, đi qua đủ bốn cổng và áp phạm vi. Rows của nhân viên
     và của trưởng nhóm vì thế là hai cassette khác nhau.
   - Lần đọc bị từ chối (người hỏi không có quyền) được đóng băng thành chính lời từ chối. Lần đọc lỗi, hoặc view
     có quá `SNAPSHOT_MAX_ROWS` (5000) dòng trong phạm vi, được đóng băng thành lỗi.
   - Các cửa ghi: thêm case, sửa `run_as`, nạp file, nạp bộ đề mẫu, sinh case V2, và nút "Ghi lại dữ liệu đóng
     băng" (`POST …/eval-cassettes/record`).
3. **Eval chỉ đọc cassette.**
   - Mỗi case chạy với `CassetteReader` của nó. Truy vấn của model chạy trên các dòng đã chụp
     (`src.datasources.in_memory`), theo ngữ nghĩa của Postgres: NULL, `LIKE` theo text, aggregate, thứ tự NULL.
     Chuỗi được sắp theo bản xấp xỉ collation `en_US.utf8`; test đối chiếu từng truy vấn mẫu với Postgres thật.
   - Thiếu cassette, cassette đã cũ (cột của view đổi), Hệ CSDL không còn, hoặc cassette là lỗi: case vào
     `skipped[]` kèm lý do, **không** đọc live.
   - Case không có `run_as` không cần cassette: không ai hỏi thì mọi lần đọc đều bị từ chối, như khi chạy live.
4. **Đáp án do code tính: luật `answers_from_query`.**
   - Case mang một truy vấn có cấu trúc. Đáp án là kết quả của truy vấn đó trên cassette của case.
   - `must_contain` được tính ra từ kết quả: các giá trị aggregate; mọi giá trị nếu kết quả có tối đa 3 dòng; cột
     đầu của mỗi dòng (tối đa 10 dòng) nếu dài hơn.
   - Chấm bằng rule, so chuỗi có chuẩn hoá: bỏ dấu phân cách hàng nghìn, chấp nhận dấu thập phân phẩy. Không qua
     judge.
   - Đáp án rỗng hoặc toàn số 0 thì case không chạy, trừ case `expects_no_rows`: case đó đạt khi lần đọc của agent
     ra rỗng.
   - Bộ đề mẫu công nợ có thêm hai case "Khách nào đang quá hạn lâu nhất?", một cho nhân viên, một cho trưởng nhóm.
5. **Chạy thử vẫn đọc live** (`LIVE_READONLY`, ADR-0023 §4). Chỉ eval mới dùng cassette.

## Chưa làm trong EV-02

- **Cassette cho action đọc qua MCP** (Gmail, Drive…). Muốn ghi trước thì phải biết tham số, mà tham số do model
  điền lúc chạy, và hộp thư không có "view" nào để chụp. Eval vẫn gọi các action đọc đó live như trước. Cần một
  quyết định riêng: ghi ở lần chạy đầu, hay giả lập theo mẫu.
- **Viết `expected` + `must_contain` cho câu trả lời văn xuôi bằng `gpt-6-luna`**, và **loại case khi sinh**. Hai
  việc này thuộc generator của EV-03; EV-02 chỉ cung cấp công cụ (`trivial_answer`, `key_values`, `cassette_problem`).
  Hiện bộ đề nạp từ file hay bộ đề mẫu không bị loại âm thầm, mà bị bỏ qua lúc chạy kèm lý do.

## Considered Options

- **Khoá theo tham số chuẩn hoá của truy vấn**, đúng chữ ADR-0069 §2. Loại vì lý do ở mục 1.
- **Nạp ảnh chụp vào SQLite trong bộ nhớ rồi dùng lại bộ biên dịch SQL.** Loại: repo không có dialect SQLite, và
  collation của SQLite còn xa Postgres hơn bản xấp xỉ ở đây.
- **Cassette dùng chung cho mọi case cùng `(view, run_as)`.** Loại cho bản đầu: ticket gắn cassette với case, và
  bộ đề demo nhỏ. Gộp lại khi bộ đề ≥100 case làm dung lượng thành vấn đề.

## Consequences

- Cassette chứa dữ liệu thật của khách hàng. Nó thuộc về case và bị xoá cùng case, suite và agent, giống
  `evidence_cells`.
- Ghi lại cassette là dời baseline: lần chạy sau đo trên dữ liệu mới, nên delta so với lần cũ không nói gì về agent.
- Collation mô phỏng là `en_US.utf8`. DB của khách dùng collation khác thì có thể xếp khác ở những chỗ hoà. Nếu
  một case chấm theo thứ tự đó, nó sẽ lệch với live; khi đó cần ghi lại cassette, hoặc sửa truy vấn để không phụ
  thuộc vào chỗ hoà.
