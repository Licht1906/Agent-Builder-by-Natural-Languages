import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Plus, ChevronLeft, Zap, Check, Eye, EyeOff, KeyRound, Mail, HardDrive, Server } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, Spinner } from "./ui";

/* ============================================================
   Add MCP server — the port of Agent Builder's own two-step flow.

   Step one is a **gallery**, not a form: cards for the connectors this platform
   maintains, then the ones creators registered themselves, then `+ Server` for
   a URL nobody has catalogued yet. That ordering is the whole point of the
   screen — somebody who wants Gmail should never have to know Gmail has a URL,
   and somebody bringing their own server should not have to hunt for the door.

   Step two is one connector's **connect screen**: what it can do listed before
   anything is asked of you, an optional description, and the one field that
   takes a credential. Two different credential shapes end up here and the
   screen says which is which — a platform connector signs you in through
   Google (per-user OAuth, no token to paste), a third-party server takes a
   token or headers.

   Whatever is typed goes **one way**: the backend hands it to the encrypted
   secret store and answers with a `secret_ref`, so the field is cleared as soon
   as it has been sent and no response ever carries a credential back.
   ============================================================ */

/** Card art for the connectors this platform ships. Kept here rather than
 *  fetched: a logo is a fact about the product, not about the deployment. */
export const BUILT_IN_LOOK = {
  gmail: { label: "Gmail", icon: Mail, color: "#ea4335" },
  google_drive: { label: "Google Drive", icon: HardDrive, color: "#1a73e8" },
};

const AUTH_MODES = [
  { id: "none", label: "No auth", kind: "none" },
  { id: "token", label: "Access token / API key", kind: "secret_reference" },
  { id: "headers", label: "Custom headers", kind: "custom_headers" },
];

const TABS = [
  { id: "all", label: "Tất cả" },
  { id: "platform", label: "Của nền tảng" },
  { id: "others", label: "Bên thứ ba" },
];

/** `X-Api-Key: abc` per line — the shape a person copies out of a server's own
 *  docs, rather than JSON whose braces they would have to get right. */
function parseHeaders(text) {
  return Object.fromEntries(
    text
      .split("\n")
      .map((line) => line.split(/:(.*)/s))
      .filter((parts) => parts.length > 1 && parts[0].trim() && parts[1].trim())
      .map((parts) => [parts[0].trim(), parts[1].trim()])
  );
}

function ConnectorCard({ look, name, subtitle, onClick }) {
  const Icon = look.icon || Server;
  return (
    <button type="button" className="mcp-card" onClick={onClick}>
      <span className="mcp-card-icon" style={{ color: look.color || "var(--color-accent)" }}>
        <Icon size={20} />
      </span>
      <span className="mcp-card-name">{name}</span>
      {subtitle && <span className="mcp-card-sub">{subtitle}</span>}
    </button>
  );
}

/** The `+` that sits on the shelf beside the connectors. A card and not a
 *  button, because the choice it belongs to is "which connector" — and an
 *  option shaped differently from its siblings reads as a different question. */
function AddCard({ onClick }) {
  return (
    <button type="button" className="mcp-card mcp-card-add" onClick={onClick}>
      <span className="mcp-card-icon"><Plus size={20} /></span>
      <span className="mcp-card-name">Server khác</span>
      <span className="mcp-card-sub">thêm bằng địa chỉ</span>
    </button>
  );
}

