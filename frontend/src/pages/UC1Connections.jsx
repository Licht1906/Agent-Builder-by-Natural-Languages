import { useCallback, useEffect, useState } from "react";
import { PlugZap, RefreshCw, Plus } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell, Spinner } from "../components/ui";
import AddMcpServer, { describeLifetime } from "../components/AddMcpServer";
import { AgentMascot } from "../components/mascot";

/* ============================================================
   UC1-08 (#122), FR-20..FR-23 — connections.

   The one screen on this platform that takes a real credential. It goes
   straight to Vault and is never read back: every response here carries only
   `credential_ref` (`vault://conn/<name>`), which is the value that gets
   pasted into a spec's `connections[].credential_ref`. The meta-agent never
   sees the secret, which is the whole reason this form exists apart from the
   spec editor.

   `probe` is the other half. A connection nobody dry-ran is a connection
   whose credential might be wrong and whose `allowed_objects` might name
   views that do not exist — and the place to find that out is here, before a
   spec depends on it and a compile fails for reasons that look unrelated.
   ============================================================ */

const TYPES = ["postgres", "rest", "smtp"];

/* The design system's own tags rather than a bespoke pill — "connected" and
   "failed" are the same kind of fact as a version status, and reading them in
   the same vocabulary is the point of having one. */
const PROBE_TAGS = {
  connected: "tag tag-accent",
  failed: "tag tag-danger",
  never: "tag tag-neutral",
};

function ProbeBadge({ status }) {
  return <span className={PROBE_TAGS[status] || "tag tag-neutral"}>{status}</span>;
}

function CreateForm({ onCreated, notify }) {
  const [name, setName] = useState("");
  const [type, setType] = useState("postgres");
  const [credential, setCredential] = useState("");
  const [allowedObjects, setAllowedObjects] = useState("");
  const [allowedHosts, setAllowedHosts] = useState("");
  const [readOnly, setReadOnly] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const lines = (value) => value.split(/[\n,]/).map((entry) => entry.trim()).filter(Boolean);

  async function create() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const created = await api.uc1Connections.create({
        name: name.trim(),
        type,
        credential,
        allowed_objects: lines(allowedObjects),
        allowed_hosts: lines(allowedHosts),
        read_only: readOnly,
      });
      notify(`Đã tạo ${created.credential_ref}`);
      // Cleared immediately: this component is the only place the secret ever
      // existed in the browser, and it has no reason to outlive the request.
      setCredential("");
      setName("");
      setAllowedObjects("");
      setAllowedHosts("");
      await onCreated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <p className="card-title">Thêm kết nối</p>

      <div className="field">
        <label>Tên</label>
        <input className="input" value={name} placeholder="erp_readonly" spellCheck={false} disabled={busy} onChange={(e) => setName(e.target.value)} />
      </div>
      <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: 0 }}>
        Chữ thường, số, gạch dưới. Thành <span className="ab-mono">vault://conn/&lt;tên&gt;</span>.
      </p>

      <div className="field">
        <label>Loại</label>
        <div className="ab-row" style={{ gap: 6 }}>
          {TYPES.map((entry) => (
            <button
              key={entry}
              type="button"
              className={`ab-chip ab-mono${entry === type ? " active" : ""}`}
              onClick={() => setType(entry)}
              disabled={busy}
            >
              {entry}
            </button>
          ))}
        </div>
      </div>

      <label className="ab-label" style={{ margin: 0 }}>Credential</label>
      <input
        className="input"
        type="password"
        value={credential}
        placeholder="postgresql://user:pass@host:5432/erp"
        spellCheck={false}
        disabled={busy}
        onChange={(e) => setCredential(e.target.value)}
      />
      <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: 0, lineHeight: 1.5 }}>
        Đi thẳng vào Vault, <b>không</b> được lưu hay hiển thị lại ở đây. Muốn đổi thì tạo kết nối mới.
      </p>

      <label className="ab-label" style={{ margin: 0 }}>Allowed objects</label>
      <textarea
        className="input ab-mono"
        style={{ minHeight: 56, fontSize: "var(--text-xs)" }}
        value={allowedObjects}
        placeholder={"v_ar_open\nv_customer_safe"}
        spellCheck={false}
        disabled={busy}
        onChange={(e) => setAllowedObjects(e.target.value)}
      />

      <label className="ab-label" style={{ margin: 0 }}>Allowed hosts</label>
      <textarea
        className="input ab-mono"
        style={{ minHeight: 44, fontSize: "var(--text-xs)" }}
        value={allowedHosts}
        placeholder="smtp.internal.corp"
        spellCheck={false}
        disabled={busy}
        onChange={(e) => setAllowedHosts(e.target.value)}
      />

      <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: "var(--text-sm)" }}>
        <input type="checkbox" checked={readOnly} disabled={busy} onChange={(e) => setReadOnly(e.target.checked)} />
        Chỉ đọc (read_only)
      </label>

      <ErrorNote>{error}</ErrorNote>
      <button type="button" className="btn btn-primary" style={{ alignSelf: "flex-start" }} onClick={create} disabled={busy || !name.trim() || !credential}>
        {busy ? <><Spinner size={14} />Đang lưu…</> : <><Plus size={14} />Tạo kết nối</>}
      </button>
    </div>
  );
}

