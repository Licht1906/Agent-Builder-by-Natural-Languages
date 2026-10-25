import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, RefreshCw, ChevronRight, CheckCircle2, AlertTriangle } from "lucide-react";
import * as api from "../lib/api";
import { useLang } from "../i18n";
import { ErrorNote, PageHead, PageShell, Spinner, StatusBadge } from "../components/ui";

/* ============================================================
   UC1-16 (#132) — the Enterprise approval surface: queue, diff, transcript, all
   three read together in one screen so an `agent_admin` never has to
   imagine what a version does from its spec alone (spec.md §4.5's own
   argument for why sandbox chat exists at all).
   ============================================================ */

export function UC1ApprovalQueue({ openVersion }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setRows(await api.uc1Approvals.queue());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <PageShell width="narrow">
        <PageHead title="Hàng chờ duyệt Enterprise" hint="Every version waiting on HITL #2 — oldest first.">
        <button type="button" className="btn btn-secondary btn-icon" onClick={load} title="Tải lại">
          <RefreshCw size={16} />
        </button>
        </PageHead>

        {loading && <div className="ab-empty"><Spinner /></div>}
        <ErrorNote>{error}</ErrorNote>

        {!loading && !error && rows.length === 0 && (
          <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>Không có version nào đang chờ duyệt.</div>
        )}

        <div className="ab-stack">
          {!loading && rows.map((row) => (
            <div
              key={`${row.agent_id}:${row.version}`}
              className="card"
              role="button"
              tabIndex={0}
              style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 16, cursor: "pointer", flexWrap: "wrap" }}
              onClick={() => openVersion(row.agent_id, row.version)}
              onKeyDown={(event) => event.key === "Enter" && openVersion(row.agent_id, row.version)}
            >
              <div style={{ minWidth: 0 }}>
                <div className="card-title" style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  {row.name}
                  {row.high_risk_tools.length > 0 && (
                    <span className="tag tag-warn" title={`Xin quyền ghi: ${row.high_risk_tools.join(", ")}`}>
                      <AlertTriangle size={12} />{row.high_risk_tools.join(", ")}
                    </span>
                  )}
                </div>
                <div className="text-muted" style={{ fontSize: "var(--text-sm)", marginTop: 3 }}>
                  {row.agent_id} · v{row.version} · chủ sở hữu {row.owner_email}
                </div>
              </div>
              <div className="ab-row" style={{ gap: 14, fontSize: "var(--text-xs)" }}>
                <span className="ab-mono" title="Golden set pass rate">
                  {row.eval_pass_rate == null ? "chưa eval" : `${Math.round(row.eval_pass_rate * 100)}%`}
                </span>
                <span className="ab-mono" title="Sandbox runs">{row.sandbox_run_count} sandbox</span>
                <span className="ab-mono" title="Approval quorum">{row.approvals_recorded}/{row.approval_quorum} duyệt</span>
                <ChevronRight size={16} />
              </div>
            </div>
          ))}
        </div>
      </PageShell>
  );
}

