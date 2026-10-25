import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, FileDiff, FlaskConical, ListChecks, MessageSquare, Pencil, PlayCircle, Power, RefreshCw, Send, ShieldCheck, TriangleAlert, Undo2 } from "lucide-react";
import ConfirmDialog from "../components/ConfirmDialog";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell, Spinner } from "../components/ui";
import { AgentMascot } from "../components/mascot";
import { useAgentActivity } from "../components/agent-presence";

/* ============================================================
   The road from a confirmed draft to the admin's approval queue.

   §13's gate (`src.agentspec.lifecycle.submit_for_approval`) opens on three
   conditions at once, and until this screen existed none of the three had a
   button: a version could be drafted and confirmed, then sat at `draft`
   forever while `GET /approvals/queue` — which lists `pending_approval` only
   — showed nothing and gave no hint why.

   So the tabs are not a menu, they are the order of the work: edit, rehearse
   in the sandbox (FR-36, ≥1 run), grade against the golden set (FR-33), ask
   for approval, then — once an approver has signed — deploy, serve traffic,
   and if it goes wrong, suspend or roll back. `Xin duyệt` states all three
   gate conditions up front and renders the 409's own list when the gate
   refuses, because "chưa đủ điều kiện" without saying which condition is the
   failure this whole screen exists to prevent.
   ============================================================ */

const TABS = [
  { id: "edit", label: "Sửa spec", icon: Pencil },
  { id: "sandbox", label: "Chat thử (sandbox)", icon: FlaskConical },
  { id: "goldenset", label: "Golden set", icon: ListChecks },
  { id: "submit", label: "Xin duyệt", icon: ShieldCheck },
  { id: "diff", label: "So sánh version", icon: FileDiff },
  { id: "lifecycle", label: "Vòng đời", icon: Power },
  { id: "production", label: "Chat production", icon: MessageSquare },
];

/* Same mapping `UC1Agents` uses for the catalog rows, so a version reads the
   same in the list it was opened from and on the screen it opened onto. */
const STATUS_TAGS = {
  draft: "tag tag-neutral",
  pending_approval: "tag tag-warn",
  approved: "tag tag-accent",
  deployed: "tag tag-accent",
  suspended: "tag tag-danger",
  retired: "tag tag-danger",
};

function StatusChip({ status }) {
  return <span className={STATUS_TAGS[status] || "tag tag-neutral"}>{status}</span>;
}

function money(value) {
  return `$${(value ?? 0).toFixed(4)}`;
}

/** One transcript entry — the node that ran, the tool it called if any, and
 *  what that cost. FR-37's "transcript sống (node, tool, chi phí)". */
function TranscriptStep({ step }) {
  return (
    <div className="ab-version-item">
      <span className="ab-chip ab-mono">{step.node}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        {step.tool && <p className="ab-mono" style={{ fontSize: "var(--text-xs)", margin: 0 }}>{step.tool}</p>}
        {step.args && Object.keys(step.args).length > 0 && (
          <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: "2px 0 0", wordBreak: "break-all" }}>
            {JSON.stringify(step.args)}
          </p>
        )}
        {step.result && (
          <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: "3px 0 0", lineHeight: 1.5, whiteSpace: "pre-wrap" }}>
            {step.result}
          </p>
        )}
      </div>
      <span className="ab-chip ab-mono">{Math.round(step.latency_ms)}ms</span>
      <span className="ab-chip ab-mono">{money(step.cost_usd)}</span>
    </div>
  );
}

