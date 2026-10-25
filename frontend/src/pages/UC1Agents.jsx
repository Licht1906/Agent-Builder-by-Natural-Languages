import { useCallback, useEffect, useState } from "react";
import { ChevronRight, RefreshCw } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell } from "../components/ui";
import { AgentMascot } from "../components/mascot";

/* ============================================================
   Every Enterprise agent, and the door to each one's workbench.

   Deliberately separate from `Dashboard`'s own agent list: that one is V2's
   `/agents` — integer ids, a different table, a workflow graph instead of an
   `AgentSpec`. The two look alike on screen and share nothing underneath, and
   a single merged list would be the place that confusion gets permanent.

   Its job is small and was, until now, missing entirely: hand out the
   `(agent_id, version)` pair every other Enterprise endpoint is addressed by.

   "UC-1" is still what this area is called in the code, the API and the issue
   tracker. On screen it is "Enterprise", because the internal name of a
   use-case tells the reader nothing.
   ============================================================ */

/** Lifecycle status as a design-system tag rather than a bespoke pill.
 *
 *  `Dashboard.statusTag` does the same job for V2's statuses and the two now
 *  agree on the vocabulary: neutral for states that are merely waiting, accent
 *  for progress, danger for a version that has been stopped. */
const STATUS_TAGS = {
  draft: "tag tag-neutral",
  pending_approval: "tag tag-warn",
  approved: "tag tag-accent",
  deployed: "tag tag-accent",
  suspended: "tag tag-danger",
  retired: "tag tag-danger",
};

export default function UC1Agents({ openAgent }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setRows(await api.uc1Agents.catalog());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <PageShell width="narrow">
        <PageHead title="Agent Enterprise" hint="Mở một agent để chat thử, chấm golden set và xin duyệt.">
        <button type="button" className="btn btn-secondary btn-icon" onClick={load} title="Tải lại">
          <RefreshCw size={16} />
        </button>
        </PageHead>

        <ErrorNote>{error}</ErrorNote>
        {loading && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}
        {!loading && rows.length === 0 && (
          <div className="ab-empty" style={{ border: "1px dashed var(--color-divider)" }}>
            Chưa có agent Enterprise nào — tạo bằng meta-agent rồi bấm Xác nhận.
          </div>
        )}

        <div className="ab-stack">
          {rows.map((row) => (
            /* A row, not a `<button>`: `.ab-root button` puts everything inside
               a button in the heading face, and an agent id is not a heading.
               Same shape `Dashboard` uses for its cards. */
            <div
              key={row.agent_id}
              className="card"
              role="button"
              tabIndex={0}
              style={{ flexDirection: "row", gap: 14, alignItems: "center", cursor: "pointer", flexWrap: "wrap" }}
              onClick={() => openAgent(row.agent_id)}
              onKeyDown={(event) => event.key === "Enter" && openAgent(row.agent_id)}
            >
              <div style={{ flex: "1 1 240px", minWidth: 0 }}>
                <p className="card-title" style={{ margin: 0 }}>{row.name}</p>
                <p className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)", margin: "2px 0 0" }}>
                  {row.agent_id} · v{row.latest_version}
                  {row.version_count > 1 ? ` · ${row.version_count} version` : ""}
                </p>
              </div>
              <span className={STATUS_TAGS[row.status] || "tag tag-neutral"}>{row.status}</span>
              <ChevronRight size={15} style={{ color: "color-mix(in srgb, var(--color-text) 52%, transparent)" }} />
            </div>
          ))}
        </div>
      </PageShell>
  );
}
