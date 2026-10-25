import { useState } from "react";
import { Pause, Play } from "lucide-react";
import * as api from "../../lib/api";
import { Spinner } from "../../components/ui";
import { useLang } from "../../i18n";
import { Empty, HBars, Panel, PanelGrid, SectionLabel, pct, secs, usd } from "./shared";

/* ============================================================
   Tab 5 — Cost & latency: what is it costing, and the kill switch.

   Cost per successful run tells a cheap agent from one that is cheap because
   it keeps failing; P90 over the ceiling is a regression even when the
   answer is right. The pause button is the one lever that acts on production
   immediately, so it lives beside the number that would make a PM reach for
   it.
   ============================================================ */
export default function CostTab({ overview, series, notify, onChanged, pickRuns }) {
  const { t } = useLang();
  return (
    <PanelGrid>
      <Panel n={3} title={t("Cost and latency per agent")} lede={t("Cost per successful run tells a cheap agent from one that is cheap because it keeps failing. P90 latency over the ceiling is a regression even when the answer is right. The pause switch is the kill switch.")} wide>
        <CostTable rows={overview.cost_by_agent} models={overview.cost_by_model} thresholds={overview.thresholds} series={series} notify={notify} onChanged={onChanged} onPick={(agentKey) => pickRuns({ agentKey })} />
      </Panel>
      <Panel n={4} title={t("What the platform spends on itself")} lede={t("Everything above is what the agents cost to run. This is what they cost to build: reading an interview, drafting a spec, composing a graph, writing and judging the eval cases. It used to be metered by nothing at all — the board once read $1.57 while the OpenAI project behind it was hitting its spend limit.")} wide>
        <PlatformSpend rows={overview.platform_spend || []} series={series} />
      </Panel>
    </PanelGrid>
  );
}

/* The other half of the bill, by job.
 *
 * By *job* and not by model, because every row here runs on one of the same two
 * models and "gpt-4.1-mini was 90%" is a fact addressed to nobody. "Composing
 * graphs is 60% of the build bill" is one somebody can act on.
 *
 * `attempts_avg` earns its column: `write_document` re-sends the whole prompt
 * with the validator's complaint appended, so an average above ~1.3 is the
 * composer failing and paying twice for the same graph — the one number here
 * that points at a fixable thing rather than at a price. */
function PlatformSpend({ rows, series }) {
  const { t } = useLang();
  if (!rows.length) {
    return <Empty>{t("No platform calls in this window.")}</Empty>;
  }
  const total = rows.reduce((sum, r) => sum + r.usd, 0);
  const cached = rows.reduce((sum, r) => sum + r.cached_input_tokens, 0);
  const input = rows.reduce((sum, r) => sum + r.input_tokens, 0);
  const max = Math.max(...rows.map((r) => r.usd), 0.0001);
  return (
    <>
      <HBars
        rows={rows.map((r) => ({
          key: r.purpose,
          label: r.purpose,
          value: r.usd,
          suffix: t("{n} calls", { n: r.calls }),
          tooltip: t("{i} in / {o} out · {a} attempts per document on average", {
            i: r.input_tokens.toLocaleString(),
            o: r.output_tokens.toLocaleString(),
            a: r.attempts_avg.toFixed(2),
          }),
        }))}
        series={series}
        format={(v) => usd(v, 4)}
        max={max}
        labelWidth={190}
      />
      <div style={{ display: "flex", flexWrap: "wrap", gap: 16, alignItems: "center", borderTop: "1px solid var(--color-divider)", paddingTop: "var(--space-2)", fontSize: "var(--text-sm)" }}>
        <SectionLabel>{t("build bill")}</SectionLabel>
        <strong>{usd(total, 4)}</strong>
        <span className="tag tag-neutral" title={t("Input tokens the provider had already seen, billed at a quarter. Low means the prompts are not repeating enough of themselves to earn the discount.")}>
          {t("{p} of input cached", { p: pct(input ? cached / input : null) })}
        </span>
      </div>
    </>
  );
}

