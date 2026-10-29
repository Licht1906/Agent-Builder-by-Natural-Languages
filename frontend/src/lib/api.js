/* ============================================================
   The one place this app talks to the backend.

   Every call goes through `request` or `stream`, so the session token, the
   `/api/v1` prefix and the way a FastAPI error becomes a readable sentence are
   each written once. A component that built its own `fetch` would be a second
   opinion about all three — and the one that goes wrong is always the error
   message, which is the only part a user ever sees.
   ============================================================ */

const BASE = "/api/v1";
const TOKEN_KEY = "agentlab.token";

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export const getToken = () => localStorage.getItem(TOKEN_KEY) || null;
export const setToken = (token) => localStorage.setItem(TOKEN_KEY, token);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

export function parseJson(text) {
  try { return JSON.parse(text); } catch { return null; }
}

/* FastAPI says `detail`, and says it in two shapes: a sentence for the errors
   the code raises on purpose, and a list of field problems for a body pydantic
   refused. Both end up as one line, because a toast has one line. */
export function messageOf(payload, status) {
  const detail = payload && payload.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map((item) => {
        // Two list shapes reach here. Pydantic's own is `{loc, msg}`; UC-1's
        // spec gate (`src.agentspec.validation.Problem`) is `{code, path,
        // message}` — read as pydantic it produces the literal string
        // "undefined", which is how a field-level refusal ends up looking like
        // a frontend bug instead of an answer.
        if (typeof item.message === "string") return item.path ? `${item.path}: ${item.message}` : item.message;
        const field = Array.isArray(item.loc) ? item.loc.slice(1).join(".") : "";
        return field ? `${field}: ${item.msg}` : item.msg;
      })
      .join(" · ");
  }
  // A Knowledge Base delete refusal (RAG-13): `detail` is `{message,
  // affected_agents, blocking}` rather than a sentence, so the confirmation
  // dialog can read the list apart from the toast's one line.
  if (detail && typeof detail === "object" && typeof detail.message === "string") return detail.message;
  if (status === 401) return "Phiên đăng nhập đã hết hạn — hãy đăng nhập lại.";
  return `Yêu cầu thất bại (HTTP ${status})`;
}

function headersFor(hasBody) {
  const token = getToken();
  return {
    ...(hasBody ? { "Content-Type": "application/json" } : {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function send(path, { method = "GET", body, signal } = {}) {
  return fetch(`${BASE}${path}`, {
    method,
    headers: headersFor(body !== undefined),
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
}

/** One multipart upload.
 *
 * Deliberately not routed through `send`: a `FormData` body must go up without a
 * `Content-Type` header, because the browser is the only thing that knows the
 * boundary string it generated. Setting `multipart/form-data` by hand produces a
 * body the server cannot parse. */
export async function upload(path, file) {
  const token = getToken();
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  });
  const text = await response.text();
  const payload = text ? parseJson(text) : null;
  if (!response.ok) throw new ApiError(response.status, messageOf(payload, response.status), payload);
  return payload;
}

/** One plain-text GET — for the suite export and template downloads, whose
 *  bodies are CSV/JSONL a screen turns into a file, never JSON to parse. */
export async function requestText(path) {
  const response = await send(path);
  const text = await response.text();
  if (!response.ok) throw new ApiError(response.status, messageOf(parseJson(text), response.status), text);
  return text;
}

/** One binary GET — a file the screen saves as it came (DS-19's package zip).
 *  Resolves `{ blob, filename }`, the name read off `Content-Disposition`. */
export async function requestBlob(path) {
  const response = await send(path);
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, messageOf(parseJson(text), response.status), text);
  }
  const disposition = response.headers.get("content-disposition") || "";
  const named = /filename\*=UTF-8''([^;]+)/i.exec(disposition) || /filename="?([^";]+)"?/i.exec(disposition);
  return { blob: await response.blob(), filename: named ? decodeURIComponent(named[1]) : null };
}

