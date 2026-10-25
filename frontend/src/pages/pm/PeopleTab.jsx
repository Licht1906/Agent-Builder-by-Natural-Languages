import { useState } from "react";
import { CheckCircle2, XCircle, ThumbsUp, ThumbsDown } from "lucide-react";
import * as api from "../../lib/api";
import { useLang } from "../../i18n";
import { ColumnWidths, Empty, Panel, PanelGrid, SectionLabel, Th, faint, minutes, muted, pct, useColumnSizing } from "./shared";

/* ============================================================
   Tab 6 — Approvals & feedback: the people in the loop.

   Two kinds of person touch a run after the agent has: the approver a
   `HumanApproval` node is waiting on (how long, how often yes, decide from
   here), and the end user who left a thumb (volume beside ratio — a ratio
   over three votes is not a trend; the notes are the cheapest golden cases
   there are).
   ============================================================ */
export default function PeopleTab({ overview, series, notify, onChanged, onOpen }) {
  const { t } = useLang();
  return (
    <PanelGrid>
      <Panel n={5} title={t("Approval funnel")} lede={t("Runs waiting on a person, how long they wait, how often the answer is yes. Decide from here.")} wide>
        <ApprovalFunnel data={overview.approvals} notify={notify} onChanged={onChanged} />
      </Panel>
      <Panel n={6} title={t("Feedback")} lede={t("Thumbs per week, volume beside ratio — a ratio over three votes is not a trend. Notes are the cheapest golden cases there are.")} wide>
        <FeedbackChart weeks={overview.feedback_by_week} notes={overview.feedback_notes} series={series} onOpen={onOpen} />
      </Panel>
    </PanelGrid>
  );
}

/** Waiting · Agent · Asks · Role · decide. "Asks" is the wide one by default —
 *  it carries a whole approval message — and every one of them is draggable
 *  from there, because whose message is long differs by agent. */
const APPROVAL_COLUMNS = [96, 240, 520, 120, 210];

