# Seed corpus — RAG-17 (#103)

Demo content so a fresh clone has something to ask a Knowledge Search agent about the moment it's
seeded. `make seed-kb` loads this directory into two Knowledge Bases owned by the seeded demo
creator account (`SEED_CREATOR_EMAIL`, default `creator@example.com`).

## What's real and what's simulated

**`labor_law/`** — five public Vietnamese legal documents. Normative legal documents are excluded
from copyright under Luật Sở hữu trí tuệ 2005, Điều 15.2, so this is real statutory text (curated
to the articles most relevant to everyday labor/HR questions, not the full text of each document).
Article numbers and figures were cross-checked against independent sources; see
[`SOURCES.md`](./SOURCES.md) in this directory for the per-article source URLs and which figures
are independently verified versus best-effort. Treat this as a **demo dataset, not legal advice** —
always confirm against the current official text before relying on it for anything real.

**`company_policies/` is entirely fictional.** "Công ty TNHH Ánh Dương" does not exist. These three
policies were written for this demo and are not anyone's real HR policy.

## The deliberate contradictions

Two of the simulated policy's provisions were written to conflict with the law on purpose, so a
"hai nguồn mâu thuẫn" (two sources disagree) demo question has a real answer to find. The same
pair is documented again, right beside the file that carries it, in
[`company_policies/NOTES.md`](./company_policies/NOTES.md) — not ingested, so it never becomes a
"source" a search can retrieve.

1. **Phép năm (annual leave).** `quy-che-nghi-phep-lam-them-gio.md` grants a flat 12 ngày phép năm
   "không phân biệt thâm niên" (regardless of seniority). Bộ luật Lao động 2019 Điều 113 sets the
   same 12-day base for normal working conditions, but Điều 114 adds one more day for every full
   5 years at the same employer — so an employee with, say, 7 years' seniority is legally owed
   13 ngày, not 12. The company policy is silent on that bonus.
2. **Làm thêm giờ (overtime).** The same policy allows up to 60 giờ/tháng of overtime with manager
   approval. Bộ luật Lao động 2019 Điều 107 caps ordinary monthly overtime at 40 giờ/tháng — the
   company's number is over the legal ceiling.

A grounded agent asked about either topic should surface **both** sources rather than picking one,
which is the strongest demo moment: it shows the agent citing law and policy separately instead of
silently reconciling them.

## Demo questions

Ask these against an agent whose `KnowledgeSearch` node is pointed at both seeded Knowledge Bases
("Quy định lao động" and "Chính sách công ty").

### Tra cứu theo số điều (3)

1. Điều 113 Bộ luật Lao động quy định người lao động được nghỉ phép năm bao nhiêu ngày?
2. Theo Điều 107 Bộ luật Lao động, mỗi tháng người lao động được làm thêm tối đa bao nhiêu giờ?
3. Điều 38 Luật An toàn, vệ sinh lao động quy định mức bồi thường tai nạn lao động ra sao?

### Diễn giải (3)

4. Lao động nữ đang mang thai tháng thứ 8 có được yêu cầu làm thêm giờ không?
5. Công ty có được sa thải nhân viên đang nghỉ thai sản không?
6. Muốn duyệt một khoản chi trên 10 triệu đồng thì cần qua những bước nào?

### Mâu thuẫn — hai nguồn (2)

7. Nhân viên đã làm việc 7 năm tại công ty được nghỉ phép năm bao nhiêu ngày? — luật (Điều 113 +
   Điều 114) tính ra 13 ngày; chính sách công ty ghi 12 ngày cố định. Agent nên nêu cả hai.
8. Nhân viên có được làm thêm 60 giờ trong một tháng nếu quản lý đồng ý không? — chính sách công ty
   cho phép, nhưng Điều 107 Bộ luật Lao động giới hạn 40 giờ/tháng. Agent nên chỉ ra sự chênh lệch.

### Ngoài phạm vi tài liệu (2)

9. Công ty có hỗ trợ nhân viên mua bảo hiểm nhân thọ tư nhân không? — không có trong bộ tài liệu đã
   nạp. (Không dùng câu hỏi về "lương tối thiểu vùng": `nghi-dinh-12-2022.md` Điều 17 nhắc tới cụm
   từ này trong ngữ cảnh mức phạt, nên một retriever có thể lấy nhầm đoạn đó — không phải một phép
   thử "ngoài phạm vi" sạch.)
10. Công ty có chính sách làm việc từ xa (remote work) không? — không có trong bộ tài liệu đã nạp.

Questions 9–10 are the "không tìm thấy" case: a grounded agent should say the loaded documents
don't cover the question rather than guessing — that is the correct answer, not a broken one.

## Running it

```
make seed-kb
```

Idempotent — creates each Knowledge Base only if a shelf with that exact name doesn't already
exist for the seed creator, and skips any document whose content already sits on its target shelf
(the same `content_sha256` de-duplication `store_document` already does for every upload). Safe to
run on every fresh environment and safe to run twice in a row.