/* The person's own Google account — per-user OAuth, not a Vault credential.
   Its own card because the mental model differs: a Vault connection is "the
   company's system, wired by IT"; this is "my account, and every agent I own
   acts as me on Gmail/Drive". The flow leaves the app (accounts.google.com)
   and comes back with `?google=...` in the query string, which this card reads
   once and then cleans off the URL. */
function GoogleAccountCard({ notify }) {
  const [link, setLink] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const listed = await api.userConnections.list();
      setLink(listed.find((entry) => entry.provider === "google") || null);
    } catch {
      setLink(null);
    }
  }, []);

  useEffect(() => {
    const outcome = new URLSearchParams(window.location.search).get("google");
    if (outcome) {
      notify(
        outcome === "connected"
          ? "Đã kết nối tài khoản Google — các agent của bạn giờ đọc/ghi Gmail và Drive bằng tài khoản này"
          : "Kết nối Google chưa thành công — bạn thử lại, và chấp thuận toàn bộ quyền được hỏi",
        outcome === "connected" ? "ok" : "error"
      );
      window.history.replaceState(null, "", window.location.pathname);
    }
    load();
  }, [load, notify]);

  async function connect() {
    setBusy(true);
    try {
      const { url } = await api.userConnections.startGoogle();
      window.location.assign(url);
    } catch (err) {
      notify(err.message, "error");
      setBusy(false);
    }
  }

  async function disconnect() {
    setBusy(true);
    try {
      await api.userConnections.disconnectGoogle();
      notify("Đã ngắt kết nối tài khoản Google");
      await load();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  }

  const connected = Boolean(link && link.connected);
  const needsReconnect = connected && link.status === "needs_reconnect";
  const pasted = connected && link.token_kind === "access_token";
  const lifetime = pasted ? describeLifetime(link.expires_at) : null;

  return (
    <div className="card">
      <div className="ab-row" style={{ gap: 12 }}>
        <div style={{ flex: "1 1 240px", minWidth: 0 }}>
          <p className="card-title" style={{ margin: 0 }}>Tài khoản Google</p>
          <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: "2px 0 0", lineHeight: 1.5 }}>
            {connected
              ? `Đang nối ${link.account_email || "một tài khoản Google"} — agent của bạn đọc/ghi Gmail và Drive bằng tài khoản này.`
              : "Nối một lần; các agent bạn tạo sẽ đọc/ghi Gmail và Drive bằng tài khoản của chính bạn."}
            {pasted && !needsReconnect && lifetime && !lifetime.expired && (
              <> Access token dán tay — còn khoảng {lifetime.label}; hết hạn thì dán token mới ở nút Gmail/Drive trong canvas, hoặc đăng nhập Google để khỏi dán lại.</>
            )}
            {pasted && (needsReconnect || (lifetime && lifetime.expired)) && (
              <> Access token dán tay đã hết hạn — dán token mới ở nút Gmail/Drive trong canvas, hoặc đăng nhập Google.</>
            )}
          </p>
        </div>
        {needsReconnect && <span className="tag tag-danger">cần kết nối lại</span>}
        {connected && !needsReconnect && <span className="tag tag-accent">connected</span>}
        {connected ? (
          <>
            {needsReconnect && (
              <button type="button" className="btn btn-primary btn-sm" onClick={connect} disabled={busy}>
                {busy ? <Spinner size={13} /> : <PlugZap size={13} />}Kết nối lại
              </button>
            )}
            <button type="button" className="btn btn-secondary btn-sm" onClick={disconnect} disabled={busy}>
              Ngắt kết nối
            </button>
          </>
        ) : (
          <button type="button" className="btn btn-primary btn-sm" onClick={connect} disabled={busy}>
            {busy ? <Spinner size={13} /> : <PlugZap size={13} />}Kết nối Google
          </button>
        )}
      </div>
    </div>
  );
}

/** The creator's own MCP registrations, with where each one stands in review. */
function McpConnectionList({ rows }) {
  if (rows.length === 0) return null;
  return (
    <div className="ab-stack">
      {rows.map((row) => (
        <div key={row.id} className="card">
          <div className="ab-row" style={{ gap: 12 }}>
            <div style={{ flex: "1 1 240px", minWidth: 0 }}>
              <p className="card-title" style={{ margin: 0 }}>{row.name}</p>
              <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: "2px 0 0", wordBreak: "break-all" }}>
                {row.server_url} · {row.credential.kind}
                {row.credential.secret_ref ? ` · ${row.credential.secret_ref}` : ""}
              </p>
            </div>
            <span className={`tag ${row.status === "approved" ? "tag-accent" : "tag-neutral"}`}>{row.status}</span>
          </div>
          <div className="ab-row" style={{ gap: 5 }}>
            {row.actions.map((action) => (
              <span key={action.name} className="ab-chip ab-mono">
                {action.name} · {action.risk_level}
              </span>
            ))}
          </div>
          {row.rejection_reason && (
            <p style={{ fontSize: "var(--text-sm)", color: "#b32828", margin: 0 }}>{row.rejection_reason}</p>
          )}
        </div>
      ))}
    </div>
  );
}

