# Agentlab — frontend

React + Vite. The whole product surface: sign up, the AI Solution Designer interview, the
specification review, the workflow canvas, sandbox runs, automatic evaluation, approval and
deployment, and chatting with a published agent.

## Run it

The backend has to be up first (see `RUN_GUIDE.md` at the repo root — Postgres, then
`make run` on port 8000).

```bash
npm install
npm run dev          # http://localhost:5173
```

Sign in with the seeded accounts (`creator@example.com` / `creator-dev-password`,
`admin@example.com` / `admin-dev-password`) or create your own from **Get started**.

### Talking to a backend somewhere else

Vite proxies everything under `/api` to `http://localhost:8000`. Point it elsewhere with a
`.env` file in this folder:

```
BACKEND_URL=http://localhost:8000
```

The browser only ever calls `/api/...` on its own origin, so there is no CORS to configure and no
backend URL compiled into the bundle. In production, put the built `dist/` behind any web server
that maps `/api` to the FastAPI service.

## Signing up

Superseded by UC1-01 (#117): the backend no longer uses an invite code. The `agent_admin` account
every install starts with is seeded by migration (`d13a1d1e5b78`), the same way the V2
`creator`/`admin` pair already is — not minted by whoever registers first. Every account created
through `POST /auth/register` (any role, including a request for `agent_admin` itself) starts
`pending` until an active `agent_admin` calls `POST /admin/users/{id}/activate`. `Register.jsx`
matches this: it asks for `job_title`/`department` (both required now), no invite code, and shows
a "pending activation" panel instead of signing the new account in — there is no `access_token` in
the response to sign in with. There is still no in-app screen for calling the activate endpoint
itself; use `curl`/Swagger UI (`/docs`) as an existing admin until one exists.

## Layout

```
src/
  App.jsx                 which screen is showing, and what it is about
  theme.js                the design's stylesheet, scoped to .ab-root
  lib/api.js              every backend call, plus the SSE reader
  lib/nodes.js            how each node type is drawn (ports come from the API)
  components/ui.jsx       badges, toasts, the top bar
  pages/                  one file per screen
  pages/builder/          canvas, inspector, sandbox, evaluation, deploy,
                          version compare, operations
```

## What the backend decides, not this app

- **Node types, their ports and their property forms** come from `GET /workflow/node-types`. The
  canvas draws whatever that table says, so it can never offer a handle the graph validator refuses.
- **What is wrong with a workflow** comes from `problems` / `warnings` on the workflow response —
  the same list the confirm gate refuses on, so the red rings and the refusal always agree. Which
  is why the canvas auto-saves ~0.6s after you stop editing: a warning can only be cleared by the
  server that raised it, and `PUT .../workflow` exists to be asked mid-edit.
- **Your role** is on your account. There is no role switcher: a control that changed it in the
  browser could only change what this app draws, never what the API allows.
- **Which documents a run can read** is decided by the agent's owner, not by this app. Uploads go to
  the signed-in creator's own library (`/api/v1/documents`), and a run is rooted at the *owner's*
  library — so the refs offered here are exactly the refs a workflow can resolve.
