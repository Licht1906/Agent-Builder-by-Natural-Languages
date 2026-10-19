import { useCallback, useEffect, useState } from "react";
import { Activity, RefreshCw, CheckCircle2, XCircle, Rocket } from "lucide-react";
import * as api from "../../lib/api";
import { ErrorNote, Hint, PageShell, Section, Spinner } from "../../components/ui";
import { useLang } from "../../i18n";
import { AgentMascot } from "../../components/mascot";

/* ============================================================
   How a published agent has actually been serving — #55 / ADR-0039.

   Its own tab rather than a panel on Approval & Deploy, for the reason that ADR
   gives: that screen answers "may this version run", this one answers "how is
   the version that *is* running behaving". They are read at different times by
   somebody in a different frame of mind — one while building, one when
   something looks wrong.

   **Nothing here is filtered in the browser.** What counts as the Deployment
   serving somebody is two conditions decided server-side — a real run, on a
   production thread — and the creator's own rehearsals are excluded there. A
   second opinion about that here is how the totals and the list would end up
   describing different populations of run.
   ============================================================ */
export default function OperationsTab({ agentId, notify }) {
  const { t } = useLang();
  const [operations, setOperations] = useState(null);
  const [published, setPublished] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setOperations(await api.deployment.operations(agentId));
      setPublished(true);
    } catch (err) {
      // A 404 means the agent was never published — an answer, not a failure.
      // An agent with no Deployment has no operations to show, and the next move
      // is to publish an approved version.
      if (err.status === 404) {
        setOperations(null);
        setPublished(false);
      } else {
        setError(err.message);
      }
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => { load(); }, [load]);

  return (
    <PageShell>
      <Section
        title={t("Operations")}
        hint={t("How the published version is behaving right now — spend, failures, and the runs behind them. Only real traffic counts: your own rehearsals and trial chats are excluded, or an agent would look busiest on the days you sat testing it.")}
        actions={
          <button className="btn btn-secondary btn-icon" onClick={load} disabled={loading} title={t("Reload")}>
            {loading ? <Spinner size={14} /> : <RefreshCw size={14} />}
          </button>
        }
      >

        <ErrorNote>{error}</ErrorNote>

        {loading && !operations && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}

        {!loading && !published && (
          <div style={{ border: "1px dashed var(--color-divider)", padding: "var(--space-12) var(--space-6)", textAlign: "center", fontSize: "var(--text-md)", color: "color-mix(in srgb, var(--color-text) 58%, transparent)" }}>
            <Rocket size={20} style={{ marginBottom: 10 }} />
            <p style={{ margin: 0 }}>
              {t("This agent has not been published, so it is not serving anyone yet. Publish an approved version from Approval & Deploy.")}
            </p>
          </div>
        )}

        {operations && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 1, background: "var(--color-divider)", border: "1px solid var(--color-divider)", marginBottom: 10 }}>
              <Metric label={t("runs served")} value={operations.runs.toLocaleString()} tone="var(--color-accent)" />
              <Metric label={t("failed part-way")} value={operations.errors} tone={operations.errors ? "#b91c1c" : undefined} />
              <Metric label={t("tokens")} value={operations.tokens.toLocaleString()} />
              <Metric label={t("cost")} value={`$${operations.cost_usd.toFixed(4)}`} />
            </div>

            <p className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)", margin: "0 0 22px", lineHeight: 1.6 }}>
              {t("serving v#{n} — the totals above span earlier versions too, so a release shows up as a change rather than as a reset", { n: operations.agent_version_id })}
            </p>

            <p style={{ margin: "0 0 10px", fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>{t('Recent runs')}</p>

            {operations.recent_executions.length === 0 ? (
              <div style={{ border: "1px dashed var(--color-divider)", padding: "var(--space-10) var(--space-6)", textAlign: "center", fontSize: "var(--text-md)", color: "color-mix(in srgb, var(--color-text) 58%, transparent)" }}>
                {t("Nobody has called this agent yet.")}
              </div>
            ) : (
              <div className="blueprint" style={{ padding: 0, overflowX: "auto" }}>
                <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
                <table className="table">
                  <thead>
                    <tr>
                      <th>{t('Run')}</th>
                      <th>{t('Version')}</th>
                      <th>{t('Outcome')}<Hint text={t("A run stopped by the budget ceiling is not counted as a failure — it still produced a real answer, and it said why it stopped.")} /></th>
                      <th style={{ textAlign: "right" }}>{t('Latency')}</th>
                      <th style={{ textAlign: "right" }}>{t('Cost')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {operations.recent_executions.map((execution) => (
                      <tr key={execution.id}>
                        <td style={{ fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)" }}>
                          {new Date(execution.at).toLocaleString()}
                          <div style={{ color: "color-mix(in srgb, var(--color-text) 48%, transparent)", fontSize: "var(--text-xs)" }}>{execution.conversation_id}</div>
                        </td>
                        <td style={{ fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)" }}>v#{execution.version_id}</td>
                        <td>
                          {execution.error ? (
                            <span style={{ display: "flex", alignItems: "flex-start", gap: 6, fontSize: "var(--text-sm)", color: "#b91c1c" }}>
                              <XCircle size={14} style={{ flexShrink: 0, marginTop: 1 }} />{execution.error}
                            </span>
                          ) : (
                            <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "var(--text-sm)", color: "var(--color-accent)" }}>
                              <CheckCircle2 size={14} />{t("succeeded")}
                            </span>
                          )}
                        </td>
                        <td style={{ textAlign: "right", fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)" }}>{execution.latency_seconds.toFixed(1)}s</td>
                        <td style={{ textAlign: "right", fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)" }}>
                          ${execution.cost_usd.toFixed(4)}
                          <div style={{ color: "color-mix(in srgb, var(--color-text) 48%, transparent)", fontSize: "var(--text-xs)" }}>{execution.tokens} {t("tok")}</div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

          </>
        )}
      </Section>
    </PageShell>
  );
}

/** One cell of the metric strip — same shape the Evaluation tab draws, so the
 *  two read as one system when somebody flips between the tabs. */
function Metric({ label, value, tone }) {
  return (
    <div style={{ background: "var(--color-bg)", padding: "var(--space-4)" }}>
      <p style={{ margin: 0, fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: "color-mix(in srgb, var(--color-text) 52%, transparent)" }}>{label}</p>
      <p style={{ margin: "5px 0 0", fontFamily: "var(--font-heading)", fontSize: "var(--text-2xl)", lineHeight: 1, color: tone || "var(--color-text)" }}>{value}</p>
    </div>
  );
}
