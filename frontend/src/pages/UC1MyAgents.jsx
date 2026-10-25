import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, MessageSquare, Send, ShieldCheck } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell, Spinner } from "../components/ui";

/* ============================================================
   The end user's door onto a deployed UC-1 agent.

   `POST /agents/{id}/deployed/chat` was always open to any signed-in caller —
   its own module docstring says why: "a deployed agent is a product, and the
   point of deploying is that other people can use it". But every screen that
   called it lived inside the Workbench, which is gated on `agent_admin`, and
   the only catalog (`GET /uc1-agents`) is `agent_admin` too. So the whole
   audience an agent is *built for* — sales_rep, sales_manager — could hold a
   valid session, own the right role, and still have no way to learn that an
   agent existed for them, let alone open it.

   This screen is deliberately not a second Workbench. A rep has no business
   with drafts, diffs, golden sets or a Deploy button; they have one question
   and want one answer. What it does keep is the transcript, because an answer
   about money that cannot be traced to a tool call and a source is not one
   anybody should act on — the same reasoning FR-37 gives for showing it in
   the sandbox.
   ============================================================ */

function money(value) {
  return `$${(value ?? 0).toFixed(4)}`;
}

/** What the caller will actually see of the data, stated before they ask.
 *
 *  Two people can put the same question to the same agent and get different
 *  numbers — a rep sees their own book, their manager sees the team's — because
 *  the PEP applies a row filter per caller. Saying so up front is the
 *  difference between "the platform enforces my scope" and "this agent is
 *  giving me wrong totals". */
function ScopeNote({ scope }) {
  const wording = {
    own_records: "Bạn chỉ thấy dữ liệu thuộc về chính mình.",
    department: "Bạn thấy dữ liệu của cả phòng ban mình.",
    all: "Bạn thấy toàn bộ dữ liệu agent được phép đọc.",
  };
  return (
    <span className="ab-chip ab-mono" title={wording[scope] || scope}>
      {scope}
    </span>
  );
}

function TranscriptStep({ step }) {
  return (
    <div className="ab-version-item">
      <span className="ab-chip ab-mono">{step.node}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        {step.tool && <p className="ab-mono" style={{ fontSize: "var(--text-xs)", margin: 0 }}>{step.tool}</p>}
        {step.row_filter && Object.keys(step.row_filter).length > 0 && (
          <p className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "var(--color-accent)", margin: "2px 0 0", wordBreak: "break-all" }}>
            phạm vi: {JSON.stringify(step.row_filter)}
          </p>
        )}
        {step.result && (
          <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: "3px 0 0", lineHeight: 1.5, whiteSpace: "pre-wrap" }}>
            {String(step.result).slice(0, 400)}
          </p>
        )}
      </div>
      <span className="ab-chip ab-mono">{Math.round(step.latency_ms)}ms</span>
    </div>
  );
}

/** One conversation with one agent. `turns` accumulates locally rather than
 *  being re-read from the server: the chat endpoint streams a turn and does not
 *  expose the thread's history, so what the screen can honestly show is what it
 *  watched happen. A reload starts a fresh view of a thread the server still
 *  remembers — which is why the turn count is not labelled as the thread's. */