export default function AddMcpServer({ open, onClose, onRegistered, onPick, notify }) {
  const [tab, setTab] = useState("all");
  /** `null` = gallery. Otherwise the connector being connected: either a
   *  catalogued one (`{ entry: tool }`) or the custom-server form (`{ custom: true }`). */
  const [chosen, setChosen] = useState(null);
  const [tools, setTools] = useState([]);
  const [googleLink, setGoogleLink] = useState(null);

  const load = useCallback(async () => {
    try {
      setTools(await api.workflow.tools());
    } catch {
      setTools([]);
    }
    try {
      const links = await api.userConnections.list();
      setGoogleLink(links.find((entry) => entry.provider === "google") || null);
    } catch {
      setGoogleLink(null);
    }
  }, []);

  useEffect(() => { if (open) load(); }, [open, load]);

  function close() {
    setChosen(null);
    setTab("all");
    onClose();
  }

  if (!open) return null;

  const overlay = (body) =>
    createPortal(
      /* A portal to `document.body`, not a div in place: the trigger lives
         inside the Inspector, which is a narrow scrolling column — a dialog
         rendered there is clipped by it and reads as a cramped inline form,
         which is exactly the complaint. Centring it also makes the connector
         grid legible at the width it was drawn for. */
      <div className="ab-dialog-backdrop" onClick={close}>
        <div className="mcp-dialog" onClick={(event) => event.stopPropagation()}>{body}</div>
      </div>,
      document.body
    );

  if (chosen && chosen.custom) {
    return overlay(
      <CustomServerForm
        onBack={() => setChosen(null)}
        onDone={(row) => { close(); if (onRegistered) onRegistered(row); }}
        notify={notify}
      />
    );
  }
  if (chosen && chosen.entry) {
    return overlay(
      <ConnectorScreen
        tool={chosen.entry}
        link={googleLink}
        onBack={() => setChosen(null)}
        onUse={onPick ? (tool) => { close(); onPick(tool); } : null}
        onConnected={load}
        notify={notify}
      />
    );
  }

  const builtIn = tools.filter((tool) => tool.source === "built_in");
  const registered = tools.filter((tool) => tool.source !== "built_in");
  const showPlatform = tab === "all" || tab === "platform";
  const showOthers = tab === "all" || tab === "others";

  return overlay(
    <div className="mcp-panel">
      <div className="ab-row" style={{ justifyContent: "space-between", gap: 10 }}>
        <p className="card-title" style={{ margin: 0 }}>Chọn MCP server</p>
        <button type="button" className="btn btn-secondary btn-sm" onClick={close}>Đóng</button>
      </div>

      <div className="mcp-tabs">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className={`mcp-tab${tab === entry.id ? " active" : ""}`}
            onClick={() => setTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      {showPlatform && (
        <>
          <p className="mcp-section">
            Kết nối của nền tảng <span className="text-muted">— do đội này viết và vận hành</span>
          </p>
          <div className="mcp-grid">
            {builtIn.map((tool) => (
              <ConnectorCard
                key={tool.id}
                look={BUILT_IN_LOOK[tool.id] || {}}
                name={(BUILT_IN_LOOK[tool.id] || {}).label || tool.name}
                subtitle={`${tool.actions.length} action`}
                onClick={() => setChosen({ entry: tool })}
              />
            ))}
            {/* Level with the connectors rather than off in a corner: bringing
                your own server is one of the choices on this shelf, not a
                different kind of act. */}
            <AddCard onClick={() => setChosen({ custom: true })} />
          </div>
        </>
      )}

      {showOthers && (
        <>
          <p className="mcp-section">
            Server bên thứ ba <span className="text-muted">— do người khác viết và vận hành</span>
          </p>
          <div className="mcp-grid">
            {registered.map((tool) => (
              <ConnectorCard
                key={tool.id}
                look={{}}
                name={tool.name}
                subtitle={`${tool.actions.length} action · đã duyệt`}
                onClick={() => setChosen({ entry: tool })}
              />
            ))}
            {/* Only when the platform grid is not on screen to carry it — two
                identical add-cards in one view would read as two doors. */}
            {!showPlatform && <AddCard onClick={() => setChosen({ custom: true })} />}
          </div>
        </>
      )}
    </div>
  );
}

/** One catalogued connector's connect screen.

 *  What it can do is listed *before* anything is asked, which is the order the
 *  screen this ports uses — and the honest one, since the answer to "should I
 *  connect this" is that list.
 *
 *  Two credential paths, side by side rather than one instead of the other,
 *  because they are genuinely different bargains and only the person choosing
 *  knows which they want:
 *
 *  - **Sign in with Google** mints a refresh token, so the connection keeps
 *    working tomorrow. It is the one to prefer and it is placed first.
 *  - **Paste an access token** is Agent Builder's own path. It works with a
 *    token minted anywhere — a service account, a script — and it costs what
 *    that costs: the token dies within the hour and there is nothing here to
 *    refresh it from. The screen says so rather than letting somebody discover
 *    it when the agent stops answering an hour later.
 */
export function ConnectorScreen({ tool, link, onBack, onUse, onConnected, notify }) {
  const [busy, setBusy] = useState(false);
  const [token, setToken] = useState("");
  const [reveal, setReveal] = useState(false);
  const [error, setError] = useState("");
  const look = BUILT_IN_LOOK[tool.id] || {};
  const Icon = look.icon || Server;
  const perUser = Boolean(BUILT_IN_LOOK[tool.id]);
  const connected = Boolean(link && link.connected);
  const pasted = connected && link.token_kind === "access_token";
  const lifetime = pasted ? describeLifetime(link.expires_at) : null;
  const stale = connected && (link.status === "needs_reconnect" || (lifetime && lifetime.expired));
  // The sign-in button and the paste box are ALWAYS on screen for a per-user
  // connector, connected or not. Two earlier versions hid them — first behind
  // "đang nối", then behind a "Đổi token" toggle — and both taught the same
  // lesson: a creator who opens this panel has come to give it a token. A
  // pasted token lives an hour, a new agent wants a fresh one, and choosing
  // Gmail a second time must show the same box as the first, ready for a
  // different token. The status line above says what is in use right now.
  const showConnect = perUser;

  async function signIn() {
    setBusy(true);
    try {
      const { url } = await api.userConnections.startGoogle();
      window.location.assign(url);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  async function paste() {
    if (busy || !token.trim()) return;
    setBusy(true);
    setError("");
    try {
      const row = await api.userConnections.connectGoogleWithToken(token);
      // Cleared the moment it has been sent — the only copy that should still
      // exist is the encrypted one.
      setToken("");
      if (notify) notify(`Đã nối ${row.account_email || "tài khoản Google"} bằng access token`);
      if (onConnected) await onConnected();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mcp-panel mcp-connect">
      <span className="mcp-connect-icon" style={{ color: look.color || "var(--color-accent)" }}>
        <Icon size={26} />
      </span>
      <p className="mcp-connect-title">Kết nối {look.label || tool.name}</p>
      <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: 0 }}>{tool.id}</p>

      <div className="mcp-abilities">
        {tool.actions.map((action) => (
          <span key={action.name} className="mcp-ability">
            <Check size={13} style={{ flexShrink: 0, opacity: 0.7 }} />
            <span className="ab-mono">{action.name}</span>
            {action.risk_level === "high" && <span className="tag tag-danger" style={{ marginLeft: "auto" }}>high</span>}
          </span>
        ))}
      </div>

      {perUser && connected && (
        <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: 0, lineHeight: 1.55, textAlign: "left" }}>
          {stale ? "Đã nối " : "Đang nối "}
          <b>{link.account_email || "một tài khoản Google"}</b>
          {!pasted && " bằng đăng nhập Google — token tự làm mới, không cần dán lại."}
          {pasted && stale && (
            <>
              {" bằng access token dán tay — "}
              <span style={{ color: "var(--color-danger, #c0392b)" }}>token đã hết hạn</span>
              {", dán token mới hoặc đăng nhập Google."}
            </>
          )}
          {pasted && !stale && (
            <>
              {" bằng access token dán tay — "}
              {lifetime ? `còn khoảng ${lifetime.label}` : "hết hạn trong khoảng một giờ"}
              {". Dán token khác bên dưới để thay."}
            </>
          )}
        </p>
      )}

      {showConnect && (
        <>
          <button type="button" className="btn btn-primary btn-sm" onClick={signIn} disabled={busy}>
            {busy ? <Spinner size={13} /> : <Zap size={13} />}Đăng nhập Google
          </button>
          <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: "-4px 0 0" }}>token tự làm mới — nên dùng cách này</p>

          <div className="mcp-or"><span>hoặc</span></div>

          <div className="mcp-secret">
            <KeyRound size={14} className="mcp-secret-icon" />
            <input
              className="input"
              type={reveal ? "text" : "password"}
              autoComplete="off"
              placeholder="Dán access token"
              value={token}
              onChange={(event) => setToken(event.target.value)}
            />
            <button type="button" className="mcp-secret-eye" onClick={() => setReveal((on) => !on)} title={reveal ? "Ẩn" : "Hiện"}>
              {reveal ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
          <button type="button" className="btn btn-secondary btn-sm" onClick={paste} disabled={busy || !token.trim()}>
            {connected ? "Dùng token mới" : "Dùng access token"}
          </button>
          <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: "-4px 0 0" }}>
            {connected ? "thay token đang dùng — hết hạn sau khoảng một giờ" : "hết hạn sau khoảng một giờ, phải dán lại"}
          </p>
        </>
      )}

      {!perUser && (
        <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: 0, lineHeight: 1.55 }}>
          Kết nối này đã được đăng ký và duyệt. Credential của nó nằm trong kho mã hoá; muốn đổi thì đăng ký lại.
        </p>
      )}

      <ErrorNote>{error}</ErrorNote>

      <div className="ab-row" style={{ gap: 8, justifyContent: "space-between" }}>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onBack} disabled={busy}>
          <ChevronLeft size={13} />Quay lại
        </button>
        {onUse && (
          <button type="button" className="btn btn-primary btn-sm" onClick={() => onUse(tool)}>
            <Check size={13} />Dùng cho bước này
          </button>
        )}
      </div>
    </div>
  );
}

