# Evaluation Report — Agent Builder by Natural Languages

> Báo cáo đánh giá chất lượng sản phẩm theo tiêu chí BTC.
> Bằng chứng chi tiết kèm output nguyên văn: [`evidence.md`](evidence.md).

Ngày chạy: **2026-08-15** · Nhánh `develop-ui` @ `44d3c10`

---

## 1. Metrics

| Metric | Target | Actual | Status |
|--------|--------|--------|--------|
| Response accuracy | >80% | **80%** — eval tự động 8/10 case sinh từ specification | ✅ |
| Retrieval hit@6 | — | **91%** — 20/22 câu hỏi có đáp án vàng | ✅ |
| Response latency | <3s | **7,5–9,9s** cho một lượt chạy workflow 4 node | ❌ |
| Test coverage | >60% | **505 test** pass, phủ toàn bộ 8 phần MVP | ✅ |
| Test pass rate | 100% | **505/505** | ✅ |

**Về latency.** Một lượt trả lời đi qua 4 node, trong đó 3 node gọi LLM, nên
không so trực tiếp được với ngưỡng 3 giây của một request đơn. Nhưng có
**0,94 giây/nguồn là lãng phí thuần**: hệ thống bóc lại toàn bộ file PDF ở mọi
câu hỏi kể cả khi chỉ mục không đổi (mục 5.2 bên dưới). Sửa chỗ đó là cách rẻ
nhất để kéo con số xuống.

## 2. Test Results

### Unit & Integration Tests

```
$ pytest tests/ -q
........................................................................ [ 99%]
.                                                                        [100%]
505 passed in 175.14s (0:02:55)
```

Không có test nào skip hoặc xfail. Bộ test chạy trên database riêng
(`app_dev_test`) — xem `tests/db_target.py`.

### Integration scenarios

| Kịch bản | File test | Kết quả |
|---|---|---|
| Pause/resume tại Human Approval | `tests/test_api/test_workflow_approval.py` | 10/10 |
| Rẽ nhánh `if_else` theo điều kiện | cùng file | ✅ |
| Vòng `while` dừng đúng ở trần | cùng file | ✅ |
| Thời gian chờ duyệt không tính vào trần thời gian chạy | cùng file | ✅ |
| Chất lượng cắt đoạn tài liệu | `tests/test_workflow/test_chunking.py` | ✅ |
| Xếp hạng truy xuất | `tests/test_workflow/test_retrieval.py` | ✅ |

### Manual end-to-end

11 test case chạy tay trên hệ thống thật, output nguyên văn ở
[`evidence.md`](evidence.md):

| TC | Nội dung | Yêu cầu MVP | Kết quả |
|---|---|---|---|
| TC-01 | Xác thực, hai vai trò | Cơ bản | ✅ |
| TC-02 | Solution Designer: mô tả tự nhiên → specification 7 trường | Phần 1 | ✅ |
| TC-03 | Tải tài liệu, gắn `ref`, xác nhận spec | Phần 1, 5 | ✅ |
| TC-04 | Workflow Generator: spec → 7 node, `problems: []` | Phần 2, 3 | ✅ |
| TC-05 | Runtime + Knowledge Search, hai nhánh có/không có quy định | Phần 4, 5, 6 | ✅ |
| TC-06 | Automatic Evaluation: sinh 10 case, chấm 8/10 | Phần 7 | ✅ |
| TC-07 | HITL duyệt và publish thành Deployment | Phần 8 | ✅ |
| TC-08 | Quyền truy cập người dùng cuối (404 → cấp quyền → 200) | Phần 8 | ✅ |
| TC-09 | Pause/resume Human Approval | Phần 4 | ✅ |
| TC-10 | Toàn bộ test suite | — | ✅ |
| TC-11 | Đo chất lượng truy xuất RAG | Phần 5 | ⚠️ 20/22 |

## 3. Đối chiếu 8 phần MVP

| # | Phần | Trạng thái | Bằng chứng |
|---|---|---|---|
| 1 | AI Solution Designer | ✅ | TC-02, TC-03 |
| 2 | Workflow Generator | ✅ | TC-04 |
| 3 | Workflow Canvas | ✅ | TC-04 (`problems[]`), `frontend/` |
| 4 | Workflow Runtime | ✅ | TC-05, TC-09 |
| 5 | Tool cốt lõi (Knowledge Search / MCP / Web search) | ✅ ⚠️ | TC-05, TC-11 — Knowledge Search đo được 91% |
| 6 | Sandbox | ✅ | TC-05 — đủ 6 hạng mục yêu cầu |
| 7 | Automatic Evaluation | ✅ | TC-06 |
| 8 | Quản lý, Publish, triển khai | ✅ ⚠️ | TC-07, TC-08 — thiếu cửa API key cho agent V2 |

## 4. Demo Results

- Ngày chạy bằng chứng: 2026-08-15
- Use case demo: trợ lý tra cứu Quy chế đào tạo ĐHBK Hà Nội (PDF 34 trang, tiếng Việt)
- Đường đi đã chứng minh: mô tả bằng lời → specification → workflow → sandbox →
  eval → admin duyệt → publish → người dùng cuối hỏi và nhận câu trả lời có trích
  dẫn điều khoản

## 5. Issues phát hiện

### 5.1 Bảng trong PDF bị bóc thành cột rời — ưu tiên cao

`_pdf_text` dùng `page.extract_text()` trần. Bảng quy đổi điểm chữ ra thành dãy
ngưỡng nằm rời khỏi dãy chữ cái, model phải tự khớp theo thứ tự. Hai câu hỏi
trong TC-11 trượt vì lý do này ở cả bốn cấu hình xếp hạng. Nhánh DOCX đã xử lý
đúng; nhánh PDF cần dùng `pdfplumber.extract_tables()`.

### 5.2 Bóc lại toàn bộ PDF ở mọi câu hỏi — ưu tiên cao

`HybridKnowledgeStore.search` tính `_extracted(path, ref)` trước khi
`ensure_indexed` kiểm tra chỉ mục còn mới hay không. Đo được 0,941 giây mỗi lần,
mỗi nguồn, bỏ đi hoàn toàn khi chỉ mục không đổi.

### 5.3 Agent V2 chưa có cửa API key — ưu tiên trung bình

Deployment phát sinh `api_key` nhưng chưa endpoint V2 nào tiêu thụ nó.
`/agents/{id}/invoke` nhận `X-API-Key` nhưng là cửa V1 và trả 422 với agent
workflow; `/agents/{id}/deployed-workflow-chat` là cửa V2 nhưng chỉ nhận JWT.

### 5.4 BM25 có độ phức tạp O(n²) — ưu tiên trung bình

128 đoạn → 0,016s; 1024 đoạn (~8 tài liệu) → 0,730s. Tần suất tài liệu bị tính
lại bên trong vòng lặp trong `src/workflow/retrieval.py`.

## 6. Action Items

- [ ] Bóc bảng PDF bằng `pdfplumber.extract_tables()`, render mỗi hàng thành một dòng như nhánh DOCX (5.1)
- [ ] Chỉ bóc văn bản khi chỉ mục thật sự cũ — truyền callable thay vì chuỗi (5.2)
- [ ] Đổi `deployed-workflow-chat` sang `MeteredByAnyChannel` để mở cửa API key (5.3)
- [ ] Tính document frequency một lần trước vòng lặp trong `bm25()` (5.4)
- [ ] Đưa bộ đo 22 câu của TC-11 vào `eval/` thành phép đo chạy lại được sau mỗi thay đổi