/** One JSON request. Resolves with the parsed body, or throws `ApiError`. */
export async function request(path, options) {
  const response = await send(path, options);
  if (response.status === 204) return null;
  const text = await response.text();
  const payload = text ? parseJson(text) : null;
  if (!response.ok) throw new ApiError(response.status, messageOf(payload, response.status), payload);
  return payload;
}

/** One Server-Sent Events request: `onEvent(name, data)` per frame, in order.
 *
 * Refusals still arrive as an ordinary status — the backend decides everything
 * it can before opening the stream — so a failed run throws here rather than
 * delivering an `error` frame nobody was watching for. */
export async function stream(path, body, onEvent, { signal } = {}) {
  const response = await send(path, { method: "POST", body, signal });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, messageOf(parseJson(text), response.status), text);
  }
  await readEvents(response, onEvent);
}

/** An event-stream response, frame by frame: `onEvent(name, data)` in order.
 *  Shared with the exported package's chat (DS-17), which talks to its own
 *  runtime rather than `/api/v1` but streams the same event names. */
export async function readEvents(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let split;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      const lines = frame.split("\n");
      const nameLine = lines.find((line) => line.startsWith("event: "));
      const dataLine = lines.find((line) => line.startsWith("data: "));
      if (!nameLine || !dataLine) continue;
      onEvent(nameLine.slice(7), parseJson(dataLine.slice(6)) ?? {});
    }
  }
}

/* ============================================================
   The endpoints, grouped the way the screens use them.
   ============================================================ */

export const auth = {
  register: (payload) => request("/auth/register", { method: "POST", body: payload }),
  // UC1-02 (#118), FR-05: `/auth/ldap-bind` authenticates via the enterprise
  // directory when one is configured, and falls back to the exact same local
  // check `/auth/login` runs when it isn't — so this is the one login call the
  // sign-in screen ever needs to make, on-prem or off.
  login: (email, password) => request("/auth/ldap-bind", { method: "POST", body: { email, password } }),
  me: () => request("/auth/me"),
};

export const agents = {
  mine: () => request("/agents"),
  running: () => request("/agents/running"),
  read: (agentId) => request(`/agents/${agentId}`),
  rename: (agentId, name) => request(`/agents/${agentId}`, { method: "PATCH", body: { name } }),
  remove: (agentId) => request(`/agents/${agentId}`, { method: "DELETE" }),
  versions: (agentId) => request(`/agents/${agentId}/versions`),
  costs: (agentId) => request(`/agents/${agentId}/costs`),
  /** What two versions of one agent differ by, document by document (#55).
   *  `spec` and `workflow` are independently nullable — a version carries
   *  whichever documents its stage reached. */
  compare: (agentId, versionAId, versionBId) =>
    request(`/agents/${agentId}/versions/compare?version_a_id=${versionAId}&version_b_id=${versionBId}`),
  /** One version as a portable JSON bundle — the spec and the graph, stamped
   *  with the format they were written against. */
  exportVersion: (agentId, versionId) => request(`/agents/${agentId}/versions/${versionId}/export`),
  /** DS-19: whether this version may leave as a package (approved), and whether air-gap images can go with it. */
  packageOptions: (agentId, versionId) => request(`/agents/${agentId}/versions/${versionId}/package/options`),
  /** The package zip itself — `{ blob, filename }`. */
  exportPackage: (agentId, versionId, airgap) =>
    requestBlob(`/agents/${agentId}/versions/${versionId}/package?airgap=${airgap ? "true" : "false"}`),
  /** A bundle back into a new agent. Answers with the agent, the version it
   *  landed on, and every node whose model this deployment had to reassign —
   *  that last one is a sentence the creator has to read, not a detail. */
  importBundle: (bundle) => request("/agents/import", { method: "POST", body: bundle }),
  /** The whole set of Knowledge Bases this agent may search — a replace, not an
   *  add/remove (see `src.workflow.knowledge_bases.set_agent_knowledge_bases`).
   *  What makes a shelf's "agent nào đang dùng" column (RAG-13) true. */
  setKnowledgeBases: (agentId, knowledgeBaseIds) =>
    request(`/agents/${agentId}/knowledge-bases`, { method: "PUT", body: { knowledge_base_ids: knowledgeBaseIds } }),
};