function DiffTable({ diffs }) {
  if (!diffs) return <p className="text-muted">Không có version trước để so sánh.</p>;
  if (diffs.length === 0) return <p className="text-muted">Không có gì thay đổi.</p>;
  return (
    <div className="ab-scrollbar" style={{ overflowX: "auto" }}>
      <table className="table">
        <thead><tr><th>Trường</th><th>Trước</th><th>Sau</th></tr></thead>
        <tbody>
          {diffs.map((d) => (
            <tr key={d.path}>
              <td className="ab-mono">{d.path}</td>
              <td className="ab-mono" style={{ color: "#b32828" }}>{JSON.stringify(d.before)}</td>
              <td className="ab-mono" style={{ color: "var(--color-accent)" }}>{JSON.stringify(d.after)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TranscriptList({ run }) {
  if (!run) return <p className="text-muted">Chưa có lượt chat thử nào (FR-38 sẽ chặn xin duyệt).</p>;
  return (
    <div>
      <p style={{ margin: "0 0 8px" }}>
        <strong>Câu hỏi:</strong> {run.message} — <strong>Trả lời:</strong> {run.final_answer || "(không có, vượt ngân sách)"}
      </p>
      <ol className="text-muted" style={{ fontSize: "var(--text-sm)", paddingLeft: "var(--space-4)", margin: 0 }}>
        {run.transcript.map((step, i) => (
          <li key={i} className="ab-mono">
            [{step.node}] {step.tool ? `${step.tool}(${JSON.stringify(step.args)})` : ""} → {step.result}
          </li>
        ))}
      </ol>
    </div>
  );
}

function EvalCaseTable({ run }) {
  if (!run) return <p className="text-muted">Chưa chạy golden set (FR-33 sẽ chặn xin duyệt).</p>;
  return (
    <div>
      <p style={{ margin: "0 0 8px" }}>
        <strong>{run.passed_count}/{run.total_count}</strong> case đạt ({Math.round(run.pass_rate * 100)}%)
      </p>
      <div className="ab-scrollbar" style={{ overflowX: "auto" }}>
        <table className="table">
          <thead><tr><th>Case</th><th>Nhóm</th><th>Kết quả</th><th>Chi tiết</th></tr></thead>
          <tbody>
            {run.cases.map((c) => (
              <tr key={c.case_id}>
                <td className="ab-mono">{c.case_id}</td>
                <td>{c.group}</td>
                <td style={{ color: c.passed ? "var(--color-accent)" : "#b32828" }}>{c.passed ? "Đạt" : "Không đạt"}</td>
                <td style={{ fontSize: "var(--text-sm)" }}>{c.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function UC1ApprovalDossier({ agentId, version, back, notify }) {
  const { t } = useLang();
  const [dossier, setDossier] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [approving, setApproving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setDossier(await api.uc1Approvals.dossier(agentId, version));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [agentId, version]);

  useEffect(() => { load(); }, [load]);

  const approve = async () => {
    setApproving(true);
    try {
      const result = await api.uc1Approvals.approve(agentId, version);
      notify(
        result.status === "approved"
          ? "Đã duyệt — đủ quorum."
          : `Đã ghi nhận — ${result.approvals_recorded}/${result.approval_quorum} chữ ký.`
      );
      await load();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setApproving(false);
    }
  };

  if (loading) return <div className="ab-empty" style={{ paddingTop: "20vh" }}><Spinner size={22} /></div>;

  return (
    <PageShell width="narrow">
        <PageHead title={`${agentId} · v${version}`}>
          <button type="button" className="btn btn-secondary" onClick={back}><ArrowLeft size={14} />Hàng chờ</button>
        </PageHead>
        {dossier && (
          <div className="ab-row" style={{ marginBottom: "var(--space-4)" }}>
            <StatusBadge status={dossier.status} />
            {dossier.high_risk_tools.length > 0 && (
              <span className="tag tag-warn">
                <AlertTriangle size={12} />Xin quyền ghi: {dossier.high_risk_tools.join(", ")}
              </span>
            )}
          </div>
        )}

        <ErrorNote>{error}</ErrorNote>
        {dossier && (
          <div className="ab-stack" style={{ gap: 16 }}>
            <section className="card">
              <h2 className="card-title">Diff so với version trước</h2>
              <DiffTable diffs={dossier.diff_against_previous} />
            </section>

            <section className="card">
              <h2 className="card-title">Lượt chat thử gần nhất (sandbox)</h2>
              <TranscriptList run={dossier.sandbox_run} />
            </section>

            <section className="card">
              {/* Not "Golden set (DeepEval)": nothing here runs the `deepeval`
                  package. The grader is this platform's own single-call LLM
                  judge wearing DeepEval's vocabulary — see
                  `src/evals/uc1_deepeval_runner.py` for why the two numbers are
                  not comparable. The heading is the one place an approver reads
                  before trusting the score, so it says which of the two it is. */}
              <h2 className="card-title">{t("Golden set (DeepEval-style LLM judge)")}</h2>
              <EvalCaseTable run={dossier.eval_run} />
            </section>

            <section className="card">
              <h2 className="card-title">Duyệt</h2>
              <p className="text-muted" style={{ margin: 0 }}>
                {dossier.approvals_recorded}/{dossier.approval_quorum} chữ ký cần thiết.
              </p>
              <button type="button" className="btn btn-primary" style={{ alignSelf: "flex-start" }} onClick={approve} disabled={approving || dossier.status !== "pending_approval"}>
                <CheckCircle2 size={14} />{approving ? "Đang ghi..." : "Duyệt version này"}
              </button>
            </section>
          </div>
        )}
      </PageShell>
  );
}