function ChatPanel({ agent, notify }) {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [turns, setTurns] = useState([]);
  const liveRef = useRef(null);

  async function send() {
    const text = message.trim();
    if (!text || busy) return;
    setBusy(true);
    setError("");
    liveRef.current = { question: text, steps: [], final_answer: null, total_cost_usd: 0, failed: null };
    setTurns((previous) => [...previous, liveRef.current]);
    try {
      await api.uc1Chat.deployed(agent.agent_id, { message: text }, (event, data) => {
        const current = liveRef.current;
        if (event === "step") current.steps = [...current.steps, data];
        if (event === "done") {
          current.final_answer = data.final_answer;
          current.total_cost_usd = data.total_cost_usd;
        }
        if (event === "error") current.failed = data;
        setTurns((previous) => previous.map((turn) => (turn === current ? { ...current } : turn)));
      });
      setMessage("");
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ab-stack">
      <div className="card">
        <div className="ab-row" style={{ gap: 8 }}>
          <p className="card-title" style={{ margin: 0 }}>{agent.name}</p>
          <span className="ab-chip ab-mono">v{agent.version}</span>
          <ScopeNote scope={agent.data_scope} />
        </div>
        {agent.description && (
          <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>{agent.description}</p>
        )}
        <textarea
          className="input"
          style={{ minHeight: 70 }}
          placeholder="Khách nào quá hạn trên 30 ngày?"
          value={message}
          disabled={busy}
          onChange={(event) => setMessage(event.target.value)}
        />
        <ErrorNote>{error}</ErrorNote>
        <button type="button" className="btn btn-primary" style={{ alignSelf: "flex-start" }} onClick={send} disabled={busy || !message.trim()}>
          {busy ? <><Spinner size={14} />Đang chạy…</> : <><Send size={14} />Gửi</>}
        </button>
      </div>

      {turns.map((turn, index) => (
        <div key={index} className="card">
          <p className="card-title" style={{ margin: 0 }}>{turn.question}</p>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {turn.steps.map((step, stepIndex) => <TranscriptStep key={stepIndex} step={step} />)}
          </div>
          {turn.failed && <ErrorNote>{turn.failed.message || turn.failed.code || "Lượt chạy bị dừng"}</ErrorNote>}
          {turn.final_answer && (
            <div style={{ paddingTop: "var(--space-3)", borderTop: "1px solid var(--color-divider)" }}>
              <p style={{ fontSize: "var(--text-md)", margin: 0, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{turn.final_answer}</p>
              <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: "6px 0 0" }}>
                chi phí {money(turn.total_cost_usd)}
              </p>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export default function UC1MyAgents({ session, notify }) {
  const [agents, setAgents] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setAgents(await api.uc1Chat.available());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <PageShell>
        {selected ? (
          <>
            <PageHead title={selected.name} hint="Câu trả lời luôn nằm trong phạm vi dữ liệu của riêng bạn.">
            <button type="button" className="btn btn-secondary" onClick={() => setSelected(null)}>
              <ArrowLeft size={14} />Danh sách agent
            </button>
            </PageHead>
            <ChatPanel agent={selected} notify={notify} />
          </>
        ) : (
          <>
            <PageHead title="Agent dành cho bạn" hint="Các agent đang chạy mà vai trò của bạn được phép dùng. Mọi câu trả lời đều theo phạm vi dữ liệu của riêng bạn." />
            <ErrorNote>{error}</ErrorNote>
            {loading ? (
              <div className="ab-empty"><Spinner size={22} /></div>
            ) : agents.length === 0 ? (
              /* Not phrased as an error: an empty list is the honest answer when
                 nothing is deployed yet, or when nothing deployed names this
                 caller's role in its `audience`. Both are ordinary states, and
                 neither is something the reader can fix by retrying. */
              <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>
                Chưa có agent nào đang chạy mà vai trò <span className="ab-mono" style={{ margin: "0 4px" }}>{session?.role}</span> được dùng.
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(304px, 1fr))", gap: 18 }}>
                {agents.map((agent) => (
                  /* Same shape as a Dashboard card, down to the blueprint corners:
                     a deployed agent is the same kind of object on both screens. */
                  <div
                    key={agent.agent_id}
                    className="blueprint"
                    role="button"
                    tabIndex={0}
                    style={{ padding: "var(--space-4)", display: "flex", flexDirection: "column", gap: 11, minHeight: 172, cursor: "pointer", animation: "agUp .4s ease both" }}
                    onClick={() => setSelected(agent)}
                    onKeyDown={(event) => event.key === "Enter" && setSelected(agent)}
                  >
                    <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
                    <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 }}>
                      <span style={{ width: 30, height: 30, flexShrink: 0, display: "grid", placeItems: "center", border: "1px solid var(--color-divider)", color: "var(--color-accent)" }}>
                        <MessageSquare size={16} />
                      </span>
                      <ScopeNote scope={agent.data_scope} />
                    </div>
                    <div>
                      <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)", lineHeight: 1.2 }}>{agent.name}</p>
                      <p className="ab-mono" style={{ margin: "2px 0 0", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)" }}>
                        {agent.agent_id} · v{agent.version}
                      </p>
                    </div>
                    {agent.description && (
                      <p className="text-muted" style={{ margin: 0, fontSize: "var(--text-sm)", lineHeight: 1.55 }}>{agent.description}</p>
                    )}
                    <div className="text-muted" style={{ display: "flex", alignItems: "center", gap: 5, fontSize: "var(--text-xs)", marginTop: "auto", paddingTop: "var(--space-3)", borderTop: "1px solid var(--color-divider)" }}>
                      <ShieldCheck size={13} />Mọi hành động ghi vẫn cần duyệt
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </PageShell>
  );
}