/** A creator's shelves — what a `KnowledgeSearch` node's `source_ids` addresses,
 *  and what `DataSource.ref` (kind `uploaded_file`) points at on the spec. Never
 *  a single file any more (RAG-04/RAG-05, ADR-0043 §1) — a node reads a shelf,
 *  which a creator can add documents to without that being a workflow edit. */
export const knowledgeBases = {
  list: () => request("/knowledge-bases"),
  /** A new shelf in one of the two modes RAG-22 (#108) offers.
   *
   *  `externalConnectionId` is what makes it a BYODB shelf — the backend
   *  derives `mode` from it rather than trusting the two to agree, so this
   *  sends both and lets it refuse anything inconsistent. A managed shelf
   *  sends neither field, exactly as every caller written before BYODB did. */
  create: (name, description, externalConnectionId = null) =>
    request("/knowledge-bases", {
      method: "POST",
      body:
        externalConnectionId === null
          ? { name, description }
          : { name, description, mode: "byodb", external_connection_id: externalConnectionId },
    }),
  read: (kbId) => request(`/knowledge-bases/${kbId}`),
  rename: (kbId, name) => request(`/knowledge-bases/${kbId}`, { method: "PATCH", body: { name } }),
  /** `confirm=true` only after a first, unconfirmed call has come back with the
   *  409's `affected_agents` and the caller has decided to proceed anyway. */
  remove: (kbId, confirm = false) => request(`/knowledge-bases/${kbId}${confirm ? "?confirm=true" : ""}`, { method: "DELETE" }),
};

/** A creator's own vector databases — RAG-20 (#106), the thing a BYODB shelf
 *  points at. `test` is the only call here that makes the server dial out; it
 *  always answers 200 with a verdict on the row (`status`, `status_reason`),
 *  never an error status, so a failed connection is read off the body rather
 *  than caught. */
export const externalConnections = {
  list: () => request("/external-connections"),
  create: (body) => request("/external-connections", { method: "POST", body }),
  read: (id) => request(`/external-connections/${id}`),
  test: (id) => request(`/external-connections/${id}/test`, { method: "POST" }),
  remove: (id) => request(`/external-connections/${id}`, { method: "DELETE" }),
};

/** A creator's Hệ CSDL (DS-02). The DSN goes in on `create`/`update` and never
 *  comes back — a response says `has_dsn`, nothing more. */
export const databaseSources = {
  list: () => request("/database-sources"),
  create: (body) => request("/database-sources", { method: "POST", body }),
  update: (id, body) => request(`/database-sources/${id}`, { method: "PATCH", body }),
  test: (id) => request(`/database-sources/${id}/test`, { method: "POST" }),
  remove: (id) => request(`/database-sources/${id}`, { method: "DELETE" }),
  /* Quy tắc truy cập (DS-03): `rules: null` means none saved — deny everyone. */
  accessRules: (id) => request(`/database-sources/${id}/access-rules`),
  saveAccessRules: (id, rules) => request(`/database-sources/${id}/access-rules`, { method: "PUT", body: rules }),
  /** DS-12: rules drafted from the creator's words — `{rules, dropped, problems}`, never saved. */
  draftAccessRules: (id, description, identity) =>
    request(`/database-sources/${id}/access-rules/draft`, { method: "POST", body: { description, identity } }),
  accessRulesYaml: (id) => requestText(`/database-sources/${id}/access-rules/yaml`),
  distinctValues: (id, view, column) =>
    request(`/database-sources/${id}/distinct-values?view=${encodeURIComponent(view)}&column=${encodeURIComponent(column)}`),
};

/** What sits on one Knowledge Base — RAG-08's per-shelf pipeline (#94), watched
 *  by the upload screen (RAG-12 / #98) as it moves `pending → extracting →
 *  chunking → embedding → indexed`. */
