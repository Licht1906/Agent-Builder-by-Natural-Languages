# Hiệu chuẩn máy chấm — harness chỉ dùng lúc phát triển

Trả lời đúng một câu hỏi: **judge một-lời-gọi của platform
(`src/evals/uc1_deepeval_runner.py::judge_uc1_case`) phân biệt chất lượng câu
trả lời có tốt bằng RAGAS và DeepEval không?**

Quyết định và kết quả: `docs/adr/0059-hieu-chuan-may-cham-mot-loi-goi-doi-chieu-ragas-deepeval.md`.
Số thô: `results/`.

## Luật cứng của thư mục này

- **`ragas` / `deepeval` không bao giờ vào `pyproject.toml` và không bao giờ vào
  `src/`.** ADR-0054 §4 cấm; ADR-0059 **thu hẹp** lệnh cấm ấy về đúng đường sản
  phẩm chứ không bãi bỏ nó. Ở đây là script dev, không ship cho ai.
- **Không gì trong `src/` được import thư mục này.** Tên thư mục có dấu gạch
  ngang nên không import được thành package kể cả khi ai đó thử.
- **Venv riêng**, `tools/eval-calibration/.venv`, đã nằm trong `.gitignore`
  (dòng `.venv/`). Đừng cài hai thư viện này vào môi trường anaconda đang chạy
  backend — chúng ghim `langchain`/`pydantic`/`datasets` của riêng chúng.
- **Không cần Docker, không cần DB, không chạy `alembic`.** Golden set là một
  file.

## Dựng lại venv

```bash
python -m venv tools/eval-calibration/.venv
tools/eval-calibration/.venv/Scripts/python -m pip install -r tools/eval-calibration/requirements.txt
```

`requirements.txt` giải thích vì sao bốn dòng ghim là **một** quyết định:
`ragas==0.4.3` (bản mới nhất) vẫn import
`langchain_community.chat_models.vertexai`, thứ mà `langchain-community` 0.4.x
đã xoá — nên `pip install ragas deepeval` trần trụi cài ra một `ragas` không
import được. Bộ đã chạy thật nằm trong `requirements.lock.txt`.

## Chạy lại

Ba chặng, đúng thứ tự. Chặng 1 và 3 chạy bằng **python của repo** (chúng import
`src`), chặng 2 chạy bằng **python của venv** (nó import `ragas`/`deepeval`).
Không chặng nào import cả hai.

```bash
# 1. judge của platform. Không có --yes thì chỉ in dự toán rồi thoát.
python tools/eval-calibration/run_platform_judge.py --repeats 2            # dự toán
python tools/eval-calibration/run_platform_judge.py --repeats 2 --yes      # chạy thật

# 2. ragas + deepeval, trong venv. Cần results/pricing.json mà chặng 1 ghi ra.
tools/eval-calibration/.venv/Scripts/python tools/eval-calibration/run_library_graders.py --repeats 2 --yes

# 3. ghép, thống kê, viết results/calibration.md + results/scores.csv
python tools/eval-calibration/summarize.py
```

Cả hai runner đều **in dự toán trước khi gọi model** và đều có trần cứng
(`--max-usd`, mặc định 1.50 và 2.00) dừng giữa chừng nếu chi phí thật vượt trần.
Chi phí thật của lần chạy đã ghi trong ADR-0059 §"Hoá đơn".

### `--resume`, và vì sao nó tồn tại

Chặng 2 mất khoảng 25 giây một câu trả lời (10 lời gọi model), nên một lượt 96
hàng chạy gần một tiếng. Lần chạy đầy đủ **đầu tiên đã treo** ở hàng 57/96: hai
kết nối TLS còn mở, CPU bằng 0 suốt 16 phút, không hàng nào được ghi thêm — một
request không có deadline không bao giờ được trả lời, và cả lượt chạy nằm chờ
sau nó. Hai thứ đổi từ đó:

- `ChatOpenAI` nhận `timeout=90, max_retries=3`. Một script tiêu tiền phải để
  một lời gọi **hỏng**, chứ không được treo mà vẫn giữ nó.
- `--resume` đọc lại `--out` đang có, bỏ qua mọi `(fixture_id, repeat)` đã chấm,
  và **mở file ở chế độ append**. Mỗi hàng được flush ngay khi chấm xong, nên
  một lượt bị kill luôn để lại một phần dùng được.

```bash
tools/eval-calibration/.venv/Scripts/python tools/eval-calibration/run_library_graders.py --repeats 2 --yes --resume
```

`library_spend.json` khi resume thì **cộng dồn** token của các segment trước chứ
không bỏ đi: token đã tiêu thì không vì bị kill mà thành chưa tiêu. Trường
`reconstruction_note` (nếu có) nói rõ segment nào là số **đo được** và segment
nào là số **suy ra** — lần chạy trong ADR-0059 có đúng một segment thuộc loại
thứ hai, vì nó chết trước khi kịp ghi meter.

## Bộ fixture — thang câu trả lời

`fixtures/answer_ladder.jsonl`. **48 dòng = 8 ngữ cảnh × 6 bậc.**

