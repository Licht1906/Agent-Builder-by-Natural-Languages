import { useCallback, useEffect, useState } from "react";
import { Check, RefreshCw, X } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell, Spinner } from "../components/ui";
import { AgentMascot } from "../components/mascot";

/* ============================================================
   UC1-21 (#137), FR-42/FR-43 — the action approval queue.

   A different queue from the one at `/approvals/queue`, and the distinction
   is worth keeping straight: that one approves a *spec version*, once, before
   it may run at all. This one approves a *single write-tool call* that a
   running agent paused on because the policy answered `require_approval` —
   keyed by `action_hash`, not by `(agent_id, version)`.

   Approving does not merely mark a row: the backend executes the tool call
   right there, under the same frozen policy spec the paused turn was judged
   by, and hands back its result. Which is why the args are rendered in full —
   whoever clicks is authorising exactly this call, not a category of call.
   ============================================================ */

export default function UC1ActionQueue({ notify }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(null);
  const [reasons, setReasons] = useState({});
  const [outcomes, setOutcomes] = useState({});

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setRows(await api.uc1Actions.queue());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function decide(row, decision) {
    setBusy(`${row.action_hash}:${decision}`);
    setError("");
    try {
      const outcome = await api.uc1Actions.decide(row.action_hash, decision, reasons[row.action_hash] || null);
      setOutcomes((current) => ({ ...current, [row.action_hash]: outcome }));
      notify(decision === "approve" ? `Đã duyệt ${outcome.tool} — ${outcome.status}` : `Đã từ chối ${outcome.tool}`);
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <PageShell width="narrow">
        <PageHead title="Hành động chờ duyệt" hint="Mỗi dòng là một lần gọi tool ghi mà agent đã dừng lại giữa chừng để chờ người quyết định. Duyệt là thực thi đúng lệnh đó ngay — hãy đọc kỹ tham số. Cũ nhất lên trước.">
        <button type="button" className="btn btn-secondary btn-icon" onClick={load} title="Tải lại">
          <RefreshCw size={16} />
        </button>
        </PageHead>

        <ErrorNote>{error}</ErrorNote>
        {loading && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}
        {!loading && rows.length === 0 && (
          <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>Không có hành động nào đang chờ.</div>
        )}

        <div className="ab-stack">
          {rows.map((row) => {
            const outcome = outcomes[row.action_hash];
            return (
              <div key={row.action_hash} className="card">
                <div className="ab-row">
                  <span className="ab-mono card-title" style={{ fontSize: "var(--text-md)" }}>{row.tool}</span>
                  <span className="ab-chip ab-mono">{row.agent_id} v{row.version}</span>
                  <span className="ab-chip">yêu cầu bởi {row.requested_by_employee_id}</span>
                  {row.approver_roles.map((role) => (
                    <span key={role} className="tag tag-outline">{role}</span>
                  ))}
                </div>

                <pre className="ab-code ab-scrollbar">{JSON.stringify(row.args, null, 2)}</pre>

                <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: 0, wordBreak: "break-all" }}>
                  {row.action_hash} · {new Date(row.created_at).toLocaleString()}
                </p>

                {outcome ? (
                  <div style={{ paddingTop: "var(--space-2)", borderTop: "1px solid var(--color-divider)" }}>
                    <p style={{ fontSize: "var(--text-sm)", margin: 0 }}>
                      Kết quả: <span className="ab-mono">{outcome.status}</span>
                    </p>
                    {outcome.reasons.length > 0 && (
                      <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: "4px 0 0" }}>{outcome.reasons.join(" · ")}</p>
                    )}
                  </div>
                ) : (
                  <>
                    <input
                      className="input"
                      placeholder="Lý do (tuỳ chọn, nhưng nên có khi từ chối)"
                      value={reasons[row.action_hash] || ""}
                      onChange={(event) => setReasons((current) => ({ ...current, [row.action_hash]: event.target.value }))}
                    />
                    <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                      <button
                        type="button"
                        className="btn btn-danger btn-sm"
                        onClick={() => decide(row, "reject")}
                        disabled={!!busy}
                      >
                        {busy === `${row.action_hash}:reject` ? <Spinner size={13} /> : <X size={13} />}Từ chối
                      </button>
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        onClick={() => decide(row, "approve")}
                        disabled={!!busy}
                      >
                        {busy === `${row.action_hash}:approve` ? <Spinner size={13} /> : <Check size={13} />}Duyệt & thực thi
                      </button>
                    </div>
                  </>
                )}
              </div>
            );
          })}
        </div>
      </PageShell>
  );
}