export function SandboxTab({ agentId, version, runs, reloadRuns, notify }) {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [live, setLive] = useState(null); // { steps, final_answer, total_cost_usd, failed }

  useAgentActivity("processing", Boolean(live) && !live.final_answer && !live.failed, "agent đang chạy");
  const liveRef = useRef(null);

  async function send() {
    const text = message.trim();
    if (!text || busy) return;
    setBusy(true);
    setError("");
    liveRef.current = { steps: [], final_answer: null, total_cost_usd: 0, failed: null };
    setLive({ ...liveRef.current });
    try {
      await api.uc1Sandbox.chat(agentId, version, { message: text }, (event, data) => {
        const current = liveRef.current;
        if (event === "step") current.steps = [...current.steps, data];
        if (event === "done") {
          current.final_answer = data.final_answer;
          current.total_cost_usd = data.total_cost_usd;
        }
        if (event === "error") current.failed = data;
        setLive({ ...current });
      });
      setMessage("");
      // The run is what FR-38's gate counts, so the history has to be right
      // the moment the turn ends — the Xin duyệt tab reads this same list.
      await reloadRuns();
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ab-stack" style={{ gap: 14 }}>
      <div className="card">
        <p className="card-title">Chạy thử một lượt</p>
        <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
          Chạy trên thread riêng của sandbox, bằng chính danh tính của bạn. Cần <b>ít nhất một lượt</b> mới xin duyệt được (FR-38).
        </p>
        <textarea
          className="input"
          style={{ minHeight: 70 }}
          placeholder="Khách nào đang quá hạn trên 30 ngày?"
          value={message}
          disabled={busy}
          onChange={(event) => setMessage(event.target.value)}
        />
        <ErrorNote>{error}</ErrorNote>
        <button type="button" className="btn btn-primary" style={{ alignSelf: "flex-start" }} onClick={send} disabled={busy || !message.trim()}>
          {busy ? <><Spinner size={14} />Đang chạy…</> : <><Send size={14} />Gửi</>}
        </button>
      </div>

      {live && (
        <div className="card">
          <p className="card-title">Lượt vừa chạy</p>
          {live.steps.length === 0 && !live.final_answer && <div className="ab-empty"><AgentMascot state="processing" size={84} /></div>}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {live.steps.map((step, index) => <TranscriptStep key={index} step={step} />)}
          </div>
          {live.failed && (
            <ErrorNote>{live.failed.message || live.failed.code || "Lượt chạy bị dừng"}</ErrorNote>
          )}
          {live.final_answer && (
            <div style={{ paddingTop: "var(--space-3)", borderTop: "1px solid var(--color-divider)" }}>
              <p style={{ fontSize: "var(--text-md)", margin: 0, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{live.final_answer}</p>
              <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: "6px 0 0" }}>
                tổng chi phí {money(live.total_cost_usd)}
              </p>
            </div>
          )}
        </div>
      )}

      <div>
        <p className="card-title">Lịch sử ({runs.length} lượt)</p>
        {runs.length === 0 && <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>Chưa có lượt chạy nào.</div>}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {runs.map((run) => (
            <div key={run.id} className="card">
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <span className="ab-chip ab-mono">#{run.id}</span>
                <p className="card-title" style={{ flex: "1 1 200px", margin: 0 }}>{run.message}</p>
                <span className="ab-chip ab-mono">{run.status}</span>
                <span className="ab-chip ab-mono">{money(run.total_cost_usd)}</span>
              </div>
              {run.final_answer && (
                <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.55, whiteSpace: "pre-wrap" }}>
                  {run.final_answer}
                </p>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function GoldenSetTab({ agentId, version, result, setResult, notify }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function run() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const outcome = await api.uc1Approvals.runGoldenSet(agentId, version);
      setResult(outcome);
      notify(`Golden set: ${outcome.passed_count}/${outcome.total_count}`);
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ab-stack" style={{ gap: 14 }}>
      <div className="card">
        <p className="card-title">Chấm điểm golden set</p>
        <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
          Không bao giờ chạy ngầm — cổng xin duyệt chỉ <b>đọc</b> kết quả lần chạy gần nhất, không tự kích hoạt. Chưa chạy lần nào
          thì pass rate tính là 0 và cổng sẽ chặn (FR-33).
        </p>
        <ErrorNote>{error}</ErrorNote>
        <button type="button" className="btn btn-primary" style={{ alignSelf: "flex-start" }} onClick={run} disabled={busy}>
          {busy ? <><Spinner size={14} />Đang chấm…</> : <><PlayCircle size={14} />Chạy golden set</>}
        </button>
      </div>

      {result && (
        <>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            <div className="ab-metric">
              <div className="val">{result.passed_count}/{result.total_count}</div>
              <div className="lbl">case đạt</div>
            </div>
            <div className="ab-metric">
              <div className="val">{(result.pass_rate * 100).toFixed(1)}%</div>
              <div className="lbl">pass rate</div>
            </div>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {result.cases.map((entry) => (
              <div key={entry.case_id} className="ab-version-item">
                <span className={entry.passed ? "tag tag-accent ab-mono" : "tag tag-danger ab-mono"}>
                  {entry.passed ? "pass" : "fail"}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <p className="ab-mono" style={{ fontSize: "var(--text-xs)", margin: 0 }}>{entry.case_id}</p>
                  <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: "2px 0 0", lineHeight: 1.5 }}>{entry.detail}</p>
                </div>
                <span className="ab-chip">{entry.group}</span>
                <span className="ab-chip ab-mono">{entry.judge}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export function SubmitTab({ agentId, version, status, runs, goldenSet, onSubmitted, notify }) {
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState([]);
  const [error, setError] = useState("");

  async function submit() {
    if (busy) return;
    setBusy(true);
    setError("");
    setProblems([]);
    try {
      const outcome = await api.uc1Approvals.submitForApproval(agentId, version);
      notify(`Đã chuyển sang ${outcome.status}`);
      onSubmitted(outcome.status);
    } catch (err) {
      // A 409 is the gate refusing with every failing condition at once —
      // spec.md §13's "thiếu bất kỳ điều kiện nào thì ở lại draft, không có
      // nhánh lỗi riêng". Rendered as the list it is, not flattened to a toast.
      if (err.status === 409 && Array.isArray(err.body?.detail)) setProblems(err.body.detail);
      else setError(err.message);
      notify("Chưa đủ điều kiện xin duyệt", "error");
    } finally {
      setBusy(false);
    }
  }

  const alreadyMoved = status !== "draft";

  return (
    <div className="ab-stack" style={{ gap: 14 }}>
      <div className="card">
        <p className="card-title">Ba điều kiện của cổng</p>
        <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
          Cả ba phải đạt cùng lúc thì version mới chuyển <span className="ab-mono">draft → pending_approval</span> và xuất hiện
          trong hàng đợi duyệt của admin.
        </p>
        <ul style={{ margin: 0, paddingLeft: "var(--space-4)", fontSize: "var(--text-sm)", lineHeight: 1.8 }}>
          <li>Form đã xác nhận (HITL #1)</li>
          <li>Có ít nhất một lượt sandbox — hiện có <b>{runs.length}</b></li>
          <li>
            Golden set đạt <span className="ab-mono">evaluation.min_pass_rate</span> —
            {goldenSet ? <b> {(goldenSet.pass_rate * 100).toFixed(1)}%</b> : <b> chưa chấm lần nào</b>}
          </li>
        </ul>
      </div>

      {problems.length > 0 && (
        <div className="card card-warn" style={{ gap: 4 }}>
          <p className="card-title" style={{ display: "flex", gap: 7, alignItems: "center", margin: 0 }}>
            <TriangleAlert size={15} />Cổng từ chối — còn thiếu {problems.length} điều kiện
          </p>
          {problems.map((problem, index) => (
            <p key={index} style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.55 }}>
              <span className="ab-mono text-muted">{problem.code}</span> — {problem.message}
            </p>
          ))}
        </div>
      )}

      <ErrorNote>{error}</ErrorNote>

      {alreadyMoved ? (
        <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>
          Version này đang ở trạng thái <span className="ab-mono" style={{ margin: "0 4px" }}>{status}</span> — chỉ version
          <span className="ab-mono" style={{ margin: "0 4px" }}>draft</span> mới xin duyệt được.
        </div>
      ) : (
        <button type="button" className="btn btn-primary" style={{ alignSelf: "flex-start" }} onClick={submit} disabled={busy}>
          {busy ? <><Spinner size={14} />Đang gửi…</> : <><ShieldCheck size={14} />Xin duyệt version {version}</>}
        </button>
      )}
    </div>
  );
}

/** FR-13's edit. A JSON editor rather than seven typed forms on purpose: the
 *  backend merges whatever top-level fields the body names into the current
 *  version and re-validates the whole thing, so a partial document is the
 *  actual contract — and a form that submitted every field would silently
 *  re-assert values the editor never looked at. */
export function EditTab({ agentId, version, onEdited, notify }) {
  const [body, setBody] = useState('{\n  "metadata": {\n    "description": ""\n  }\n}');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function apply() {
    if (busy) return;
    let payload;
    try {
      payload = JSON.parse(body);
    } catch (err) {
      setError(`JSON không hợp lệ: ${err.message}`);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const row = await api.uc1Agents.patchVersion(agentId, version, payload);
      notify(`Đã tạo version ${row.version}`);
      onEdited(row.version);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <p className="card-title">Sửa version {version}</p>
      <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
        Chỉ ghi những trường bạn nêu ở đây. Version {version} <b>không bị sửa</b> — thay đổi luôn tạo ra version {version + 1}
        mới ở trạng thái draft. Không sửa được <span className="ab-mono">id</span>, <span className="ab-mono">version</span>,
        <span className="ab-mono"> status</span>, và không gỡ được guardrail baseline.
      </p>
      <textarea
        className="input ab-mono"
        style={{ minHeight: 180, fontSize: "var(--text-xs)" }}
        spellCheck={false}
        value={body}
        disabled={busy}
        onChange={(event) => setBody(event.target.value)}
      />
      <ErrorNote>{error}</ErrorNote>
      <button type="button" className="btn btn-primary" style={{ alignSelf: "flex-start" }} onClick={apply} disabled={busy}>
        {busy ? <><Spinner size={14} />Đang lưu…</> : <><Pencil size={14} />Lưu thành version {version + 1}</>}
      </button>
    </div>
  );
}

/** FR-31's field-level diff — the same data the approval dossier reads, put
 *  where the person *making* the edits can see it too. */
export function DiffTab({ agentId, version, versions }) {
  const others = versions.filter((row) => row.version !== version);
  const [against, setAgainst] = useState(() => (version > 1 ? version - 1 : null));
  const [rows, setRows] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    setBusy(true);
    setError("");
    try {
      setRows(await api.uc1Approvals.diff(agentId, version, against));
    } catch (err) {
      setError(err.message);
      setRows(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ab-stack" style={{ gap: 14 }}>
      <div className="card">
        <p className="card-title">So version {version} với</p>
        {others.length === 0 ? (
          <p className="text-muted" style={{ margin: 0 }}>Agent này mới có một version.</p>
        ) : (
          <>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {others.map((row) => (
                <button
                  key={row.version}
                  type="button"
                  className={`ab-chip ab-mono${row.version === against ? " active" : ""}`}
                  onClick={() => setAgainst(row.version)}
                >
                  v{row.version}
                </button>
              ))}
            </div>
            <button type="button" className="btn btn-primary" style={{ alignSelf: "flex-start" }} onClick={load} disabled={busy || against == null}>
              {busy ? <><Spinner size={14} />Đang so…</> : <><FileDiff size={14} />So sánh</>}
            </button>
          </>
        )}
        <ErrorNote>{error}</ErrorNote>
      </div>

      {rows && rows.length === 0 && <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>Hai version này giống hệt nhau.</div>}
      {rows && rows.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {rows.map((row, index) => (
            <div key={index} className="card">
              <p className="ab-mono card-title" style={{ fontSize: "var(--text-md)", margin: 0 }}>{row.path}</p>
              <p className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "#b32828", margin: 0, wordBreak: "break-all" }}>
                − {JSON.stringify(row.before)}
              </p>
              <p className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "var(--color-accent)", margin: 0, wordBreak: "break-all" }}>
                + {JSON.stringify(row.after)}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** FR-34/FR-35/FR-51 — the three moves that touch what is actually serving
 *  traffic. Deploy and rollback both reprovision the agent's Postgres role;
 *  suspend revokes it. All three are consequential enough to confirm first. */
export function LifecycleTab({ agentId, version, status, versions, onChanged, notify }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [problems, setProblems] = useState([]);
  const [pending, setPending] = useState(null); // { kind, version }

  const deployed = versions.find((row) => row.status === "deployed") || null;

  async function run(kind, targetVersion) {
    setBusy(kind);
    setError("");
    setProblems([]);
    try {
      if (kind === "deploy") await api.uc1Approvals.deploy(agentId, targetVersion);
      if (kind === "suspend") await api.uc1Approvals.suspend(agentId);
      if (kind === "rollback") await api.uc1Approvals.rollback(agentId, targetVersion);
      notify(kind === "deploy" ? "Đã deploy" : kind === "suspend" ? "Đã dừng agent" : `Đã rollback về v${targetVersion}`);
      await onChanged();
    } catch (err) {
      if (err.status === 409 && Array.isArray(err.body?.detail)) setProblems(err.body.detail);
      else setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy("");
      setPending(null);
    }
  }

  return (
    <div className="ab-stack" style={{ gap: 14 }}>
      <div className="card">
        <p className="card-title">Đang phục vụ</p>
        {deployed ? (
          <p style={{ margin: 0 }}>
            Version <b>v{deployed.version}</b> đang <span className="ab-mono">deployed</span>. Mỗi agent chỉ có tối đa một
            version chạy thật.
          </p>
        ) : (
          <p className="text-muted" style={{ margin: 0 }}>Chưa version nào được deploy.</p>
        )}
      </div>

      {problems.length > 0 && (
        <div className="card card-warn" style={{ gap: 4 }}>
          <p className="card-title" style={{ display: "flex", gap: 7, alignItems: "center", margin: 0 }}>
            <TriangleAlert size={15} />Chuyển trạng thái bị từ chối
          </p>
          {problems.map((problem, index) => (
            <p key={index} style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.55 }}>
              <span className="ab-mono text-muted">{problem.code}</span> — {problem.message}
            </p>
          ))}
        </div>
      )}
      <ErrorNote>{error}</ErrorNote>

      <div className="card">
        <p className="card-title">Deploy version {version}</p>
        <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
          Chỉ hợp lệ từ <span className="ab-mono">approved</span> — version này đang <span className="ab-mono">{status}</span>.
          Deploy đồng thời cấp Postgres role riêng cho agent theo đúng <span className="ab-mono">connections[]</span> của version.
        </p>
        <button
          type="button"
          className="btn btn-primary"
          style={{ alignSelf: "flex-start" }}
          onClick={() => setPending({ kind: "deploy", version })}
          disabled={!!busy || status !== "approved"}
        >
          {busy === "deploy" ? <><Spinner size={14} />Đang deploy…</> : <><Power size={14} />Deploy v{version}</>}
        </button>
      </div>

      <div className="card">
        <p className="card-title">Kill switch</p>
        <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
          Dừng ngay version đang chạy và <b>thu hồi quyền Postgres của nó trước khi lệnh trả về</b> — không phải job chạy nền.
        </p>
        <button
          type="button"
          className="btn btn-danger"
          style={{ alignSelf: "flex-start" }}
          onClick={() => setPending({ kind: "suspend" })}
          disabled={!!busy || !deployed}
        >
          {busy === "suspend" ? <><Spinner size={14} />Đang dừng…</> : <><Power size={14} />Dừng agent</>}
        </button>
      </div>

      <div className="card">
        <p className="card-title">Rollback</p>
        <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
          Trỏ bản đang phục vụ về một version cũ đã <span className="ab-mono">approved</span> hoặc{" "}
          <span className="ab-mono">suspended</span>. Không tạo version mới và không cần ký duyệt lại.
        </p>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {versions
            .filter((row) => row.status === "approved" || row.status === "suspended")
            .map((row) => (
              <button
                key={row.version}
                type="button" className="btn btn-secondary btn-sm"
                onClick={() => setPending({ kind: "rollback", version: row.version })}
                disabled={!!busy}
              >
                <Undo2 size={13} />về v{row.version}
              </button>
            ))}
          {versions.every((row) => row.status !== "approved" && row.status !== "suspended") && (
            <p className="text-muted" style={{ margin: 0 }}>Chưa có version nào đủ điều kiện rollback.</p>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={!!pending}
        title={
          !pending
            ? ""
            : pending.kind === "deploy"
              ? "Deploy version này?"
              : pending.kind === "suspend"
                ? "Dừng agent ngay?"
                : `Rollback về v${pending.version}?`
        }
        body={
          pending && pending.kind === "suspend"
            ? "Version đang chạy sẽ dừng phục vụ và quyền Postgres của nó bị thu hồi ngay lập tức."
            : "Thao tác này cấp lại Postgres role của agent theo connections[] của version đích."
        }
        confirmLabel={pending && pending.kind === "suspend" ? "Dừng" : "Tiếp tục"}
        busy={!!busy}
        onCancel={() => setPending(null)}
        onConfirm={() => pending && run(pending.kind, pending.version)}
      />
    </div>
  );
}

/** UC1-19: talking to what is actually serving. Not the sandbox — this
 *  endpoint resolves the `deployed` version itself, so there is no version to
 *  pass and no way to reach an unapproved spec through it. */
export function ProductionChatTab({ agentId, deployedVersion, notify }) {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [live, setLive] = useState(null);
  const liveRef = useRef(null);

  async function send() {
    const text = message.trim();
    if (!text || busy) return;
    setBusy(true);
    setError("");
    liveRef.current = { steps: [], final_answer: null, total_cost_usd: 0, failed: null };
    setLive({ ...liveRef.current });
    try {
      await api.uc1Chat.deployed(agentId, { message: text }, (event, data) => {
        const current = liveRef.current;
        if (event === "step") current.steps = [...current.steps, data];
        if (event === "done") {
          current.final_answer = data.final_answer;
          current.total_cost_usd = data.total_cost_usd;
        }
        if (event === "error") current.failed = data;
        setLive({ ...current });
      });
      setMessage("");
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  }

  if (!deployedVersion) {
    return (
      <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>
        Agent này chưa có version nào <span className="ab-mono" style={{ margin: "0 4px" }}>deployed</span> — chưa chat production được.
      </div>
    );
  }

  return (
    <div className="ab-stack" style={{ gap: 14 }}>
      <div className="card">
        <p className="card-title">Chat với bản đang chạy (v{deployedVersion})</p>
        <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.6 }}>
          Chạy đúng version đang phục vụ, bằng danh tính của bạn — mọi hành động ghi vẫn qua PEP và có thể bị đưa vào hàng đợi duyệt.
        </p>
        <textarea
          className="input"
          style={{ minHeight: 70 }}
          placeholder="Khách nào đang quá hạn trên 30 ngày?"
          value={message}
          disabled={busy}
          onChange={(event) => setMessage(event.target.value)}
        />
        <ErrorNote>{error}</ErrorNote>
        <button type="button" className="btn btn-primary" style={{ alignSelf: "flex-start" }} onClick={send} disabled={busy || !message.trim()}>
          {busy ? <><Spinner size={14} />Đang chạy…</> : <><Send size={14} />Gửi</>}
        </button>
      </div>

      {live && (
        <div className="card">
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {live.steps.map((step, index) => <TranscriptStep key={index} step={step} />)}
          </div>
          {live.failed && (
            <ErrorNote>{live.failed.message || live.failed.code || "Lượt chạy bị dừng"}</ErrorNote>
          )}
          {live.final_answer && (
            <div style={{ paddingTop: "var(--space-3)", borderTop: "1px solid var(--color-divider)" }}>
              <p style={{ fontSize: "var(--text-md)", margin: 0, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{live.final_answer}</p>
              <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: "6px 0 0" }}>
                tổng chi phí {money(live.total_cost_usd)}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function UC1Workbench({ agentId, back, notify }) {
  const [tab, setTab] = useState("sandbox");
  const [versions, setVersions] = useState([]);
  const [version, setVersion] = useState(null);
  const [runs, setRuns] = useState([]);
  const [goldenSet, setGoldenSet] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadVersions = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const rows = await api.uc1Agents.specVersions(agentId);
      setVersions(rows);
      // Newest by default: it is the one an edit just produced and the only
      // one still at `draft` after a submit moved an earlier one on.
      setVersion((current) => (current && rows.some((row) => row.version === current) ? current : rows[rows.length - 1]?.version ?? null));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  const reloadRuns = useCallback(async () => {
    if (version == null) return;
    try {
      setRuns(await api.uc1Sandbox.runs(agentId, version));
    } catch {
      setRuns([]);
    }
  }, [agentId, version]);

  useEffect(() => { loadVersions(); }, [loadVersions]);
  useEffect(() => { reloadRuns(); }, [reloadRuns]);
  // Evidence is per-version: a golden-set score read on version 2 says nothing
  // about version 3, and showing it there would be showing a lie.
  useEffect(() => { setGoldenSet(null); }, [version]);

  const current = versions.find((row) => row.version === version) || null;

  return (
    <PageShell width="narrow">
        <PageHead title={current ? current.name : agentId}>
          <button type="button" className="btn btn-secondary btn-icon" onClick={loadVersions} title="Tải lại">
            <RefreshCw size={15} />
          </button>
          <button type="button" className="btn btn-secondary" onClick={back}><ArrowLeft size={14} />Danh sách</button>
        </PageHead>
        <div className="ab-row" style={{ marginBottom: "var(--space-4)" }}>
          {current && <StatusChip status={current.status} />}
          <span className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)" }}>{agentId}</span>
        </div>

        {versions.length > 0 && (
          <div className="ab-row" style={{ gap: 6, marginBottom: 4 }}>
            <span className="ab-label" style={{ margin: 0 }}>Version</span>
            {versions.map((row) => (
              <button
                key={row.version}
                type="button"
                className={`ab-chip ab-mono${row.version === version ? " active" : ""}`}
                onClick={() => setVersion(row.version)}
              >
                v{row.version} · {row.status}
              </button>
            ))}
          </div>
        )}

        <div className="ab-tabs" style={{ padding: 0, margin: "18px 0" }}>
          {TABS.map((entry) => (
            <button key={entry.id} type="button" className={`ab-tab${tab === entry.id ? " active" : ""}`} onClick={() => setTab(entry.id)}>
              <entry.icon size={14} />{entry.label}
            </button>
          ))}
        </div>

        <ErrorNote>{error}</ErrorNote>
        {loading && <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}><Spinner size={18} /></div>}
        {!loading && version == null && <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>Agent này chưa có version nào.</div>}

        {!loading && version != null && (
          <>
            {tab === "sandbox" && (
              <SandboxTab agentId={agentId} version={version} runs={runs} reloadRuns={reloadRuns} notify={notify} />
            )}
            {tab === "goldenset" && (
              <GoldenSetTab agentId={agentId} version={version} result={goldenSet} setResult={setGoldenSet} notify={notify} />
            )}
            {tab === "submit" && (
              <SubmitTab
                agentId={agentId}
                version={version}
                status={current ? current.status : "draft"}
                runs={runs}
                goldenSet={goldenSet}
                onSubmitted={loadVersions}
                notify={notify}
              />
            )}
            {tab === "edit" && (
              <EditTab
                agentId={agentId}
                version={version}
                notify={notify}
                onEdited={async (newVersion) => { await loadVersions(); setVersion(newVersion); }}
              />
            )}
            {tab === "diff" && <DiffTab agentId={agentId} version={version} versions={versions} />}
            {tab === "lifecycle" && (
              <LifecycleTab
                agentId={agentId}
                version={version}
                status={current ? current.status : "draft"}
                versions={versions}
                onChanged={loadVersions}
                notify={notify}
              />
            )}
            {tab === "production" && (
              <ProductionChatTab
                agentId={agentId}
                deployedVersion={(versions.find((row) => row.status === "deployed") || {}).version || null}
                notify={notify}
              />
            )}
          </>
        )}
      </PageShell>
  );
}