export const documents = {
  list: (kbId) => request(`/knowledge-bases/${kbId}/documents`),
  read: (kbId, documentId) => request(`/knowledge-bases/${kbId}/documents/${documentId}`),
  upload: (kbId, file) => upload(`/knowledge-bases/${kbId}/documents`, file),
  reingest: (kbId, documentId) => request(`/knowledge-bases/${kbId}/documents/${documentId}/reingest`, { method: "POST" }),
  remove: (kbId, documentId) => request(`/knowledge-bases/${kbId}/documents/${documentId}`, { method: "DELETE" }),
  /** "Xem lại" (RAG-12 / #98): the original bytes, opened in a new tab.
   *
   *  Fetched with the session's `Authorization` header and handed to the
   *  browser as a `blob:` URL — never as `?token=...` on the request itself,
   *  which would put a bearer credential in server logs, browser history and
   *  the `Referer` header of whatever that tab loads next. The object URL
   *  this returns is only ever valid in this tab and this session. */
  viewContent: async (kbId, documentId) => {
    const response = await fetch(`${BASE}/knowledge-bases/${kbId}/documents/${documentId}/content`, {
      headers: headersFor(false),
    });
    if (!response.ok) {
      const payload = parseJson(await response.text());
      throw new ApiError(response.status, messageOf(payload, response.status), payload);
    }
    return URL.createObjectURL(await response.blob());
  },
};

export const designer = {
  start: (description) => request("/solution-interviews", { method: "POST", body: { description } }),
  list: () => request("/solution-interviews"),
  read: (id) => request(`/solution-interviews/${id}`),
  answer: (id, answer) => request(`/solution-interviews/${id}/answers`, { method: "POST", body: { answer } }),
  buildSpec: (id) => request(`/solution-interviews/${id}/spec`, { method: "POST" }),
  /* Claude-Code-style recommend: 0–5 pickable answers for the open question,
     the first maybe flagged `recommended`. A read-only model call the screen
     fires when it shows a question — never on the answer path. `{field, multi,
     suggestions}`; an empty list means "type it yourself", which is also what a
     failed call degrades to. */
  suggestions: (id) => request(`/solution-interviews/${id}/suggestions`),
};

export const spec = {
  read: (agentId, versionId) => request(`/agents/${agentId}/versions/${versionId}/solution-spec`),
  revise: (agentId, versionId, document) =>
    request(`/agents/${agentId}/versions/${versionId}/solution-spec`, { method: "PUT", body: document }),
  confirm: (agentId, versionId) =>
    request(`/agents/${agentId}/versions/${versionId}/solution-spec/confirmation`, { method: "POST" }),
};

export const workflow = {
  nodeTypes: () => request("/workflow/node-types"),
  tools: () => request("/workflow/tools"),
  /** The starting shapes on offer, for the creator who would rather pick one
   *  than describe one. Nothing here is required to generate a workflow — see
   *  `generate` below. */
  templates: () => request("/workflow/templates"),
  /** Draft this version's workflow. With no `templateId` the platform composes a
   *  graph for this spec's own problem; with one, it builds that template. The
   *  first is the default because a creator who described a business described
   *  *that* business, not the nearest shape on a shelf. */
  generate: (agentId, versionId, templateId = null) =>
    request(`/agents/${agentId}/versions/${versionId}/workflow`, {
      method: "POST",
      body: { template_id: templateId },
    }),
  read: (agentId, versionId) => request(`/agents/${agentId}/versions/${versionId}/workflow`),
  save: (agentId, versionId, document) =>
    request(`/agents/${agentId}/versions/${versionId}/workflow`, { method: "PUT", body: document }),
  confirm: (agentId, versionId) =>
    request(`/agents/${agentId}/versions/${versionId}/workflow/confirm`, { method: "POST" }),
  /** One sentence in, the whole edited graph out — and nothing saved. The server
   *  proposes; `save` above is still the only call that stores anything, which
   *  is what keeps undo an ordinary edit rather than a second endpoint. */
  refine: (agentId, versionId, instruction) =>
    request(`/agents/${agentId}/versions/${versionId}/workflow/refine`, { method: "POST", body: { instruction } }),
  /** ADR-0050 — one turn with the meta agent. Successor of `refine`: same
   *  writes-nothing contract, but the reply also carries the meta agent's own
   *  step trail, its explanation, and possibly a question back. */
  workbenchTurn: (agentId, versionId, loiNhan, viewingRunId) =>
    request(`/agents/${agentId}/versions/${versionId}/workbench/turn`, {
      method: "POST",
      body: { loi_nhan: loiNhan, viewing_run_id: viewingRunId ?? null },
    }),
  pendingApprovals: (agentId, versionId) => request(`/agents/${agentId}/versions/${versionId}/workflow-approvals`),
};

