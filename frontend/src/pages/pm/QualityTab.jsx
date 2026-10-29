import { useEffect, useState } from "react";
import { AlertTriangle, BookOpen, Scale, Activity, FlaskConical } from "lucide-react";
import * as api from "../../lib/api";
import { ErrorNote, Hint, Spinner } from "../../components/ui";
import { useLang } from "../../i18n";
import { useAgentActivity } from "../../components/agent-presence";
import { Drawer, Empty, Panel, PanelGrid, RateCell, SectionLabel, faint, muted, pct } from "./shared";
import { AgentMascot } from "../../components/mascot";

/* ============================================================
   Tab 3 — Quality by version: is the new version better?

   Offline beside online per version (slide 18, 65) so "is the reference set
   describing reality" is a subtraction on one row; the Baseline · Candidate
   · Delta report behind each row; and the meta agent's own regression gate,
   which is the same question asked of the platform's builder.
   ============================================================ */
export default function QualityTab({ overview, windowDays, series, notify, onChanged, pickRuns }) {
  const { t } = useLang();
  const [reportVersionId, setReportVersionId] = useState(null);
  return (
    <>
      <PanelGrid>
        <Panel n={4} title={t("Offline vs online, per version")} lede={t("What the version scored on its reference set before release, beside what it does in production: success, the sampled judge, the people's thumbs, the expert's labels. A gap is the reference set not describing reality.")} wide
          actions={<JudgeSampleButton notify={notify} onChanged={onChanged} />}>
          <QualityTable rows={overview.quality_by_version} thresholds={overview.thresholds} series={series} onReport={setReportVersionId} onPick={(agentKey) => pickRuns({ agentKey })} />
        </Panel>
      </PanelGrid>
      <GatePanel history={overview.gate_history} notify={notify} onChanged={onChanged} />
      {reportVersionId != null && (
        <ReportDrawer versionId={reportVersionId} windowDays={windowDays} thresholds={overview.thresholds} onClose={() => setReportVersionId(null)} series={series} />
      )}
    </>
  );
}