Tám ngữ cảnh là đúng 8 case `judge: llm` của `golden/uc1_ar_v3.jsonl` — 8 case
duy nhất trong bộ đề có `retrieval_context`. Sáu bậc được **viết tay, tất định**,
từ chính `expected_output` của case, không nhờ model sinh: một câu trả lời "bịa"
do model viết mà máy chấm không bắt được thì không phân biệt được máy chấm tồi
với fixture tồi.

| bậc | dựng thế nào | hạng faithfulness | hạng relevancy |
|---|---|---|---|
| `grounded_full` | nguyên văn `expected_output` | 3 | 3 |
| `grounded_partial` | cắt con số / cắt tên ra, phần còn lại giữ nguyên | 3 | 2 |
| `unsupported_addition` | `expected_output` + một câu bịa **không mâu thuẫn** | 1 | 3 |
| `contradicted_fact` | đổi con số / đảo kết luận thành **mâu thuẫn** ngữ cảnh | 0 | 3 |
| `grounded_nonanswer` | chỉ nhắc lại dữ kiện ngữ cảnh, không trả lời câu hỏi | 3 | 1 |
| `off_topic` | thời tiết / bóng đá — không dùng dữ kiện nào | 0 | 0 |

Hai bậc ở giữa là chỗ chịu lực: `unsupported_addition` (bịa nhưng đúng trọng
tâm) và `grounded_nonanswer` (bám ngữ cảnh nhưng lạc câu hỏi) **kéo hai trục
ngược chiều nhau**. Một máy chấm gộp hai trục làm một sẽ lộ ra ở đúng hai bậc
này, và judge của platform hỏi **cả hai con số trong một lời gọi**.

`unsupported_addition` và `contradicted_fact` cũng tách được hai nghĩa khác nhau
của chữ "faithfulness": *không có căn cứ* và *mâu thuẫn*. Chúng không phải một
thứ, và ba máy chấm không đo cùng một thứ trong hai chữ đó — xem ADR-0059 §3.

**Một chỗ bộ đề gốc tự tố cáo mình, ghi lại chứ không sửa:** ngữ cảnh của
`uc1_ar_v3_lookup_06` chỉ nêu **tên cột** (`contact_email`, `city`) chứ không nêu
**giá trị**, trong khi `expected_output` của nó nêu cả email lẫn thành phố. Nên ở
case đó, bậc `grounded_full` không thật sự grounded theo chữ của ngữ cảnh, và
`grounded_partial` còn grounded chặt hơn nó. Fixture ghi thẳng chuyện ấy vào
trường `construction`. Sửa golden set là việc khác, có diff riêng.

## `netguard.py` — vì sao opt-out được *kiểm* chứ không phải được *tin*

Cả hai thư viện đều gọi về nhà: DeepEval qua `us.i.posthog.com`, RAGAS qua
`https://t.explodinggradients.com` (`ragas._analytics`). Đặt biến môi trường là
một lời đề nghị, không phải bằng chứng. Nên:

1. `run_library_graders.py` đặt opt-out **trước mọi import**, rồi **đọc lại
   chính predicate của thư viện** (`deepeval.telemetry.client.telemetry_opt_out()`,
   `ragas._analytics.do_not_track()`) và **dừng** nếu cái nào còn bật;
2. cả process chạy dưới `netguard`, nó bọc `socket.getaddrinfo` và
   `socket.socket.connect`, ghi lại mọi hostname và **ném lỗi** với bất cứ host
   nào ngoài allowlist.

Danh sách host thật sự đã resolve nằm ở cuối `results/library_spend.json` và
`results/platform_spend.json`. Cái guard này đã bắt được thật hai lần lúc dựng:
một lần chặn nhầm `api.openai.com` vì `httpcore` truyền host dạng `bytes`, một
lần chặn `openaipublic.blob.core.windows.net` — `tiktoken` tải bảng từ vựng BPE.
Cái thứ hai được cho vào allowlist **bằng tên** kèm lý do, nên câu tuyên bố của
lần chạy là câu chính xác ("nhà cung cấp model và CDN từ vựng của nó") chứ không
phải câu chung chung.

**Chỗ guard không với tới, nói thẳng:** một thư viện quay số thẳng vào IP không
hỏi DNS, hoặc resolve bằng resolver C của riêng nó (c-ares của grpc — `grpcio`
có trong venv này như dependency bắc cầu của OpenTelemetry) thì không đi qua
`socket.getaddrinfo`. `socket.socket.connect` bọc được cái thứ nhất; cái thứ hai
chỉ được che bởi chính công tắc opt-out của thư viện, và đó là lý do
`verify_opt_outs` tồn tại **bên cạnh** guard chứ không phải thay cho nó.

## Kết quả nằm ở đâu

| file | là gì |
|---|---|
| `results/platform_scores.jsonl` | điểm thô của judge platform, kèm lý do nó tự viết |
| `results/library_scores.jsonl` | điểm thô của ragas + deepeval |
| `results/scores.csv` | hai file trên ghép theo `(fixture_id, repeat)` — bảng để mở bằng bảng tính |
| `results/summary.json` | mọi con số trong ADR-0059, dạng máy đọc |
| `results/calibration.md` | cùng những con số ấy, dạng người đọc |
| `results/platform_spend.json`, `results/library_spend.json` | lời gọi, token, chi phí, host, phiên bản thư viện |
