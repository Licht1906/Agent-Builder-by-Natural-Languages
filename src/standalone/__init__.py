"""The exported package's runtime — DS-13 / ADR-0059 §10.

One agent, run inside a company's own infrastructure: `bundle.json` (the
confirmed spec and workflow, `src.agents.portability`'s format), one
`access.yaml` per Hệ CSDL, and `runtime.yaml` — where the company says which
Postgres, which LLM, which secrets backend and how askers sign in.

What it serves is chat, approvals and health, and nothing else: no Designer,
no Builder, no admin, no PM. It runs the platform's own V2 runtime unchanged —
the same `data_query` scope, the same DS-08 payload binding, the same DS-10
audit chain — against its own Postgres for checkpoints, runs and audit.

`config` is importable before any database exists (it is what decides which
database that is); `package` and `app` are not, and `__main__` imports them
only once `DATABASE_URL` points at the package's own Postgres.
"""
