"""The human-in-the-loop: what an admin reads before saying yes, and what they decide.

See issue #14 / ADR-0012. Three modules, split by the three different reasons
this area changes:

- `queue` — which versions are waiting on a person, and which are stuck.
- `dossier` — everything about one version, gathered so that deciding does not
  require going and finding things.
- `decisions` — approve, reject, waive the eval bar; each written down forever.

Nothing here re-derives a gate. `src.agents.versions` owns the state machine and
`src.evals.gate` owns the bar (ADR-0011 §7); this package calls into both and
turns what they say into something a person can read and act on. The moment a
rule is stated twice, one of the two statements starts being the wrong one.
"""