function CostTable({ rows, models, thresholds, series, notify, onChanged, onPick }) {
  const { t } = useLang();
  const [busy, setBusy] = useState(null);
  async function toggle(row) {
    const serving = row.deployment_status !== "active";
    if (!serving && !window.confirm(t('Pause "{name}"? Everyone but its owner gets a 404 until you resume it.', { name: row.agent_name }))) return;
    setBusy(row.agent_id);
    try {
      await api.pm.setDeploymentStatus(row.agent_id, serving);
      notify(serving ? t('"{name}" is serving again', { name: row.agent_name }) : t('"{name}" paused', { name: row.agent_name }));
      onChanged();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(null);
    }
  }
  if (!rows.length) return <Empty>{t("No runs in this window.")}</Empty>;
  const maxModel = Math.max(...models.map((m) => m.cost_usd), 0.0001);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 2fr) minmax(320px, 1fr)", gap: 18 }}>
      <div style={{ overflowX: "auto" }}>
        <table className="ab-table">
          <thead>
            <tr>
              <th>{t("Agent")}</th>
              <th style={{ textAlign: "right" }}>{t("runs")}</th>
              <th style={{ textAlign: "right" }}>{t("success")}</th>
              <th style={{ textAlign: "right" }}>{t("$ / run")}</th>
              <th style={{ textAlign: "right" }}>{t("$ / success")}</th>
              <th style={{ textAlign: "right" }}>{t("P50")}</th>
              <th style={{ textAlign: "right" }}>{t("P90")}</th>
              <th>{t("Serving")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const dear = row.cost_per_success != null && row.cost_per_run != null && row.cost_per_success > row.cost_per_run * 1.4;
              const slow = row.latency_p90 != null && row.latency_p90 > thresholds.latency_p90_max_seconds;
              const pricey = row.cost_per_run != null && row.cost_per_run > thresholds.cost_per_run_max_usd;
              return (
                <tr key={row.agent_key}>
                  <td style={{ fontFamily: "var(--font-heading)", fontSize: "var(--text-md)" }}>
                    <button type="button" className="btn btn-ghost btn-sm" style={{ padding: 0, font: "inherit" }} onClick={() => onPick(row.agent_key)}>{row.agent_name}</button>
                    {row.pipeline === "uc1" && <span className="tag tag-neutral" style={{ marginLeft: 6, fontSize: "var(--text-2xs)" }}>UC-1</span>}
                  </td>
                  <td className="ab-mono" style={{ textAlign: "right" }}>{row.runs}</td>
                  <td className="ab-mono" style={{ textAlign: "right" }}>{pct(row.runs ? row.successes / row.runs : null)}</td>
                  <td className="ab-mono" style={{ textAlign: "right", color: pricey ? series.red : undefined }} title={pricey ? t("Over the cost ceiling") : undefined}>{usd(row.cost_per_run)}</td>
                  <td className="ab-mono" style={{ textAlign: "right", color: dear ? series.red : undefined }} title={dear ? t("Far above cost per run: failures are eating the budget") : undefined}>{usd(row.cost_per_success)}</td>
                  <td className="ab-mono" style={{ textAlign: "right" }}>{secs(row.latency_p50)}</td>
                  <td className="ab-mono" style={{ textAlign: "right", color: slow ? series.red : undefined }} title={slow ? t("Over the latency ceiling") : undefined}>{secs(row.latency_p90)}</td>
                  <td>
                    {row.agent_id == null || row.deployment_status == null ? (
                      <span className="tag tag-neutral">{row.agent_id == null ? t("UC-1") : t("not published")}</span>
                    ) : (
                      <button type="button" className={`btn btn-sm ${row.deployment_status === "active" ? "btn-secondary" : "btn-primary"}`} disabled={busy === row.agent_id} onClick={() => toggle(row)} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                        {busy === row.agent_id ? <Spinner size={12} /> : row.deployment_status === "active" ? <Pause size={12} /> : <Play size={12} />}
                        {row.deployment_status === "active" ? t("Pause") : t("Resume")}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div>
        <SectionLabel>{t("by model")}</SectionLabel>
        <HBars rows={models.map((m) => ({ key: m.model, label: m.model, value: m.cost_usd, suffix: t("{n} runs", { n: m.runs }), tooltip: `${m.tokens.toLocaleString()} tokens` }))} series={series} format={(v) => usd(v, 2)} max={maxModel} />
      </div>
    </div>
  );
}
