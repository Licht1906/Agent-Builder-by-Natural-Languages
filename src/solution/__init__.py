"""The AI Solution Designer: a business description in, a confirmed spec out — see #42.

V2's first stage (ADR-0020). Four modules, in the order a creator meets them:

- `draft` — the half-filled spec the interview works on, and the one place that
  decides whether a field counts as answered.
- `designer` — the single model call: read everything said so far, fill what it
  can, flag what contradicts, ask one question.
- `sessions` — the turn loop and the row it commits after every turn, which is
  what makes an abandoned interview resumable.
- `specs` — the gate at the end: a finished interview becomes an `AgentVersion`
  at `spec_pending_confirmation`, and only the creator confirming makes it
  `spec_confirmed` and immutable.

Deliberately separate from V1's `src.interview`, which fills five fixed slots
against a deterministic stop (ADR-0014). ADR-0020 §3 chose the opposite trade for
V2 — no fixed question schedule — and folding the two together would mean one
module holding two contradictory answers to "when does the interview end".
"""