export default function UC1Connections({ notify }) {
  const [rows, setRows] = useState([]);
  const [mcpRows, setMcpRows] = useState([]);
  const [addingMcp, setAddingMcp] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [probing, setProbing] = useState(null);

  /* Failure here is silent on purpose: a creator with no MCP registrations yet
     is the normal state, and an error banner about an empty list would read as
     something being broken. */
  const loadMcp = useCallback(async () => {
    try {
      setMcpRows(await api.mcpConnections.mine());
    } catch {
      setMcpRows([]);
    }
  }, []);

  useEffect(() => { loadMcp(); }, [loadMcp]);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setRows(await api.uc1Connections.list());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function probe(connection) {
    setProbing(connection.id);
    setError("");
    try {
      const probed = await api.uc1Connections.probe(connection.id);
      setRows((current) => current.map((row) => (row.id === probed.id ? probed : row)));
      notify(
        probed.last_probe_status === "connected"
          ? `${probed.name}: kết nối được, ${probed.last_probe_objects.length} object`
          : `${probed.name}: ${probed.last_probe_detail || "probe thất bại"}`,
        probed.last_probe_status === "connected" ? "ok" : "error"
      );
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setProbing(null);
    }
  }

  return (
    <PageShell width="narrow">
        <PageHead title="Kết nối hệ thống" hint="Credential vào Vault; spec chỉ tham chiếu credential_ref. Dry-run trước khi agent phụ thuộc vào nó.">
        <button type="button" className="btn btn-secondary btn-icon" onClick={load} title="Tải lại">
          <RefreshCw size={16} />
        </button>
        </PageHead>

        <div style={{ marginBottom: 14 }}>
          <GoogleAccountCard notify={notify} />
        </div>

        {/* MCP servers the creator registers themselves — the second of the two
            credential paths, and the one that takes a token rather than a
            sign-in. */}
        <div className="ab-stack" style={{ marginBottom: 14 }}>
          <McpConnectionList rows={mcpRows} />
          <button type="button" className="btn btn-secondary" onClick={() => setAddingMcp(true)}>
            <Plus size={14} />Thêm MCP server
          </button>
          <AddMcpServer
            open={addingMcp}
            onClose={() => setAddingMcp(false)}
            notify={notify}
            onRegistered={loadMcp}
          />
        </div>

        <ErrorNote>{error}</ErrorNote>
        {loading && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}
        {!loading && rows.length === 0 && (
          <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>Chưa có kết nối nào.</div>
        )}

        <div className="ab-stack">
          {rows.map((row) => (
            <div key={row.id} className="card">
              <div className="ab-row" style={{ gap: 12 }}>
                <div style={{ flex: "1 1 240px", minWidth: 0 }}>
                  <p className="card-title" style={{ margin: 0 }}>{row.name}</p>
                  <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: "2px 0 0", wordBreak: "break-all" }}>
                    {row.credential_ref} · {row.type}{row.read_only ? " · read-only" : ""}
                  </p>
                </div>
                <ProbeBadge status={row.last_probe_status} />
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => probe(row)} disabled={probing === row.id}>
                  {probing === row.id ? <Spinner size={13} /> : <PlugZap size={13} />}Dry-run
                </button>
              </div>

              {row.allowed_objects.length > 0 && (
                <div className="ab-row" style={{ gap: 5 }}>
                  {row.allowed_objects.map((object) => (
                    <span key={object} className="ab-chip ab-mono">{object}</span>
                  ))}
                </div>
              )}

              {row.last_probe_objects.length > 0 && (
                <div className="ab-row" style={{ gap: 5 }}>
                  {row.last_probe_objects.map((entry) => (
                    <span key={entry.object} className="tag tag-accent ab-mono">
                      {entry.object} · {entry.row_count} dòng
                    </span>
                  ))}
                </div>
              )}

              {row.last_probe_status === "failed" && row.last_probe_detail && (
                <p style={{ fontSize: "var(--text-sm)", color: "#b32828", margin: 0, lineHeight: 1.5 }}>{row.last_probe_detail}</p>
              )}
            </div>
          ))}
        </div>

        <div style={{ marginTop: 16 }}>
          <CreateForm onCreated={load} notify={notify} />
        </div>
      </PageShell>
  );
}
