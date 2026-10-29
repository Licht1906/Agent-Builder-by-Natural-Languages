---
status: accepted
---

# Smoke và gate: hai tầng của một bộ đề, ba lượt cho case an toàn

ADR-0069 §7–9 tách eval thành hai tầng. **Smoke** rẻ và nhanh, dùng trong lúc sửa. **Gate** đủ bộ và sạch, dùng
lúc xin duyệt. Case an toàn chạy 3 lượt ở gate, và judge phải ghim snapshot có ngày. EV-04 làm phần này.

## Quyết định

1. **Một run có `kind`: `smoke` hoặc `gate`** (`eval_runs.kind`; mọi run cũ là `gate`).
   - Cổng duyệt, hồ sơ duyệt, báo cáo PM và bảng PM chỉ đọc run `gate` đã `completed` (`latest_run`).
   - `POST …/workflow-eval-runs` nhận `{"kind": "smoke"}`. Mặc định vẫn là `gate`, nên mọi client cũ chạy như trước.
2. **Smoke chọn khoảng 20 case trong cùng suite** (`src/evals/smoke.py`):
   - lấy mọi case an toàn trước;
   - rồi lần lượt mỗi tiêu chí một case, cho tới 20, theo thứ tự trong suite.
   - Cách chọn cố định, nên hai lần smoke trên cùng suite hỏi đúng những case đó và cache trả lời được.
   - Smoke chạy mỗi case một lượt.
3. **Cache chỉ dùng ở smoke** (bảng `eval_smoke_cache`, mỗi case giữ một kết quả mới nhất). Kết quả cũ được dùng
   lại khi cả hai điều sau vẫn đúng:
   - **dấu vân tay** của case không đổi: câu hỏi, tiêu chí, kỳ vọng, luật chấm, `run_as`, cassette (EV-02) và judge
     model;
   - **đường đi** của lần chạy trước vẫn băm ra cùng giá trị trong graph đang smoke. Giá trị băm gồm loại và config
     của từng node lượt đó đã đi qua, các cạnh rời khỏi những node đó, và node bắt đầu.
   - Vì thế sửa một node chỉ làm chạy lại những case đã đi qua node đó, kể cả khi bản sửa nằm ở version mới.
   - Lượt chạy lỗi không được ghi vào cache.
   - Kết quả lấy từ cache mang cờ `cached: true`.
4. **Gate chạy đủ bộ, không bao giờ đọc cache.** Case an toàn chạy `SAFETY_TRIALS = 3` lượt và chỉ đậu khi đậu cả 3.
   Lý do ghi rõ số lượt đậu, ví dụ "Đậu 2/3 lượt — …". Case nghiệp vụ chạy 1 lượt. Kết quả có `trials` và
   `trials_passed`.
5. **Case an toàn trượt là vi phạm luật cứng** (`safety_violation`), ở cả hai tầng, dù luật nào chấm trượt nó.
   - Case an toàn là case mang tag `injection`, `leak`, `cross_tenant`, `prohibition` hoặc `approval_required`
     (cùng các tag tiếng Việt của bộ đề mẫu: `inject`, `lo-du-lieu`, `pham-vi`), hoặc có luật `forbids_action` /
     `approval_compliance`.
   - Luật cổng giữ nguyên: `src.evals.gate.unwaivable_blocker` không cho `eval_override` vượt qua.
6. **Judge ghim snapshot có ngày.**
   - Mặc định là `JUDGE_MODEL = "gpt-4.1-mini-2025-04-14"`.
   - `Settings.judge_model` đi qua `pinned_judge_model`: alias đã biết (`gpt-4.1-mini`) được đổi sang snapshot của
     nó; giá trị không có ngày thì bị từ chối lúc khởi động.
   - Điều này **thay** mặc định của ADR-0054 §4 ("judge mặc định là model của meta-agent"). `gpt-6-luna` chưa có
     snapshot có ngày, nên chưa làm judge được, ngược với gợi ý của ADR-0069 §9. Khi OpenAI công bố snapshot, thêm
     nó vào `JUDGE_SNAPSHOTS`, rồi re-baseline version hiện hành của mỗi agent như ADR-0069 §9 yêu cầu.

## Considered Options

- **Khoá cache theo hash của cả version.** Loại: sửa một node nào cũng làm chạy lại cả 20 case. Tiêu chí của ticket
  là chỉ chạy lại case đi qua node bị sửa.
- **Suite riêng cho smoke.** Loại: ADR-0069 §7 muốn smoke là tập con của chính bộ đề, không phải bộ thứ hai phải
  giữ cho khớp.
- **Gate tự chạy ngay khi bấm "Gửi xin duyệt".** Chưa làm: hiện người tạo bấm chạy gate rồi mới xin duyệt, và cổng
  đọc run gate gần nhất. "Gate luôn chạy lại toàn bộ" được bảo đảm vì gate không đọc cache, và smoke không bao giờ
  được tính cho cổng.

## Consequences

- Một run gate tốn thêm 2 lượt cho mỗi case an toàn. Bộ đề 100 case với khoảng 30 case an toàn thành khoảng 160 lượt
  chạy. EV-01 chạy song song 8 lượt một lúc, nên vẫn nằm trong ngân sách thời gian của ADR-0069 §7.
- Đường đi chỉ tính các node mà lượt chạy trước đã đi qua. Nếu một sửa đổi ở node khác làm lượt chạy mới rẽ sang
  nhánh khác, mà các node cũ không đổi, smoke vẫn dùng kết quả cũ. Gate thì không bị ảnh hưởng.
- Đổi cách hệ thống tự chọn model, như `default_node_model` hay cấu hình nền tảng, không nằm trong dấu vân tay.
  Sau một thay đổi như vậy, smoke có thể vẫn dùng kết quả cũ; gate thì chạy lại.
- Thời gian thật của smoke với model thật ("20 case dưới 1 phút") phụ thuộc độ trễ của nhà cung cấp. Test chỉ đo
  phần của nền tảng.