function JudgeSampleButton({ notify, onChanged }) {
  const { t } = useLang();
  const [busy, setBusy] = useState(false);

  useAgentActivity("thinking", busy, t("judging a sample"));
  async function run() {
    if (!window.confirm(t("Judge up to 10 unjudged production runs now? Each run costs tokens (one call per criterion)."))) return;
    setBusy(true);
    try {
      const out = await api.pm.judgeSample(10);
      notify(t("Judged {n} runs, {p} passed, {s} skipped", { n: out.judged.length, p: out.passed, s: out.skipped }));
      onChanged();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  }
  return (
    <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={run} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      {busy ? <Spinner size={12} /> : <Scale size={12} />}{t("Judge a sample")}
    </button>
  );
}

function QualityTable({ rows, thresholds, series, onReport, onPick }) {
  const { t } = useLang();
  if (!rows.length) return <Empty>{t("No version has traffic or an eval in this window.")}</Empty>;
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="ab-table">
        <thead>
          <tr>
            <th>{t("Agent")}</th>
            <th>{t("Version")}</th>
            <th style={{ textAlign: "right" }}>{t("offline eval")}</th>
            <th style={{ textAlign: "right" }}>{t("online success")}</th>
            <th style={{ textAlign: "right" }}>{t("judge")}</th>
            <th style={{ textAlign: "right" }}>{t("thumbs")}</th>
            <th style={{ textAlign: "right" }}>{t("expert")}</th>
            <th style={{ textAlign: "right" }}>{t("links grounded")}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const belowGate = r.offline.n > 0 && r.offline_threshold != null && r.offline.rate < r.offline_threshold;
            return (
              <tr key={`${r.agent_key}-${r.version_key}`}>
                <td style={{ fontFamily: "var(--font-heading)", fontSize: "var(--text-md)" }}>
                  <button type="button" className="btn btn-ghost btn-sm" style={{ padding: 0, font: "inherit" }} onClick={() => onPick(r.agent_key)}>{r.agent_name}</button>
                  {r.pipeline === "uc1" && <span className="tag tag-neutral" style={{ marginLeft: 6, fontSize: "var(--text-2xs)" }}>UC-1</span>}
                </td>
                <td className="ab-mono" style={{ whiteSpace: "nowrap" }}>
                  v{r.version_key}
                  {r.deployed && <span className="tag tag-accent" style={{ marginLeft: 6, fontSize: "var(--text-2xs)" }}>{t("live")}</span>}
                  {r.regressed && <span className="tag tag-neutral" style={{ marginLeft: 6, color: series.red }} title={t("Offline pass rate fell against the previous version")}><AlertTriangle size={11} style={{ verticalAlign: -1 }} /> {t("regressed")}</span>}
                  {r.diverged && <span className="tag tag-neutral" style={{ marginLeft: 6, color: series.red }} title={t("Offline and online disagree by more than the allowed gap")}><AlertTriangle size={11} style={{ verticalAlign: -1 }} /> {t("diverged")}</span>}
                </td>
                <td style={{ textAlign: "right" }}>
                  <RateCell rate={r.offline} smallN={thresholds.small_n} series={series} bad={belowGate} title={r.offline_threshold != null ? t("gate {p}", { p: pct(r.offline_threshold) }) : undefined} />
                </td>
                <td style={{ textAlign: "right" }}><RateCell rate={r.online} smallN={thresholds.small_n} series={series} bad={r.online.n >= 10 && r.online.rate < thresholds.production_success_min} /></td>
                <td style={{ textAlign: "right" }}><RateCell rate={r.judge} smallN={thresholds.small_n} series={series} /></td>
                <td style={{ textAlign: "right" }}><RateCell rate={r.thumbs} smallN={thresholds.small_n} series={series} bad={r.thumbs.n >= 10 && r.thumbs.rate < thresholds.thumbs_up_min} /></td>
                <td style={{ textAlign: "right" }}><RateCell rate={r.expert} smallN={thresholds.small_n} series={series} /></td>
                <td style={{ textAlign: "right" }}><RateCell rate={r.tool_grounded} smallN={thresholds.small_n} series={series} bad={r.tool_grounded.n > 0 && r.tool_grounded.rate < 1} /></td>
                <td style={{ whiteSpace: "nowrap" }}>
                  {r.version_id != null && <button type="button" className="btn btn-ghost btn-sm" onClick={() => onReport(r.version_id)} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><BookOpen size={12} />{t("report")}</button>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p style={{ margin: "8px 0 0", fontSize: "var(--text-xs)", color: faint }}>{t("A dot after n marks a sample smaller than {n} — read it as a hint, not a number.", { n: thresholds.small_n })}</p>
    </div>
  );
}

/* ── The version report (slide 18) ────────────────────────────────────── */

function ReportDrawer({ versionId, windowDays, thresholds, onClose, series }) {
  const { t } = useLang();
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api.pm.versionReport(versionId, windowDays).then(setReport).catch((err) => setError(err.message));
  }, [versionId, windowDays]);
  if (error) return <Drawer title={t("Eval report")} onClose={onClose}><ErrorNote>{error}</ErrorNote></Drawer>;
  if (!report) return <Drawer title={t("Eval report")} onClose={onClose}><div className="ab-empty"><AgentMascot state="exploring" size={78} /></div></Drawer>;
  const verdictColor = report.verdict === "ship" ? series.blue : report.verdict === "iterate" ? "var(--color-accent)" : series.red;
  const delta = (d) => (d == null ? "—" : `${d > 0 ? "+" : ""}${Math.round(d * 100)}pt`);
  return (
    <Drawer title={t("Eval report — {name} v{v}", { name: report.agent_name, v: report.version_id })} onClose={onClose}>
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <span className="tag tag-accent" style={{ fontSize: "var(--text-sm)", color: verdictColor }}>{t(report.verdict)}</span>
        <span className="ab-mono" style={{ color: muted }}>{report.baseline_version_id != null ? t("baseline v{v}", { v: report.baseline_version_id }) : t("no earlier version with an eval")}</span>
        {report.threshold != null && <span className="ab-mono" style={{ color: muted }}>{t("gate {p}", { p: pct(report.threshold) })}</span>}
      </div>
      <div style={{ overflowX: "auto" }}>
        <table className="ab-table">
          <thead><tr><th>{t("dimension")}</th><th style={{ textAlign: "right" }}>{t("baseline")}</th><th style={{ textAlign: "right" }}>{t("candidate")}</th><th style={{ textAlign: "right" }}>{t("delta")}</th></tr></thead>
          <tbody>
            <tr style={{ fontWeight: 600 }}>
              <td>{t("all cases")}</td>
              <td style={{ textAlign: "right" }}><RateCell rate={report.baseline} smallN={thresholds?.small_n ?? 20} series={series} /></td>
              <td style={{ textAlign: "right" }}><RateCell rate={report.candidate} smallN={thresholds?.small_n ?? 20} series={series} bad={report.threshold != null && report.candidate.rate != null && report.candidate.rate < report.threshold} /></td>
              <td className="ab-mono" style={{ textAlign: "right" }}>{report.baseline ? delta((report.candidate.rate ?? 0) - (report.baseline.rate ?? 0)) : "—"}</td>
            </tr>
            {report.lines.map((line) => (
              <tr key={line.category}>
                <td className="ab-mono">{line.category}</td>
                <td style={{ textAlign: "right" }}><RateCell rate={line.baseline} smallN={thresholds?.small_n ?? 20} series={series} /></td>
                <td style={{ textAlign: "right" }}><RateCell rate={line.candidate} smallN={thresholds?.small_n ?? 20} series={series} /></td>
                <td className="ab-mono" style={{ textAlign: "right", color: line.delta != null && !line.noise && line.delta < 0 ? series.red : undefined }}>{delta(line.delta)}{line.noise ? <span style={{ color: faint }}> {t("noise")}</span> : null}</td>
              </tr>
            ))}
            <tr>
              <td>{t("regressions (passed before, fail now)")}</td>
              <td /><td className="ab-mono" style={{ textAlign: "right", color: report.regressions.length ? series.red : undefined }}>{report.regressions.length}</td><td />
            </tr>
            <tr>
              <td>{t("new cases, no baseline (not in the delta)")}</td>
              <td /><td className="ab-mono" style={{ textAlign: "right" }}>{(report.new_cases || []).length}</td><td />
            </tr>
            <tr>
              <td>{t("online, this window")}</td>
              <td />
              <td style={{ textAlign: "right" }}><RateCell rate={report.online} smallN={thresholds?.small_n ?? 20} series={series} /></td>
              <td className="ab-mono" style={{ textAlign: "right", fontSize: "var(--text-xs)", color: muted }}>{t("judge")} {pct(report.judge.rate)} · {t("thumbs")} {pct(report.thumbs.rate)} · {t("expert")} {pct(report.expert.rate)}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <ul style={{ margin: 0, paddingLeft: "var(--space-4)", fontSize: "var(--text-sm)", lineHeight: 1.6 }}>
        {report.notes.map((n, i) => <li key={i}>{n}</li>)}
      </ul>
      {(report.new_cases || []).length > 0 && (
        <div>
          <SectionLabel>{t("new cases — no baseline yet")}</SectionLabel>
          <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 4, fontSize: "var(--text-xs)" }}>
            {report.new_cases.map((c, i) => (
              <li key={i} style={{ color: c.passed ? undefined : series.red }}>
                {c.passed ? "✓" : "✗"} {c.input_message} <span style={{ color: muted }}>— {c.criterion}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {report.regressions.length > 0 && (
        <div>
          <SectionLabel>{t("read these before shipping")}</SectionLabel>
          <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 8, fontSize: "var(--text-xs)" }}>
            {report.regressions.map((r, i) => (
              <li key={i} style={{ padding: "var(--space-2)", border: "1px solid var(--color-divider)" }}>
                <div style={{ fontWeight: 600 }}>{r.input_message}</div>
                <div style={{ color: muted }}>{r.criterion}</div>
                <div style={{ color: series.red }}>{r.reason}</div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Drawer>
  );
}

/* ── The meta agent's regression gate ─────────────────────────────────── */

function GatePanel({ history, notify, onChanged }) {
  const { t } = useLang();
  const [running, setRunning] = useState(false);
  const [fidelity, setFidelity] = useState(0.78);
  const [repeats, setRepeats] = useState(1);
  const [latest, setLatest] = useState(null);
  const [detailId, setDetailId] = useState(null);
  const [detail, setDetail] = useState(null);

  useEffect(() => {
    if (detailId == null) { setDetail(null); return; }
    api.pm.gateRun(detailId).then(setDetail).catch((err) => notify(err.message, "error"));
  }, [detailId, notify]);

  async function run(mode) {
    if (mode === "real" && !window.confirm(t("Run the gate against the real model? All 19 cases cost real tokens, and the pre-registered thresholds are applied exactly."))) return;
    setRunning(true);
    try {
      const out = await api.pm.runGate({ mode, fidelity, repeats: mode === "real" ? 1 : repeats });
      setLatest(out);
      setDetailId(out.runs[0]?.id ?? null);
      notify(out.runs.every((r) => r.passed) ? t("Gate passed") : t("Gate failed — see the report"), out.runs.every((r) => r.passed) ? "ok" : "error");
      onChanged();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setRunning(false);
    }
  }

  const unsat = latest?.unsatisfiable ?? history[0]?.thresholds?._unsatisfiable_cases ?? [];

  return (
    <section className="blueprint" style={{ padding: "var(--space-4)", marginTop: 18, display: "flex", flexDirection: "column", gap: 12 }}>
      <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
      <div style={{ display: "flex", justifyContent: "space-between", gap: 16, flexWrap: "wrap", alignItems: "flex-end" }}>
        <div>
          <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)", display: "flex", alignItems: "center", gap: 8 }}>
            <Activity size={18} />{t("Meta agent regression gate")}
            <Hint text={t("Playbook step 3: the gate exists, the thresholds are frozen, and it had never been run. Simulated mode drives the same grader over a scripted stand-in so the pipeline and this panel work before tokens are spent; only a real run is a measurement. Each failure is sorted: builder_issue (the model got it wrong) or case_issue (no model can get it right).")} />
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <label className="ab-mono" style={{ fontSize: "var(--text-xs)", color: muted }}>{t("fidelity")} <input type="number" min="0" max="1" step="0.05" value={fidelity} onChange={(e) => setFidelity(Number(e.target.value))} className="ab-input" style={{ width: 70, marginLeft: 4 }} /></label>
          <label className="ab-mono" style={{ fontSize: "var(--text-xs)", color: muted }}>{t("repeats")} <input type="number" min="1" max="5" value={repeats} onChange={(e) => setRepeats(Number(e.target.value))} className="ab-input" style={{ width: 56, marginLeft: 4 }} /></label>
          <button type="button" className="btn btn-primary btn-sm" disabled={running} onClick={() => run("simulated")} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>{running ? <Spinner size={12} /> : <FlaskConical size={12} />}{t("Run simulated")}</button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={running} onClick={() => run("real")}>{t("Run real (costs tokens)")}</button>
        </div>
      </div>

      {unsat.length > 0 && (
        <div style={{ display: "flex", gap: 10, padding: "var(--space-2) var(--space-3)", border: "1px solid var(--color-divider)", fontSize: "var(--text-sm)", lineHeight: 1.5 }}>
          <AlertTriangle size={15} style={{ flex: "none", marginTop: 2 }} />
          <div>
            <strong>{t("{n} cases cannot pass even with a perfect builder:", { n: unsat.length })}</strong> <span className="ab-mono">{unsat.join(", ")}</span>
            {" — "}
            {t("the grader reads `precedence: [human_approval, mcp]` as \"an approval before every mcp node\", but these graphs start with a read-only mcp step, and the case means \"before the send\". With 2 of 5 multi_turn cases unsatisfiable, the ≥4/5 bar can never be met. Fixing this is a reviewed diff to the deck or the grader, not a threshold change after seeing a score.")}
          </div>
        </div>
      )}

      {latest && (
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", fontSize: "var(--text-sm)" }}>
          <span className="tag tag-accent">{t("pass^{k}: {n} cases passed every run", { k: latest.runs.length, n: latest.always_passed.length })}</span>
          {latest.runs.map((r) => <span key={r.id} className="tag tag-neutral">#{r.id} {r.passed ? t("PASS") : t("FAIL")}</span>)}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 3fr) minmax(300px, 2fr)", gap: 18 }}>
        <div style={{ overflowX: "auto" }}>
          <table className="ab-table" style={{ whiteSpace: "nowrap" }}>
            <thead><tr><th>#</th><th>{t("mode")}</th><th>{t("verdict")}</th><th>{t("families")}</th><th>{t("failures")}</th></tr></thead>
            <tbody>
              {history.length === 0 && <tr><td colSpan={5} style={{ color: muted }}>{t("Never run.")}</td></tr>}
              {history.map((g) => (
                <tr key={g.id} style={{ cursor: "pointer", background: detailId === g.id ? "color-mix(in srgb, var(--color-text) 5%, transparent)" : undefined }} onClick={() => setDetailId(g.id)}>
                  <td className="ab-mono">{g.id}</td>
                  <td><span className={`tag ${g.mode === "real" ? "tag-accent" : "tag-neutral"}`}>{g.mode}</span></td>
                  <td className="ab-mono" style={{ color: g.passed ? undefined : "#b91c1c" }}>{g.passed ? "PASS" : "FAIL"}</td>
                  <td className="ab-mono" style={{ fontSize: "var(--text-xs)", lineHeight: 1.5 }}>{Object.entries(g.per_family).map(([f, e]) => <div key={f} style={{ color: e.passed >= e.wanted ? undefined : "#b91c1c" }}>{f} {e.passed}/{e.total} <span style={{ color: faint }}>≥{e.wanted}</span></div>)}</td>
                  <td className="ab-mono" style={{ fontSize: "var(--text-xs)", lineHeight: 1.5 }}>{Object.entries(g.failure_classes).map(([k, v]) => <div key={k}>{k} {v}</div>)}{Object.keys(g.failure_classes).length === 0 && "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          {detail ? (
            <pre className="ab-code ab-scrollbar" style={{ margin: 0, fontSize: "var(--text-xs)", lineHeight: 1.5, maxHeight: 280, overflow: "auto", whiteSpace: "pre-wrap" }}>{detail.report}</pre>
          ) : (
            <Empty>{t("Pick a run to read its report.")}</Empty>
          )}
        </div>
      </div>
    </section>
  );
}