function ApprovalFunnel({ data, notify, onChanged }) {
  const { t } = useLang();
  const [busy, setBusy] = useState(null);
  const sizing = useColumnSizing("pm.approvals.columns", APPROVAL_COLUMNS);
  async function decide(row, approved) {
    let reason = null;
    if (!approved) {
      reason = window.prompt(t("Why reject? The run's `rejected` branch continues with this reason."));
      if (reason == null) return;
    }
    setBusy(row.id);
    try {
      const out = await api.pm.decideApproval(row.agent_id, row.id, approved, reason);
      notify(out.note, approved ? "ok" : "error");
      onChanged();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(null);
    }
  }
  const tiles = [
    [t("pending"), data.pending],
    [t("approval rate"), pct(data.approval_rate)],
    [t("avg wait"), minutes(data.avg_wait_minutes)],
    [t("p90 wait"), minutes(data.p90_wait_minutes)],
    [t("decided in window"), data.decided],
  ];
  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 1, background: "var(--color-divider)", border: "1px solid var(--color-divider)" }}>
        {tiles.map(([label, value]) => (
          <div key={label} style={{ background: "var(--color-bg)", padding: "var(--space-3)" }}>
            <p style={{ margin: 0, fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: faint }}>{label}</p>
            <p style={{ margin: "4px 0 0", fontFamily: "var(--font-heading)", fontSize: "var(--text-2xl)", lineHeight: 1 }}>{value}</p>
          </div>
        ))}
      </div>
      {data.pending_list.length === 0 ? (
        <Empty>{t("Nothing is waiting on a person right now.")}</Empty>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="ab-table ab-table-sized" style={{ minWidth: sizing.widths.reduce((a, b) => a + b, 0) }}>
            <ColumnWidths sizing={sizing} />
            <thead>
              <tr>
                <Th sizing={sizing} index={0}>{t("Waiting")}</Th>
                <Th sizing={sizing} index={1}>{t("Agent")}</Th>
                <Th sizing={sizing} index={2}>{t("Asks")}</Th>
                <Th sizing={sizing} index={3}>{t("Role")}</Th>
                <Th sizing={sizing} index={4} />
              </tr>
            </thead>
            <tbody>
              {data.pending_list.map((row) => (
                <tr key={row.id}>
                  <td className="ab-mono" style={{ whiteSpace: "nowrap", color: row.waiting_minutes > 60 * 24 ? "#b91c1c" : undefined }}>{minutes(row.waiting_minutes)}{row.simulated && <span className="tag tag-neutral" style={{ marginLeft: 6, fontSize: "var(--text-2xs)" }}>{t("sim")}</span>}</td>
                  <td style={{ fontFamily: "var(--font-heading)", fontSize: "var(--text-md)" }}>{row.agent_name}<div className="ab-mono" style={{ fontSize: "var(--text-xs)", color: faint }}>v#{row.version_id} · {row.node_id}{row.sandbox ? ` · ${t("trial")}` : ""}</div></td>
                  <td>
                    <div className="ab-scrollbar" style={{ maxHeight: 132, overflowY: "auto", whiteSpace: "pre-wrap" }}>
                      {row.message}
                      <div style={{ fontSize: "var(--text-xs)", color: muted, marginTop: 4 }}>{row.input_message}</div>
                    </div>
                  </td>
                  <td className="ab-mono">{row.approver_role}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button type="button" className="btn btn-sm btn-primary" disabled={busy === row.id} onClick={() => decide(row, true)} style={{ marginRight: 6, display: "inline-flex", alignItems: "center", gap: 5 }}><CheckCircle2 size={12} />{t("Approve")}</button>
                    <button type="button" className="btn btn-sm btn-secondary" disabled={busy === row.id} onClick={() => decide(row, false)} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}><XCircle size={12} />{t("Reject")}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function FeedbackChart({ weeks, notes, series, onOpen }) {
  const { t } = useLang();
  if (!weeks.length && !notes.length) return <Empty>{t("No thumbs yet. They arrive from the chat window and from the runs table.")}</Empty>;
  const max = Math.max(...weeks.map((w) => w.thumbs_up + w.thumbs_down), 1);
  const H = 110; const W = Math.max(weeks.length, 1) * 56;
  return (
    <>
      {weeks.length > 0 && (
        <div>
          <div style={{ display: "flex", gap: 14, fontSize: "var(--text-xs)", color: muted, marginBottom: 6 }}>
            <span><i style={{ display: "inline-block", width: 10, height: 10, background: series.blue, marginRight: 5, verticalAlign: -1 }} />{t("thumbs up")}</span>
            <span><i style={{ display: "inline-block", width: 10, height: 10, background: series.red, marginRight: 5, verticalAlign: -1 }} />{t("thumbs down")}</span>
          </div>
          <svg width="100%" viewBox={`0 0 ${W} ${H + 34}`} preserveAspectRatio="xMinYMid meet" style={{ display: "block", maxWidth: W * 1.4 }} role="img">
            {weeks.map((w, i) => {
              const total = w.thumbs_up + w.thumbs_down;
              const x = i * 56 + 8; const bw = 36;
              const upH = (w.thumbs_up / max) * H; const downH = (w.thumbs_down / max) * H;
              return (
                <g key={w.week_start}>
                  <title>{`${new Date(w.week_start).toLocaleDateString()}: +${w.thumbs_up} / −${w.thumbs_down}`}</title>
                  <rect x={x} y={H - downH} width={bw} height={downH} fill={series.red} />
                  <rect x={x} y={H - downH - upH - (downH ? 2 : 0)} width={bw} height={upH} rx="2" fill={series.blue} />
                  <text x={x + bw / 2} y={H + 14} textAnchor="middle" fontSize="10" fill="currentColor" opacity="0.6" fontFamily="ui-monospace, monospace">{new Date(w.week_start).toLocaleDateString(undefined, { day: "2-digit", month: "2-digit" })}</text>
                  <text x={x + bw / 2} y={H + 28} textAnchor="middle" fontSize="10.5" fill="currentColor" fontFamily="ui-monospace, monospace">{total ? pct(w.thumbs_up / total) : "—"}</text>
                </g>
              );
            })}
          </svg>
        </div>
      )}
      {notes.length > 0 && (
        <div style={{ borderTop: "1px solid var(--color-divider)", paddingTop: "var(--space-2)" }}>
          <SectionLabel>{t("latest notes")}</SectionLabel>
          <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6, fontSize: "var(--text-sm)" }}>
            {notes.slice(0, 6).map((n) => (
              <li key={n.score_id} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                {n.thumbs_up ? <ThumbsUp size={13} style={{ flex: "none", marginTop: 3, color: series.blue }} /> : <ThumbsDown size={13} style={{ flex: "none", marginTop: 3, color: series.red }} />}
                <span>
                  <span style={{ color: "var(--color-text)" }}>{n.comment}</span>{" "}
                  <span className="ab-mono" style={{ fontSize: "var(--text-xs)", color: faint }}>— {n.agent_name} · v{n.version_key}</span>
                  {n.run_id != null && <button type="button" className="btn btn-ghost btn-sm" onClick={() => onOpen(n.run_id)}>{t("read")}</button>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}
