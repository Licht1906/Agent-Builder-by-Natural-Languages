import { CheckCircle2, AlertTriangle } from "lucide-react";
import { useLang } from "../../i18n";
import { Empty, HBars, Panel, PanelGrid, SimulationBanner, faint, muted, pct } from "./shared";

/* ============================================================
   Tab 1 — Overview: is anything on fire?

   The four production questions (slide 19) as alerts against pre-registered
   thresholds, six headline tiles, and the two cuts that say *where* to look
   next: by trigger (rehearsals apart from real traffic) and by how runs end.
   Every number here is a door: a bar or an alert opens the Runs tab with that
   filter on, the review tile opens the Review tab.
   ============================================================ */
export default function OverviewTab({ overview, series, runFilter, pickRuns, openTab }) {
  const { t } = useLang();
  return (
    <>
      <SimulationBanner sim={overview.simulation} traces={overview.traces} />
      <AlertStrip alerts={overview.alerts} series={series} onPick={(agentKey) => pickRuns({ agentKey })} />
      <HeroRow overview={overview} series={series} onReview={() => openTab("review")} />
      <div style={{ marginTop: 18 }}>
        <PanelGrid>
          <Panel n={1} title={t("Run success rate by trigger")} lede={t("The creator's own rehearsals kept apart from real traffic: averaged together, an agent looks healthiest on the days its owner sat testing it.")}>
            <TriggerBars rows={overview.success_by_trigger} series={series} threshold={overview.thresholds.production_success_min} onPick={(trigger) => pickRuns(trigger ? { trigger } : {})} active={runFilter.trigger} />
          </Panel>
          <Panel n={2} title={t("How runs end")} lede={t("Decided by code at write time: spec_error is something the creator fixes, system_error is a call to the platform team. A success with a failed tool is a failure under the surface.")}>
            <OutcomeBars data={overview.outcomes} series={series} onPick={(outcome) => pickRuns(outcome ? { outcome } : {})} active={runFilter.outcome} />
          </Panel>
          <Panel title={t("Activation funnel")} lede={t("Measured by events, not surveys. It stops at the last thing the platform can honestly count — somebody other than the owner used the agent.")}>
            <ActivationFunnel data={overview.activation} series={series} />
          </Panel>
        </PanelGrid>
      </div>
    </>
  );
}

function AlertStrip({ alerts, series, onPick }) {
  const { t } = useLang();
  if (!alerts.length) {
    return (
      <div style={{ display: "flex", gap: 8, alignItems: "center", padding: "var(--space-2) var(--space-3)", border: "1px solid var(--color-divider)", marginBottom: 18, fontSize: "var(--text-sm)", color: muted }}>
        <CheckCircle2 size={15} style={{ color: series.blue }} />{t("No alert: every pre-registered threshold holds on the data there is.")}
      </div>
    );
  }
  return (
    <ul style={{ listStyle: "none", margin: "0 0 18px", padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
      {alerts.map((a, i) => (
        <li key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start", padding: "var(--space-2) var(--space-3)", border: `1px solid ${a.severity === "critical" ? series.red : "var(--color-divider)"}`, fontSize: "var(--text-sm)", lineHeight: 1.45 }}>
          <AlertTriangle size={14} style={{ flex: "none", marginTop: 2, color: a.severity === "critical" ? series.red : "var(--color-accent)" }} />
          <span className="tag tag-neutral" style={{ fontSize: "var(--text-2xs)", flex: "none" }}>{a.code}</span>
          <span style={{ flex: 1 }}>{a.message}</span>
          {a.agent_key && <button type="button" className="btn btn-ghost btn-sm" onClick={() => onPick(a.agent_key)}>{t("runs")}</button>}
        </li>
      ))}
    </ul>
  );
}

export function productionTraffic(rows) {
  return rows.find((r) => r.trigger === "manual") || null;
}

function HeroRow({ overview, series, onReview }) {
  const { t } = useLang();
  const th = overview.thresholds;
  const lead = productionTraffic(overview.success_by_trigger);
  const sum = (key) => overview.quality_by_version.reduce((acc, v) => ({ n: acc.n + v[key].n, passed: acc.passed + v[key].passed }), { n: 0, passed: 0 });
  const judge = sum("judge");
  const thumbs = sum("thumbs");
  const expert = sum("expert");
  const deployedBelowGate = overview.quality_by_version.filter((v) => v.deployed && v.offline.n > 0 && v.offline_threshold != null && v.offline.rate < v.offline_threshold).length;
  const deployedWithEval = overview.quality_by_version.filter((v) => v.deployed && v.offline.n > 0).length;
  const cal = overview.calibration;
  const ratio = (r) => (r.n ? r.passed / r.n : null);
  const tiles = [
    { label: t("production success"), value: pct(lead?.rate), sub: lead ? t("{n} runs · bar {p}", { n: lead.runs, p: pct(th.production_success_min) }) : t("no real runs yet"), bad: Boolean(lead && lead.runs >= 10 && lead.rate != null && lead.rate < th.production_success_min) },
    { label: t("offline eval"), value: deployedWithEval ? `${deployedWithEval - deployedBelowGate}/${deployedWithEval}` : "—", sub: t("deployed versions above their gate"), bad: deployedBelowGate > 0 },
    { label: t("judge (sampled)"), value: pct(ratio(judge)), sub: cal.trusted ? t("{n} runs · calibrated", { n: judge.n }) : t("{n} runs · not calibrated", { n: judge.n }), bad: judge.n >= 5 && ratio(judge) < th.production_success_min },
    { label: t("thumbs up"), value: pct(ratio(thumbs)), sub: t("{n} votes", { n: thumbs.n }), bad: thumbs.n >= 10 && ratio(thumbs) < th.thumbs_up_min },
    { label: t("expert pass"), value: pct(ratio(expert)), sub: t("{n} labels", { n: expert.n }), bad: false },
    { label: t("to review"), value: overview.review_queue, sub: t("unlabelled production runs"), bad: false, action: overview.review_queue > 0 ? onReview : null },
  ];
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 1, background: "var(--color-divider)", border: "1px solid var(--color-divider)" }}>
      {tiles.map((tile) => (
        <div key={tile.label} style={{ background: "var(--color-bg)", padding: "var(--space-4)", cursor: tile.action ? "pointer" : undefined }} onClick={tile.action || undefined} role={tile.action ? "button" : undefined}>
          <p style={{ margin: 0, fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: faint }}>{tile.label}</p>
          <p style={{ margin: "5px 0 0", fontFamily: "var(--font-heading)", fontSize: "var(--text-3xl)", lineHeight: 1, color: tile.bad ? series.red : "var(--color-text)" }}>{tile.value}</p>
          <p className="ab-mono" style={{ margin: "6px 0 0", fontSize: "var(--text-xs)", color: muted }}>{tile.sub}{tile.action ? " →" : ""}</p>
        </div>
      ))}
    </div>
  );
}