export const runs = {
  /** `options.runAs` is who this trial asks as (DS-05) — blank is nobody, so
   *  every `data_query` read is denied. `options.conversationId` carries on a
   *  trial thread; null starts a new one. */
  sandbox: (agentId, versionId, message, onEvent, { runAs, conversationId, sendForReal = false, ...options } = {}) =>
    stream(
      `/agents/${agentId}/versions/${versionId}/workflow-sandbox-runs`,
      { message, run_as: runAs || null, conversation_id: conversationId || null, send_for_real: Boolean(sendForReal) },
      onEvent,
      options,
    ),
  /** "Chạy với tư cách…"'s suggestions, read off the employee views behind
   *  this version's `data_query` nodes. */
  askers: (agentId, versionId, q) =>
    request(`/agents/${agentId}/versions/${versionId}/askers?q=${encodeURIComponent(q || "")}`),
  trialChat: (agentId, versionId, body, onEvent, options) =>
    stream(`/agents/${agentId}/versions/${versionId}/workflow-chat`, body, onEvent, options),
  deployedChat: (agentId, body, onEvent, options) =>
    stream(`/agents/${agentId}/deployed-workflow-chat`, body, onEvent, options),
  /** The V1 door — a published *pipeline* (`AgentSpec`) rather than a workflow.
   *  Same body, same `thread` first frame; its events are per step
   *  (`step_started`/`token`/`step`) where a workflow's are per node, which
   *  `lib/turns.js` reads as one and the same. */
  pipelineChat: (agentId, body, onEvent, options) =>
    stream(`/agents/${agentId}/chat`, body, onEvent, options),
  decideApproval: (agentId, versionId, approvalId, decision, onEvent, options) =>
    stream(`/agents/${agentId}/versions/${versionId}/workflow-approvals/${approvalId}`, decision, onEvent, options),
};

/** The chat sidebar's own calls — which conversations are mine, and what was said.
 *
 * `list` returns labels and timestamps only; `messages` is what fetches a
 * transcript, and it is called for the one conversation that gets opened rather
 * than for all of them. That split is the whole reason opening the chat screen
 * costs one small request instead of the entire history.
 *
 * `remove` takes a conversation out of the list. It does not erase what was
 * said — the checkpointer keeps that (see the endpoint's docstring), so the
 * button that calls this must not be labelled "delete for ever". */
export const conversations = {
  list: (agentId) => request(`/agents/${agentId}/conversations`),
  messages: (agentId, conversationId) => request(`/agents/${agentId}/conversations/${conversationId}/messages`),
  rename: (agentId, conversationId, title) =>
    request(`/agents/${agentId}/conversations/${conversationId}`, { method: "PATCH", body: { title } }),
  remove: (agentId, conversationId) =>
    request(`/agents/${agentId}/conversations/${conversationId}`, { method: "DELETE" }),
};

/* The PM control room (docs/PM-PLAYBOOK.md §5). Admin-only on the server;
   every read is one `overview` payload so the six widgets describe the same
   instant, and every lever is the smallest write that does the thing. */