/** The `+ Server` door: a server this platform has never heard of. Saving is
 *  also the discovery step — the backend calls the server's own `tools/list`,
 *  which is why a failure comes back in the server's words. */
function CustomServerForm({ onBack, onDone, notify }) {
  const [name, setName] = useState("");
  const [serverUrl, setServerUrl] = useState("");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState("token");
  const [secret, setSecret] = useState("");
  const [headers, setHeaders] = useState("");
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit() {
    if (busy || !name.trim() || !serverUrl.trim()) return;
    setBusy(true);
    setError("");
    const chosen = AUTH_MODES.find((entry) => entry.id === mode);
    try {
      const row = await api.mcpConnections.register({
        name: name.trim(),
        server_url: serverUrl.trim(),
        description: description.trim(),
        credential: {
          kind: chosen.kind,
          ...(mode === "token" ? { secret } : {}),
          ...(mode === "headers" ? { headers: parseHeaders(headers) } : {}),
        },
      });
      // Cleared the moment it has been sent: the only copy that should still
      // exist is the encrypted one.
      setSecret("");
      setHeaders("");
      if (notify) notify(`Đã đăng ký ${row.name} — ${row.actions.length} action, đang chờ admin duyệt`);
      onDone(row);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mcp-panel mcp-connect">
      <span className="mcp-connect-icon"><Server size={26} /></span>
      <p className="mcp-connect-title">Kết nối MCP server của bạn</p>

      <input className="input" placeholder="https://.../mcp" value={serverUrl} onChange={(e) => setServerUrl(e.target.value)} />
      <input className="input" placeholder="Tên kết nối — ví dụ ERP Kinh doanh" value={name} onChange={(e) => setName(e.target.value)} />
      <input className="input" placeholder="Mô tả (không bắt buộc)" value={description} onChange={(e) => setDescription(e.target.value)} />

      <select className="input" value={mode} onChange={(e) => setMode(e.target.value)}>
        {AUTH_MODES.map((entry) => <option key={entry.id} value={entry.id}>{entry.label}</option>)}
      </select>

      {mode === "token" && (
        <div className="mcp-secret">
          <KeyRound size={14} className="mcp-secret-icon" />
          <input
            className="input"
            type={reveal ? "text" : "password"}
            autoComplete="off"
            placeholder="Dán access token"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
          />
          <button type="button" className="mcp-secret-eye" onClick={() => setReveal((on) => !on)} title={reveal ? "Ẩn" : "Hiện"}>
            {reveal ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
        </div>
      )}

      {mode === "headers" && (
        <textarea
          className="input"
          rows={3}
          placeholder={"X-Api-Key: abc123\nX-Tenant: acme"}
          value={headers}
          onChange={(e) => setHeaders(e.target.value)}
        />
      )}

      <p className="text-muted" style={{ fontSize: "var(--text-xs)", margin: 0, lineHeight: 1.55 }}>
        Bấm Kết nối là hệ thống gọi <span className="ab-mono">tools/list</span> của server để lấy danh sách action.
        Credential vào thẳng kho mã hoá — không lưu trên bản ghi, không đọc lại được.
      </p>

      <ErrorNote>{error}</ErrorNote>

      <div className="ab-row" style={{ gap: 8, justifyContent: "space-between" }}>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onBack} disabled={busy}>
          <ChevronLeft size={13} />Quay lại
        </button>
        <button type="button" className="btn btn-primary btn-sm" onClick={submit} disabled={busy || !name.trim() || !serverUrl.trim()}>
          {busy ? <Spinner size={13} /> : <Zap size={13} />}Kết nối
        </button>
      </div>
    </div>
  );
}

/** "còn khoảng 52 phút" / expired — what a pasted token's `expires_at` means right now.
 *  Returns `null` when the link has no expiry (the sign-in path). */
export function describeLifetime(expiresAt, now = Date.now()) {
  if (!expiresAt) return null;
  // The backend writes naive UTC; a string without an offset must be read as UTC,
  // not as the browser's local time, or the clock is off by the timezone.
  const iso = /[zZ]|[+-]\d\d:?\d\d$/.test(expiresAt) ? expiresAt : `${expiresAt}Z`;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  const seconds = Math.round((at - now) / 1000);
  if (seconds <= 0) return { expired: true, seconds, label: "đã hết hạn" };
  const minutes = Math.ceil(seconds / 60);
  return { expired: false, seconds, label: minutes >= 60 ? `${Math.floor(minutes / 60)} giờ ${minutes % 60} phút` : `${minutes} phút` };
}
