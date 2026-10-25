import { useCallback, useEffect, useState } from "react";
import { UserCheck, UserPlus, RefreshCw, Users } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell, Spinner } from "../components/ui";
import { AgentMascot } from "../components/mascot";

/* ============================================================
   UC1-01 (#117), FR-03 — the screen behind "liên hệ agent_admin".

   A refused login (`_PENDING_ACCOUNT` in `src.api.auth_routes`) tells the
   locked-out person to go find an admin. This is where that admin lands: the
   pending accounts, oldest first, each with the one button that unlocks it.
   Everything before this ticket existed except the surface — the endpoints
   were reachable only by hand with a bearer token and a `user_id` nobody had
   a way to look up.

   Two tabs rather than one list, because the two are read for different
   reasons. "Chờ kích hoạt" is a worklist: it is meant to reach empty, and an
   empty one is the good outcome. "Tất cả" is a directory — never empty, never
   finished, and mixing it into the queue would make a platform with three
   pending accounts and forty active ones look like forty-three of them are
   waiting.
   ============================================================ */

const TABS = [
  { id: "pending", label: "Chờ kích hoạt", icon: UserPlus, status: "pending" },
  { id: "all", label: "Tất cả tài khoản", icon: Users, status: null },
];

/** How long this account has been locked out, in the units that matter for
 *  the decision. "3 ngày" is a nudge; "3 phút" means somebody just signed up
 *  and is probably still at their desk waiting. */
function waitedFor(createdAt) {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(createdAt).getTime()) / 60000));
  if (minutes < 1) return "vừa xong";
  if (minutes < 60) return `${minutes} phút`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} giờ`;
  return `${Math.round(hours / 24)} ngày`;
}

export default function AdminUsers({ notify }) {
  const [tab, setTab] = useState("pending");
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // Per-row rather than one page-wide flag: activating one account must not
  // grey out the button on the next one an admin was about to click.
  const [activating, setActivating] = useState(null);

  const status = TABS.find((entry) => entry.id === tab).status;

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setRows(await api.adminUsers.list(status));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => { load(); }, [load]);

  async function activate(user) {
    setActivating(user.id);
    setError("");
    try {
      await api.adminUsers.activate(user.id);
      notify(`Đã kích hoạt ${user.email}`);
      // Reload rather than dropping the row locally: on the "Tất cả" tab the
      // account stays, it just changes state, and the list is the only thing
      // that knows which of the two happened.
      await load();
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setActivating(null);
    }
  }

  return (
    <PageShell width="narrow">
        <PageHead title="Tài khoản" hint="Tài khoản đăng ký mới không đăng nhập được cho tới khi được kích hoạt ở đây. Cũ nhất lên trước.">
        <button type="button" className="btn btn-secondary btn-icon" onClick={load} title="Tải lại">
          <RefreshCw size={16} />
        </button>
        </PageHead>

        <div className="ab-tabs" style={{ padding: 0, marginBottom: 18 }}>
          {TABS.map((entry) => (
            <button key={entry.id} type="button" className={`ab-tab${tab === entry.id ? " active" : ""}`} onClick={() => setTab(entry.id)}>
              <entry.icon size={14} />{entry.label}
            </button>
          ))}
        </div>

        <ErrorNote>{error}</ErrorNote>
        {loading && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}
        {!loading && rows.length === 0 && (
          <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>
            {tab === "pending" ? "Không có ai đang chờ kích hoạt." : "Chưa có tài khoản nào."}
          </div>
        )}

        <div className="ab-stack">
          {rows.map((user) => (
            <div key={user.id} className="card" style={{ flexDirection: "row", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
              <div style={{ flex: "1 1 240px", minWidth: 0 }}>
                <p className="card-title" style={{ margin: 0 }}>{user.email}</p>
                <p className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)", margin: "2px 0 0" }}>
                  #{user.id} · {user.role}
                  {user.employee_id ? ` · ${user.employee_id}` : ""}
                </p>
                {(user.job_title || user.department) && (
                  <p className="text-muted" style={{ fontSize: "var(--text-sm)", margin: "2px 0 0" }}>
                    {[user.job_title, user.department].filter(Boolean).join(" · ")}
                  </p>
                )}
              </div>

              {/* The old "đang hoạt động" pill was painted in `rgba(0,212,168,…)`,
                  a teal from a palette the design system no longer carries. Both
                  states are tags now, and the pair says what it means: a waiting
                  account is a thing to act on, an active one is not. */}
              {user.status === "pending" ? (
                <span className="tag tag-warn">chờ kích hoạt · {waitedFor(user.created_at)}</span>
              ) : (
                <span className="tag tag-accent">đang hoạt động</span>
              )}

              {user.status === "pending" && (
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  onClick={() => activate(user)}
                  disabled={activating === user.id}
                >
                  {activating === user.id ? <Spinner size={13} /> : <UserCheck size={13} />}Kích hoạt
                </button>
              )}
            </div>
          ))}
        </div>
      </PageShell>
  );
}
