"""The PM control room — `docs/PM-PLAYBOOK.md` §5 made runnable.

Three modules, three verbs. `metrics` **reads**: the six widgets the playbook
asks for first, each a group-by over tables the platform already writes
(`run_costs`, `run_approvals`, `quality_scores`, `meta_agent_gate_runs`).
`controls` **acts**: the handful of levers a PM pulls from that screen —
pause a deployment, decide a run waiting on a person, leave a thumbs verdict,
turn a bad run into a golden case. `gate` **measures**: runs the meta agent's
regression gate (playbook step 3) and keeps the answer.

The screen lives in the platform rather than in Langfuse, and the reason is
the verbs: a trace backend can show the approval queue but cannot approve
anything. Langfuse stays what `tracing.py` made it — an export that degrades
to nothing — and every number here is computed from the platform's own rows,
so the dashboard works on a deployment with no trace key at all.

`simulation` is the stand-in for runs that have not happened yet: a seed
script writes four weeks of plausible, clearly-marked rows so the widgets
and the levers can be exercised before the first real run, and `--reset`
takes them away again without touching anything real.
"""
