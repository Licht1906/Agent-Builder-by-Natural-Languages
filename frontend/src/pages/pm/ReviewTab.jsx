import { useLang } from "../../i18n";
import { ErrorNote } from "../../components/ui";
import { Empty, Panel, PanelGrid, SectionLabel, faint, muted, pct } from "./shared";
import { RunReview, useRunDetail } from "./RunDetail";
import { AgentMascot } from "../../components/mascot";

/* ============================================================
   Tab 4 — Review: the expert hour, and what it buys.

   The review screen fills the tab (slide 62: one screen, one decision) and
   walks the queue — judged runs first, then thumbed-down, then failed, then
   newest — so the labels land where they calibrate the judge soonest.
   Beside it, what those labels have bought so far: the judge's confusion
   matrix against the expert on the same runs (slide 53), and whether it
   has earned the "trusted" badge yet.
   ============================================================ */
export default function ReviewTab({ overview, includeSimulated, series, notify, onChanged, onOpen }) {
  const { t } = useLang();
  const { detail, error, loaded, reload } = useRunDetail({ reviewMode: true, includeSimulated });
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 3fr) minmax(320px, 2fr)", gap: 18, alignItems: "start" }}>
      <section className="blueprint" style={{ padding: "var(--space-4)", display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
        <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
          <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>
            {detail ? t("Review — {n} left", { n: detail.queue_remaining ?? "?" }) : t("Review")}
          </p>
          <span className="ab-mono" style={{ fontSize: "var(--text-xs)", color: faint }}>{t("G = pass · B = fail · one run at a time")}</span>
        </div>
        <ErrorNote>{error}</ErrorNote>
        {!loaded && !error && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}
        {loaded && detail === null && (
          <Empty>{t("Nothing left to review. New production runs, judge verdicts and thumbs-downs queue up here on their own.")}</Empty>
        )}
        {detail && (
          <RunReview detail={detail} reviewMode codes={overview.trace_codes} notify={notify} onChanged={onChanged} onSaved={reload} series={series} />
        )}
      </section>
      <PanelGrid>
        <Panel n={8} title={t("Judge calibration")} lede={t("The judge's verdict beside the expert's on the same runs. Matching pass rates prove nothing; what matters is how often a good answer is recognised (TPR) and a bad one caught (TNR).")} wide>
          <CalibrationPanel data={overview.calibration} series={series} onOpen={onOpen} />
        </Panel>
      </PanelGrid>
    </div>
  );
}

function CalibrationPanel({ data, series, onOpen }) {
  const { t } = useLang();
  if (!data.labelled) return <Empty>{t("No run has both a judge verdict and an expert label yet. Judge a sample (Quality tab), then review those runs — each label is also a calibration point.")}</Empty>;
  const cell = (label, n, danger) => (
    <div style={{ background: "var(--color-bg)", padding: "var(--space-2) var(--space-3)" }}>
      <p style={{ margin: 0, fontSize: "var(--text-2xs)", letterSpacing: ".12em", textTransform: "uppercase", color: faint }}>{label}</p>
      <p style={{ margin: "3px 0 0", fontFamily: "var(--font-heading)", fontSize: "var(--text-2xl)", lineHeight: 1, color: danger && n > 0 ? series.red : undefined }}>{n}</p>
    </div>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap", fontSize: "var(--text-sm)" }}>
        <span className={`tag ${data.trusted ? "tag-accent" : "tag-neutral"}`}>{data.trusted ? t("judge trusted") : t("judge not yet trusted")}</span>
        <span className="ab-mono">TPR {pct(data.tpr)} <span style={{ color: faint }}>≥{pct(data.tpr_min)}</span></span>
        <span className="ab-mono">TNR {pct(data.tnr)} <span style={{ color: faint }}>≥{pct(data.tnr_min)}</span></span>
        <span className="ab-mono">κ {data.kappa == null ? "—" : data.kappa.toFixed(2)}</span>
        <span className="ab-mono" style={{ color: muted }}>{t("{n} labelled · need {m}", { n: data.labelled, m: data.min_labels })}</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 1, background: "var(--color-divider)", border: "1px solid var(--color-divider)" }}>
        {cell(t("good · judge pass"), data.tp)}
        {cell(t("good · judge fail (blocked)"), data.fn)}
        {cell(t("bad · judge pass (let through)"), data.fp, true)}
        {cell(t("bad · judge fail"), data.tn)}
      </div>
      {data.disagreements.length > 0 && (
        <div>
          <SectionLabel>{t("disagreements — the only rows worth digging into")}</SectionLabel>
          <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6, fontSize: "var(--text-xs)" }}>
            {data.disagreements.slice(0, 6).map((d) => (
              <li key={d.run_id} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                <span className="tag tag-neutral" style={{ fontSize: "var(--text-2xs)", flex: "none" }}>{d.judge_passed ? t("judge ✓ · expert ✗") : t("judge ✗ · expert ✓")}</span>
                <span style={{ flex: 1 }}>{d.expert_note || d.judge_reason}{d.expert_codes.length ? <span className="ab-mono" style={{ color: faint }}> · {d.expert_codes.join(", ")}</span> : null}</span>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => onOpen(d.run_id)}>{t("read")}</button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