function TriggerBars({ rows, series, onPick, active, threshold }) {
  const { t } = useLang();
  const order = ["manual", "chat_test"];
  const data = order
    .map((trigger) => rows.find((r) => r.trigger === trigger))
    .filter(Boolean)
    .map((r) => ({ key: r.trigger, label: r.trigger, value: r.rate ?? 0, suffix: t("of {n}", { n: r.runs }), tooltip: `${r.successes}/${r.runs}` }));
  if (!data.length) return <Empty>{t("No runs in this window.")}</Empty>;
  return (
    <div title={t("Bar for manual traffic: {p}. Click a bar to open those runs.", { p: pct(threshold) })}>
      <HBars rows={data} series={series} emphasis={productionTraffic(rows)?.trigger} onPick={onPick} active={active} format={pct} max={1} />
    </div>
  );
}

function OutcomeBars({ data, series, onPick, active }) {
  const { t } = useLang();
  const rows = data.slices.map((s) => ({ key: s.outcome, label: s.outcome, value: s.runs, tooltip: s.outcome }));
  if (!rows.length) return <Empty>{t("No runs in this window.")}</Empty>;
  const ratio = data.self_fix_ratio;
  const healthy = ratio != null && ratio >= data.self_fix_healthy_above;
  return (
    <>
      <HBars rows={rows} series={series} onPick={onPick} active={active} format={(v) => v.toLocaleString()} />
      <div style={{ display: "flex", flexWrap: "wrap", gap: 14, alignItems: "center", borderTop: "1px solid var(--color-divider)", paddingTop: "var(--space-2)", fontSize: "var(--text-sm)" }}>
        <span className="ab-mono" style={{ fontSize: "var(--text-xs)", letterSpacing: ".1em", textTransform: "uppercase", color: faint }}>{t("self-fix ratio")}</span>
        <svg width="140" height="12" aria-hidden="true"><rect width="140" height="12" fill={series.track} /><rect width={ratio == null ? 0 : 140 * ratio} height="12" rx="2" fill={healthy ? series.blue : series.red} /></svg>
        <strong>{pct(ratio)}</strong>
        <span style={{ color: muted }}>{ratio == null ? t("no classified errors yet") : healthy ? t("creators fix most of their own failures") : t("too many failures need the platform team")}</span>
        {data.unclassified_errors > 0 && <span className="tag tag-neutral" title={t("Plain `error` rows the taxonomy has no word for yet")}>{t("{n} unclassified", { n: data.unclassified_errors })}</span>}
        {data.success_with_tool_failures > 0 && <span className="tag tag-neutral" style={{ color: series.red }} title={t("Ended success while a tool it called failed — read the trace")}>{t("{n} success with a failed tool", { n: data.success_with_tool_failures })}</span>}
      </div>
    </>
  );
}

function ActivationFunnel({ data, series }) {
  const { t } = useLang();
  const steps = [
    ["agents", t("agents created")],
    ["with_confirmed_workflow", t("workflow confirmed")],
    ["with_trial_run", t("first trial run")],
    ["published", t("published")],
    ["with_production_run", t("first production run")],
  ];
  return <HBars rows={steps.map(([key, label]) => ({ key, label, value: data[key] }))} series={series} format={(v) => v} max={Math.max(data.agents, 1)} labelWidth={200} />;
}
