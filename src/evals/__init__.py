"""Eval — grading an agent against the criteria its own creator declared.

See issue #13 / ADR-0011. The test set is not written by us and not invented by
a model out of nothing: it is derived from `AgentSpec.slots.success_criteria`,
the answer the creator gave at interview time to *"thế nào là một câu trả lời
đúng"*. That is the thing the interview exists to collect, and no other step in
the platform can produce it.

Three modules, split by what would change them:

- `cases` — turning those criteria into editable test cases, and the editing
  itself. A generated case is a machine's reading of a human's sentence, so the
  creator gets to correct it *before* anything is graded.
- `judge` — deciding whether one answer met one criterion, and saying why.
- `runner` — executing the set in sandbox mode, scoring it, and comparing the
  score against the threshold an admin owns (#12).

Everything that talks to a model does so through the injected `ChatModelFactory`
seam, the **judge included**. That is what makes "toàn bộ verify bằng model giả
có kịch bản định trước, kể cả model chấm điểm" possible at all: a scorer reached
for directly would be a scorer no test could script.
"""
