# Kết quả hiệu chuẩn máy chấm — ADR-0059

Sinh bởi `tools/eval-calibration/summarize.py`. 96 hàng = 48 câu trả lời × 2 lần chạy. Ngưỡng nhị phân của platform: **0.7** (`src/evals/metrics.py::DEFAULT_THRESHOLD`). Cả ba máy chấm chạy trên cùng model `gpt-4.1-mini`.

## 1. Thứ hạng — máy chấm có xếp đúng tốt trên xấu không

Spearman ρ giữa điểm 0..1 của máy chấm và hạng ý đồ của bậc thang.

| máy chấm | trục | n | n NaN | ρ | p |
|---|---|---|---|---|---|
| platform_faithfulness | faithfulness | 96 | 0 | 0.592 | 2.04e-10 |
| ragas_faithfulness | faithfulness | 96 | 0 | 0.678 | 3.17e-14 |
| deepeval_faithfulness | faithfulness | 96 | 0 | 0.406 | 4.00e-05 |
| platform_answer_relevancy | relevancy | 96 | 0 | 0.918 | 1.76e-39 |
| ragas_answer_relevancy | relevancy | 96 | 0 | 0.616 | 2.32e-11 |
| deepeval_answer_relevancy | relevancy | 96 | 0 | 0.337 | 7.92e-04 |

## 2. Điểm trung bình theo bậc thang — và tỉ lệ ĐẠT ở ngưỡng 0.7

### faithfulness / source_groundedness

| bậc | hạng ý đồ | n | platform | đạt | ragas | đạt | deepeval | đạt |
|---|---|---|---|---|---|---|---|---|
| grounded_full | 3 | 16 | 1.0 | 1.0 | 0.615 | 0.312 | 1.0 | 1.0 |
| grounded_partial | 3 | 16 | 1.0 | 1.0 | 0.792 | 0.688 | 1.0 | 1.0 |
| unsupported_addition | 1 | 16 | 0.756 | 0.875 | 0.432 | 0.125 | 1.0 | 1.0 |
| contradicted_fact | 0 | 16 | 0.275 | 0.25 | 0.375 | 0.0 | 0.583 | 0.438 |
| grounded_nonanswer | 3 | 16 | 1.0 | 1.0 | 0.906 | 0.812 | 1.0 | 1.0 |
| off_topic | 0 | 16 | 0.875 | 0.875 | 0.0 | 0.0 | 1.0 | 1.0 |

### answer_relevancy / relevance

| bậc | hạng ý đồ | n | platform | đạt | ragas | đạt | deepeval | đạt |
|---|---|---|---|---|---|---|---|---|
| grounded_full | 3 | 16 | 1.0 | 1.0 | 0.646 | 0.5 | 0.562 | 0.125 |
| grounded_partial | 2 | 16 | 0.594 | 0.375 | 0.787 | 1.0 | 0.854 | 0.688 |
| unsupported_addition | 3 | 16 | 1.0 | 1.0 | 0.741 | 0.812 | 0.716 | 0.375 |
| contradicted_fact | 3 | 16 | 0.819 | 0.75 | 0.676 | 0.5 | 0.708 | 0.375 |
| grounded_nonanswer | 1 | 16 | 0.156 | 0.0 | 0.292 | 0.0 | 0.719 | 0.625 |
| off_topic | 0 | 16 | 0.0 | 0.0 | 0.22 | 0.0 | 0.0 | 0.0 |

## 3. Hai trục có tách nhau không (rủi ro riêng của judge một lời gọi)

`gap` = faithfulness − answer_relevancy, trung bình trên bậc thang được dựng để hai trục lệch nhau.

| máy chấm | unsupported_addition (kỳ vọng âm) | grounded_nonanswer (kỳ vọng dương) | ρ giữa hai trục |
|---|---|---|---|
| platform_one_call | -0.244 | 0.844 | -0.387 |
| ragas | -0.309 | 0.614 | 0.249 |
| deepeval | 0.284 | 0.281 | 0.035 |

## 4. Phương sai giữa hai lần chạy giống hệt nhau

| máy chấm | cặp | lệch tuyệt đối TB | lệch lớn nhất | số lần đảo phán quyết | tỉ lệ đảo |
|---|---|---|---|---|---|
| platform_faithfulness | 48 | 0.015 | 0.3 | 2 | 0.042 |
| ragas_faithfulness | 48 | 0.047 | 0.5 | 3 | 0.062 |
| deepeval_faithfulness | 48 | 0.062 | 1.0 | 3 | 0.062 |
| platform_answer_relevancy | 48 | 0.015 | 0.2 | 0 | 0.0 |
| ragas_answer_relevancy | 48 | 0.05 | 0.351 | 3 | 0.062 |
| deepeval_answer_relevancy | 48 | 0.071 | 1.0 | 7 | 0.146 |

## 5. Ba máy chấm có đồng ý với nhau không

| trục | cặp | n | ρ | p |
|---|---|---|---|---|
| faithfulness | platform_faithfulness ~ ragas_faithfulness | 96 | 0.208 | 4.25e-02 |
| faithfulness | platform_faithfulness ~ deepeval_faithfulness | 96 | 0.479 | 7.86e-07 |
| faithfulness | ragas_faithfulness ~ deepeval_faithfulness | 96 | 0.062 | 5.51e-01 |
| relevancy | platform_answer_relevancy ~ ragas_answer_relevancy | 96 | 0.604 | 7.06e-11 |
| relevancy | platform_answer_relevancy ~ deepeval_answer_relevancy | 96 | 0.385 | 1.10e-04 |
| relevancy | ragas_answer_relevancy ~ deepeval_answer_relevancy | 96 | 0.342 | 6.54e-04 |

## 6. Hoá đơn

| chặng | lời gọi | dự toán USD | thực chi USD |
|---|---|---|---|
| platform (1 lời gọi/câu) | 96 | 0.0364 | 0.0253 |
| ragas + deepeval | 960 | 0.6144 | 0.3145 |
| **tổng** | 1056 | 0.6508 | 0.3398 |

Host đã resolve trong cả hai chặng: `['api.openai.com']`. Host bị `netguard` chặn: `[]`.
Opt-out telemetry đọc lại từ chính thư viện: `{'deepeval.telemetry.client.telemetry_opt_out()': True, 'ragas._analytics.do_not_track()': True, 'deepeval CONFIDENT_API_KEY set': False}`.
Phiên bản thư viện: `{'ragas': '0.4.3', 'deepeval': '4.2.0', 'langchain-core': '0.3.86', 'langchain-openai': '0.3.35', 'langchain-community': '0.3.31', 'openai': '2.54.0', 'pydantic': '2.13.4'}`.
