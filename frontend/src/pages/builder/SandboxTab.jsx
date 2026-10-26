import { useEffect, useState } from "react";
import { Play, CheckCircle2, XCircle, AlertCircle, ShieldCheck } from "lucide-react";
import * as api from "../../lib/api";
import { lookOf } from "../../lib/nodes";
import { ErrorNote, Hint, PageShell, Section, Spinner } from "../../components/ui";
import { AgentBusy } from "../../components/mascot";
import { useAgentActivity } from "../../components/agent-presence";
import { useLang } from "../../i18n";
import SourcesPanel from "../../components/SourcesPanel";
import { CITING_TOOLS } from "../../lib/turns";

/* ============================================================
   A real sandbox run (#46): one SSE stream, rendered as it arrives.

   Side-effecting tools run their mock here — which the transcript says line by
   line, in `simulated`, read off the resolved implementation rather than off the
   fact that this is a rehearsal. A run that stops at a `HumanApproval` node ends
   with `awaiting_approval` and nothing is stored until somebody decides; that
   decision resumes the *same* run on a second stream.
   ============================================================ */
export default function SandboxTab({ agentId, versionId, onActiveNode, onVersionChanged, notify }) {
  const { t } = useLang();
  const [message, setMessage] = useState("");
  const [lines, setLines] = useState([]);
  const [running, setRunning] = useState(false);

  useAgentActivity("processing", running, t("running in the sandbox"));
  const [summary, setSummary] = useState(null);
  const [pending, setPending] = useState(null);
  const [rejectReason, setRejectReason] = useState("");
  const [error, setError] = useState("");

  // A pause survives a reload: the row is in the database, not in this stream.
  useEffect(() => {
    let cancelled = false;
    api.workflow.pendingApprovals(agentId, versionId)
      .then((rows) => { if (!cancelled && rows.length) setPending(rows[rows.length - 1]); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [agentId, versionId]);

  const append = (line) => setLines((current) => [...current, line]);

  function handle(name, data) {
    switch (name) {
      case "node_started":
        onActiveNode(data.node_id);
        append({ kind: "started", nodeId: data.node_id, nodeType: data.node_type, text: "" });
        break;
      case "token":
        // Tokens belong to the node that is streaming them, appended in place so
        // an answer appears the way the model produced it.
        setLines((current) => {
          const last = current[current.length - 1];
          if (last && last.kind === "started" && last.nodeId === data.node_id) {
            return [...current.slice(0, -1), { ...last, text: last.text + data.text }];
          }
          return current;
        });
        break;
      case "tool":
        append({ kind: "tool", nodeId: data.node_id, tool: data.tool, args: data.args, simulated: data.simulated });
        break;
      case "tool_result":
        append({ kind: "tool_result", nodeId: data.node_id, tool: data.tool, ok: data.ok, result: data.result, simulated: data.simulated, citations: data.citations || [] });
        break;
      case "node":
        // The node's settled outcome lands *on* the line its `node_started`
        // opened, not under it. Every node used to take two lines — the one
        // its tokens streamed into and a second repeating the same text with
        // the port attached — so a six-node run read as twelve steps.
        setLines((current) => {
          const settled = {
            kind: "node", nodeId: data.node_id, nodeType: data.node_type, content: data.content,
            port: data.port_taken, iteration: data.iteration, forcedExit: data.forced_exit,
          };
          const opened = current.findLastIndex((line) => line.kind === "started" && line.nodeId === data.node_id);
          if (opened === -1) return [...current, settled];
          const merged = current.slice();
          merged[opened] = settled;
          return merged;
        });
        break;
      case "budget_exceeded":
        append({ kind: "stopped", nodeId: data.node_id, text: data.message, limit: data.limit, limitValue: data.limit_value, used: data.used });
        break;
      case "usage":
      case "cost":
        setSummary(data);
        break;
      case "awaiting_approval":
        onActiveNode(null);
        setPending({ ...data, version_id: versionId });
        append({ kind: "paused", nodeId: data.node_id, text: data.message, role: data.approver_role });
        break;
      case "sandbox_run":
        onActiveNode(null);
        setPending(null);
        append({ kind: "stored", status: data.status, versionStatus: data.version_status, error: data.error, runId: data.run_id });
        onVersionChanged(data.version_status);
        break;
      case "error":
        onActiveNode(null);
        append({ kind: "failed", text: data.message });
        break;
      default:
        break;
    }
  }

  async function run() {
    if (running || !message.trim()) return;
    setRunning(true);
    setError("");
    setLines([]);
    setSummary(null);
    setPending(null);
    try {
      await api.runs.sandbox(agentId, versionId, message.trim(), handle);
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setRunning(false);
      onActiveNode(null);
    }
  }

  async function decide(approved) {
    if (!pending || running) return;
    if (!approved && !rejectReason.trim()) {
      setError(t("A rejection needs a reason — the 'rejected' branch is told why."));
      return;
    }
    setRunning(true);
    setError("");
    const decision = { approved, reason: approved ? null : rejectReason.trim() };
    const approvalId = pending.approval_id;
    setPending(null);
    try {
      await api.runs.decideApproval(agentId, versionId, approvalId, decision, handle);
      setRejectReason("");
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setRunning(false);
      onActiveNode(null);
    }
  }

  return (
    <PageShell width="narrow">
      <Section
        title={t('Sandbox run')}
        hint={t("Every tool with side effects runs its mock — a trial never touches the real world. A run that finishes is the evidence an approval request is built on.")}
      >

        <div className="blueprint" style={{ padding: "var(--space-4)", marginBottom: 20 }}>
          <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
          <div className="field">
            <label htmlFor="sbx">{t('Test message')}</label>
            <textarea
              id="sbx"
              className="input"
              rows={2}
              placeholder={t("What would a user ask this agent?")}
              value={message}
              disabled={running}
              onChange={(event) => setMessage(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); run(); }
              }}
            />
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 12, flexWrap: "wrap" }}>
            <button className="btn btn-primary" onClick={run} disabled={running || !message.trim()} style={{ height: 38 }}>
              {running ? <Spinner size={14} /> : <Play size={14} />}{running ? t("Running…") : t("Run in sandbox")}
            </button>
            <span style={{ fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
              {t("Billed separately from live traffic.")}
            </span>
          </div>
        </div>

        <ErrorNote>{error}</ErrorNote>

        {summary && (
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 18 }}>
            <div className="ab-metric"><div className="val">{(summary.input_tokens ?? 0) + (summary.output_tokens ?? 0)}</div><div className="lbl">{t("tokens")}</div></div>
            <div className="ab-metric"><div className="val" style={{ color: "var(--color-accent)" }}>${(summary.usd ?? 0).toFixed(4)}</div><div className="lbl">{t(summary.label || "cost")}</div></div>
            {summary.seconds != null && <div className="ab-metric"><div className="val">{summary.seconds.toFixed(1)}s</div><div className="lbl">{t("duration")}</div></div>}
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {lines.map((line, index) => <TraceLine key={index} line={line} />)}
          {running && <AgentBusy state="processing" label={t("running")} size={34} />}
        </div>

        {pending && (
          <div style={{ border: "1px solid var(--color-accent)", background: "var(--color-accent-100)", color: "var(--color-accent-800)", padding: "var(--space-4)", marginTop: 14, animation: "agUp .35s ease both" }}>
            <p style={{ margin: "0 0 4px", display: "flex", alignItems: "center", gap: 8, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>
              <ShieldCheck size={16} />{t("Waiting on {who}", { who: pending.approver_role || t("an approver") })}
            </p>
            <p style={{ margin: "0 0 12px", fontSize: "var(--text-sm)", lineHeight: 1.6 }}>{pending.message}</p>
            <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
              <input
                className="input"
                style={{ flex: 1, minWidth: 220 }}
                placeholder={t("Reason (required to reject)")}
                value={rejectReason}
                onChange={(event) => setRejectReason(event.target.value)}
              />
              <button className="btn btn-primary" onClick={() => decide(true)} disabled={running}>{t("Approve & continue")}</button>
              <button className="btn btn-secondary" onClick={() => decide(false)} disabled={running}>{t('Reject')}</button>
            </div>
          </div>
        )}

        {lines.length === 0 && !running && (
          <div style={{ border: "1px dashed var(--color-divider)", padding: "var(--space-12) var(--space-6)", textAlign: "center", fontSize: "var(--text-md)", color: "color-mix(in srgb, var(--color-text) 58%, transparent)" }}>
            {t("No sandbox run yet. Send a test message to watch every step with mock tools.")}
          </div>
        )}
      </Section>
    </PageShell>
  );
}

function TraceLine({ line }) {
  const { t } = useLang();
  if (line.kind === "failed") {
    return (
      <div className="ab-trace-item" style={{ borderColor: "rgba(239,68,68,.3)" }}>
        <XCircle size={15} style={{ color: "#f87171", flexShrink: 0 }} />
        <span style={{ fontSize: "var(--text-sm)" }}>{line.text}</span>
      </div>
    );
  }

  if (line.kind === "stopped") {
    return (
      <div className="ab-trace-item" style={{ borderColor: "rgba(245,158,11,.3)" }}>
        <AlertCircle size={15} style={{ color: "var(--ab-warn)", flexShrink: 0 }} />
        <div style={{ fontSize: "var(--text-sm)" }}>
          {line.text}
          <div className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "var(--ab-muted)", marginTop: 4 }}>
            {line.limit}: used {line.used} of {line.limitValue}
          </div>
        </div>
      </div>
    );
  }

  if (line.kind === "stored") {
    const ok = line.status === "succeeded";
    return (
      <div className="ab-trace-item" style={{ borderColor: ok ? "rgba(0,212,168,.3)" : "rgba(239,68,68,.3)" }}>
        {ok ? <CheckCircle2 size={15} style={{ color: "var(--ab-primary)", flexShrink: 0 }} /> : <XCircle size={15} style={{ color: "#f87171", flexShrink: 0 }} />}
        <div style={{ fontSize: "var(--text-sm)" }}>
          Trial run #{line.runId} stored as <b>{line.status}</b>. Version is now <b>{line.versionStatus}</b>.
          {line.error && <div style={{ color: "#f8a5a5", marginTop: 4 }}>{line.error}</div>}
        </div>
      </div>
    );
  }

  if (line.kind === "paused") {
    return (
      <div className="ab-trace-item" style={{ borderColor: "rgba(236,72,153,.3)" }}>
        <ShieldCheck size={15} style={{ color: "var(--ab-pink)", flexShrink: 0 }} />
        <div style={{ fontSize: "var(--text-sm)" }}>
          <span className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "var(--ab-muted)" }}>{line.nodeId}</span>
          <div style={{ marginTop: 3 }}>Paused for {line.role || "approval"}: {line.text}</div>
        </div>
      </div>
    );
  }

  if (line.kind === "tool" || line.kind === "tool_result") {
    const failed = line.kind === "tool_result" && line.ok === false;
    // RAG-14 / #100: a `knowledge_search` (or `data_query`, DS-04) result renders its structured
    // citations, not the raw text blob every other tool still gets — the
    // sandbox transcript is the same evidence a deployed chat's "Nguồn tham
    // khảo" panel is built from, so it reads the same way here.
    const isCiting = line.kind === "tool_result" && CITING_TOOLS.has(line.tool);
    return (
      <div className="ab-trace-item" style={failed ? { borderColor: "rgba(239,68,68,.3)" } : undefined}>
        <div style={{ width: 26, flexShrink: 0, textAlign: "center", color: "var(--ab-muted)", fontSize: "var(--text-xs)" }} className="ab-mono">
          {line.kind === "tool" ? "→" : "←"}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span className="ab-mono" style={{ fontSize: "var(--text-sm)" }}>{line.tool}</span>
            {line.simulated && <span className="ab-chip" style={{ color: "var(--ab-warn)", borderColor: "rgba(245,158,11,.3)" }}>{t('simulated')}</span>}
            {failed && <span className="ab-chip" style={{ color: "#f87171", borderColor: "rgba(239,68,68,.3)" }}>{t('failed')}</span>}
          </div>
          {isCiting ? (
            <SourcesPanel entries={[{ ok: line.ok, result: line.result, citations: line.citations }]} />
          ) : (
            <pre className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 65%, transparent)", background: "var(--color-surface)", border: "1px solid var(--color-divider)", borderRadius: 0, padding: "var(--space-1) var(--space-2)", margin: "6px 0 0", whiteSpace: "pre-wrap", overflowX: "auto" }}>
              {line.kind === "tool" ? JSON.stringify(line.args ?? {}, null, 1) : String(line.result ?? "")}
            </pre>
          )}
        </div>
      </div>
    );
  }

  // `started` (with whatever tokens streamed into it) and `node` share a shape.
  const look = lookOf(line.nodeType);
  const body = line.kind === "node" ? line.content : line.text;
  return (
    <div className="ab-trace-item">
      <div style={{ width: 26, height: 26, borderRadius: 7, background: `${look.color}22`, border: `1px solid ${look.color}44`, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
        <look.icon size={13} style={{ color: look.color }} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
          <span className="ab-mono" style={{ fontSize: "var(--text-sm)", fontWeight: 500 }}>{line.nodeId}</span>
          <span style={{ display: "flex", gap: 6 }}>
            {line.port && <span className="ab-chip">port: {line.port}</span>}
            {line.iteration != null && <span className="ab-chip">loop {line.iteration}</span>}
            {line.forcedExit && <span className="ab-chip" style={{ color: "var(--ab-warn)", borderColor: "rgba(245,158,11,.3)" }}>hit the loop ceiling</span>}
          </span>
        </div>
        {body ? <p style={{ fontSize: "var(--text-sm)", color: "var(--ab-muted)", margin: "5px 0 0", lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{body}</p> : null}
      </div>
    </div>
  );
}