export const pm = {
  overview: (windowDays = 28, includeSimulated = false) =>
    request(`/pm/overview?window_days=${windowDays}&include_simulated=${includeSimulated}`),
  runs: ({ windowDays = 28, includeSimulated = false, outcome, trigger, agentKey, failureNodeType, needsReview, limit = 60 } = {}) => {
    const params = new URLSearchParams({
      window_days: String(windowDays),
      include_simulated: String(includeSimulated),
      limit: String(limit),
    });
    if (outcome) params.set("outcome", outcome);
    if (trigger) params.set("trigger", trigger);
    if (agentKey) params.set("agent_key", agentKey);
    if (failureNodeType) params.set("failure_node_type", failureNodeType);
    if (needsReview) params.set("needs_review", "true");
    return request(`/pm/runs?${params}`);
  },
  run: (runId) => request(`/pm/runs/${runId}`),
  /** The next unlabelled production run worth a person's minute — ADR-0053 §3. */
  reviewNext: (includeSimulated = false, agentKey) => {
    const params = new URLSearchParams({ include_simulated: String(includeSimulated) });
    if (agentKey) params.set("agent_key", agentKey);
    return request(`/pm/review/next?${params}`);
  },
  label: (runId, passed, codes, note) =>
    request(`/pm/runs/${runId}/label`, { method: "POST", body: { passed, codes: codes || [], note: note || null } }),
  versionReport: (versionId, windowDays = 28) => request(`/pm/versions/${versionId}/report?window_days=${windowDays}`),
  judgeSample: (limit = 10, agentKey) =>
    request("/pm/judge/sample", { method: "POST", body: { limit, agent_key: agentKey || null } }),
  setDeploymentStatus: (agentId, serving) =>
    request(`/pm/deployments/${agentId}/status`, { method: "PUT", body: { serving } }),
  feedback: (runId, thumbsUp, comment) =>
    request(`/pm/runs/${runId}/feedback`, { method: "POST", body: { thumbs_up: thumbsUp, comment: comment || null } }),
  decideApproval: (agentId, approvalId, approved, reason) =>
    request(`/pm/agents/${agentId}/approvals/${approvalId}`, { method: "POST", body: { approved, reason: reason || null } }),
  promoteRun: (runId, inputMessage, expected, criterion) =>
    request(`/pm/runs/${runId}/promote`, {
      method: "POST",
      body: { input_message: inputMessage || null, expected, criterion: criterion || null },
    }),
  runGate: ({ mode = "simulated", fidelity = 0.78, seed = null, repeats = 1 } = {}) =>
    request("/pm/gate/runs", { method: "POST", body: { mode, fidelity, seed, repeats } }),
  gateRun: (gateRunId) => request(`/pm/gate/runs/${gateRunId}`),
};

/* The thumbs a person leaves on an answer they just got — filed by the run's
 * `trace_id`, which the `thread` event announced before the answer arrived
 * (ADR-0053 §3). One door for both pipelines. */
export const runFeedback = {
  leave: (traceId, thumbsUp, comment) =>
    request("/runs/feedback", { method: "POST", body: { trace_id: traceId, thumbs_up: thumbsUp, comment: comment || null } }),
};

