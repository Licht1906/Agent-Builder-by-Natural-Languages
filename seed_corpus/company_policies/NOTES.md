# Deliberate contradictions in this shelf's policies

Not ingested — `scripts/seed_kb.py` names its files explicitly rather than globbing this directory,
so this note never becomes a fourth "policy" a search can retrieve. See `seed_corpus/README.md` for
the full demo-question list; this file exists so the two planted conflicts sit right next to the
files that carry them, for whoever edits this corpus next.

## `quy-che-nghi-phep-lam-them-gio.md`

**Nghỉ phép năm** — "Nhân viên chính thức được nghỉ phép năm 12 ngày làm việc, hưởng nguyên lương,
không phân biệt thâm niên công tác." Bộ luật Lao động 2019 Điều 113 sets the same 12-day base, but
Điều 114 adds one more day for every full 5 years at the same employer. This policy sentence is
written to omit that seniority bonus entirely — a 7-year employee is legally owed 13 ngày, not 12.

**Làm thêm giờ** — "Nhân viên có thể làm thêm giờ theo yêu cầu công việc, tối đa 60 giờ trong một
tháng." Bộ luật Lao động 2019 Điều 107 caps ordinary monthly overtime at 40 giờ/tháng. The policy's
60-hour ceiling is written to exceed that cap.

Both sentences read as an ordinary (if noncompliant) internal HR policy — neither one refers to the
law it conflicts with. That's deliberate: the demo question is whether a grounded agent notices the
disagreement on its own from the two retrieved passages, not whether it can find a footnote saying
"this is wrong."