export const evaluation = {
  generate: (agentId, versionId) =>
    request(`/agents/${agentId}/versions/${versionId}/workflow-eval-cases/generate`, { method: "POST" }),
  cases: (agentId, versionId) => request(`/agents/${agentId}/versions/${versionId}/eval-cases`),
  /** `caseIds` null/omitted = the whole set; a list = "đã chọn" / "chỉ fail" —
   *  the screen decides the ids, the backend only filters. */
  run: (agentId, versionId, caseIds, kind = "gate") =>
    request(`/agents/${agentId}/versions/${versionId}/workflow-eval-runs`, {
      method: "POST",
      /* EV-04: "smoke" = ~20 cases the system picks, cached; "gate" = the set the approval gate reads. */
      ...(caseIds || kind !== "gate" ? { body: { ...(caseIds ? { case_ids: caseIds } : {}), kind } } : {}),
    }),
  latest: (agentId, versionId) => request(`/agents/${agentId}/versions/${versionId}/eval-runs/latest`),
  /** One run read back — polled while it is `running` (EV-01: the POST answers
   *  at once and the cases run in the background). */
  /** EV-02: freeze again every view the graph reads, for every case — after the
   *  Hệ CSDL's columns changed, or to grade on today's data. */
  /** EV-05: "agent sai" / "case sai" on a failing case — who and when are kept. */
  triage: (agentId, versionId, caseId, verdict, runId) =>
    request(`/agents/${agentId}/versions/${versionId}/eval-cases/${caseId}/triage`, {
      method: "POST",
      body: { verdict, run_id: runId },
    }),
  history: (agentId, versionId) => request(`/agents/${agentId}/versions/${versionId}/eval-suite/history`),
  /** EV-03: library templates, data questions, then the model — up to ≥100 live cases. */
  buildSuite: (agentId, versionId) =>
    request(`/agents/${agentId}/versions/${versionId}/eval-suite/build`, { method: "POST" }),
  recordCassettes: (agentId, versionId) =>
    request(`/agents/${agentId}/versions/${versionId}/eval-cassettes/record`, { method: "POST" }),
  runById: (agentId, versionId, runId) => request(`/agents/${agentId}/versions/${versionId}/eval-runs/${runId}`),
  updateCase: (agentId, versionId, caseId, payload) =>
    request(`/agents/${agentId}/versions/${versionId}/eval-cases/${caseId}`, { method: "PATCH", body: payload }),
  /** Throw one test case away. A run already scored against it keeps its own
   *  copy, so deleting a case rewrites no history — see `EvalRun.results`. */
  removeCase: (agentId, versionId, caseId) =>
    request(`/agents/${agentId}/versions/${versionId}/eval-cases/${caseId}`, { method: "DELETE" }),

  /* — the suite's own doors (ADR-0054 §2), keyed by agent, not version — */
  importSuite: (agentId, file, dryRun) =>
    upload(`/agents/${agentId}/eval-suite/import?dry_run=${dryRun ? "true" : "false"}`, file),
  exportSuite: (agentId, format) => requestText(`/agents/${agentId}/eval-suite/export?format=${format}`),
  template: (agentId) => requestText(`/agents/${agentId}/eval-suite/template.csv`),
  /** DS-22: the sample suite the agent's template ships, if any — `{available, name, case_count}`. */
  sample: (agentId) => request(`/agents/${agentId}/eval-suite/sample`),
  /** Load it through the ordinary importer — the same row report as an upload. */
  loadSample: (agentId) => request(`/agents/${agentId}/eval-suite/sample`, { method: "POST" }),
};

export const approvals = {
  submit: (agentId, versionId) =>
    request(`/agents/${agentId}/versions/${versionId}/approval-request`, { method: "POST" }),
  status: (agentId, versionId) => request(`/agents/${agentId}/versions/${versionId}/approval`),
  queue: () => request("/admin/approvals"),
  /** Reviews that are over, newest first (ADR-0064). A second call rather than a
   *  filter on `queue()`: a settled row carries who decided and when, a waiting
   *  row carries why it is stuck, and neither field means anything on the
   *  other's rows. */
  decided: () => request("/admin/approvals/decided"),
  /** Serves any version, decided ones included — which is why the *screen* is
   *  what withholds the buttons. Nothing here is read-only; `dossier.status` is. */
  dossier: (versionId) => request(`/admin/approvals/${versionId}`),
  approve: (versionId) => request(`/admin/approvals/${versionId}/approve`, { method: "POST" }),
  reject: (versionId, reason) =>
    request(`/admin/approvals/${versionId}/reject`, { method: "POST", body: { reason } }),
  overrideEval: (versionId, reason) =>
    request(`/admin/approvals/${versionId}/eval-override`, { method: "POST", body: { reason } }),
};

/* Per-user OAuth — the person links their own Google account, and every agent
 * they own runs Gmail/Drive actions as that account. `start` answers with the
 * consent URL to navigate to; the backend's callback brings the browser home
 * with `?google=connected|failed|denied` in the query string. */
/* MCP connections the creator registers themselves — the port of Agent
 * Builder's "add server" dialog. `credential.secret` / `credential.headers`
 * carry the actual token and go **one way only**: the backend puts them in the
 * secret store and every response carries a `secret_ref` instead, so nothing
 * here ever reads a credential back. */
export const mcpConnections = {
  mine: () => request("/workflow/tools/registrations"),
  register: (payload) => request("/workflow/tools", { method: "POST", body: payload }),
  setAllowedActions: (registrationId, actions) =>
    request(`/workflow/tools/registrations/${registrationId}/allowed-actions`, {
      method: "PUT",
      body: { allowed_actions: actions },
    }),
  /** Sandbox stand-ins for the write actions. An admin cannot approve a
   *  connection until every action that writes has one (ADR-0023 §4) — a
   *  rehearsal of a write has to say what it *would* have done. */
  setMocks: (registrationId, examples) =>
    request(`/workflow/tools/registrations/${registrationId}/mocks`, { method: "PUT", body: { examples } }),
};

export const userConnections = {
  list: () => request("/user-connections"),
  startGoogle: () => request("/user-connections/google/start", { method: "POST" }),
  /** The paste path, beside `startGoogle`. Carries a live credential, so it
   *  goes out once and is never read back — every response names a
   *  `token_kind`, never the token. */
  connectGoogleWithToken: (accessToken) =>
    request("/user-connections/google/token", { method: "POST", body: { access_token: accessToken } }),
  disconnectGoogle: () => request("/user-connections/google", { method: "DELETE" }),
};

/* UC1-26 (#142) — pull a signed template from the vendor's control plane.
 * Does nothing at all until `CONTROL_PLANE_URL` is configured; that empty
 * default is the correct state for a fresh install, not a misconfiguration. */
export const controlPlane = {
  pullTemplate: (templateId) => request(`/control-plane/templates/${templateId}/pull`, { method: "POST" }),
};

/* UC1-01 (#117) — the other end of "Tài khoản đang chờ kích hoạt — liên hệ
 * agent_admin". The login screen tells a locked-out person to go find an
 * admin; this is what that admin opens. */
export const adminUsers = {
  /** `status` omitted = the whole directory. The "Chờ kích hoạt" tab passes
   *  `"pending"`, and passing it explicitly is deliberate: a tab that meant to
   *  show the queue and forgot the filter would show everybody, which reads as
   *  a much longer queue than there is. */
  list: (status) => request(`/admin/users${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  activate: (userId) => request(`/admin/users/${userId}/activate`, { method: "POST" }),
};

export const platformConfig = {
  read: () => request("/admin/config"),
  /** Change the eval bar and nothing else.
   *
   *  `model_allowlist` is deliberately absent from the body rather than sent
   *  back unchanged. The endpoint reads an omitted field as "leave this one
   *  alone", which is what stops a screen that edits the threshold from writing
   *  an allowlist it read before somebody else changed it. */
  setEvalThreshold: (evalPassThreshold) =>
    request("/admin/config", { method: "PATCH", body: { eval_pass_threshold: evalPassThreshold } }),
  /** Change which models an agent may be built out of, and nothing else.
   *
   *  The mirror image of `setEvalThreshold`: the threshold is omitted so this
   *  screen cannot write back a number it read before somebody else changed it.
   *  A list the platform refuses — an unvetted model, or an empty allowlist,
   *  which is not a policy — comes back as a 422 carrying the reason. */
  setModelAllowlist: (models) =>
    request("/admin/config", { method: "PATCH", body: { model_allowlist: models } }),
};

export const deployment = {
  read: (agentId) => request(`/agents/${agentId}/deployment`),
  /** How this Deployment has actually been serving (#55) — real traffic only:
   *  the creator's own rehearsals are excluded server-side, so nothing here has
   *  to be filtered again on the way in. */
  operations: (agentId) => request(`/agents/${agentId}/deployment/operations`),
  publish: (agentId, versionId) =>
    request(`/agents/${agentId}/deployed-version`, { method: "PUT", body: { version_id: versionId } }),
  setStatus: (agentId, status) =>
    request(`/agents/${agentId}/deployment/status`, { method: "PUT", body: { status } }),
  setAccess: (agentId, accessMode, emails) =>
    request(`/agents/${agentId}/deployment/access`, {
      method: "PUT",
      body: { access_mode: accessMode, allowed_user_emails: emails },
    }),
  setRateLimit: (agentId, perUser) =>
    request(`/agents/${agentId}/deployment/rate-limit`, { method: "PUT", body: { rate_limit_per_user: perUser } }),
  regenerateKey: (agentId) => request(`/agents/${agentId}/deployment/api-key`, { method: "POST" }),
};
