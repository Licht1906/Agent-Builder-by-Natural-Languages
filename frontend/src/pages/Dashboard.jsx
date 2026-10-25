import { useCallback, useEffect, useRef, useState } from "react";
import { Upload } from "lucide-react";
import * as api from "../lib/api";
import { DeployedBadge, ErrorNote, Hint, PageHead, PageShell, Section, Spinner } from "../components/ui";
import ConfirmDialog from "../components/ConfirmDialog";
import { useLang } from "../i18n";
import { AgentMascot } from "../components/mascot";
import { useAgentStandIn } from "../components/agent-presence";

/** Which screen an agent opens on, decided by the stage its newest version is at.
 *  A version still holding an unconfirmed spec has no workflow to draw. */
export function screenForVersion(version) {
  if (!version) return "spec";
  return version.status === "spec_pending_confirmation" || version.status === "spec_confirmed" ? "spec" : "builder";
}

const newest = (versions) => (versions.length ? versions[versions.length - 1] : null);

/* SVG icons used inline — matching Agentlab.dc.html */
const botIcon = `<svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M12 8V4H8"/><rect x="4" y="8" width="16" height="12" rx="2"/><path d="M2 14h2m16 0h2"/><circle cx="9" cy="13" r="1"/><circle cx="15" cy="13" r="1"/></svg>`;
const trashIcon = `<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`;

/* The sidebar owns navigation and the session now, so this screen is only the
   two lists: what you have built, and what you are allowed to talk to. */
export default function Dashboard({ openAgent, openChat, startDesigner, notify }) {
  const { t } = useLang();


  const [mine, setMine] = useState([]);
  const [running, setRunning] = useState([]);
  const [loading, setLoading] = useState(true);

  /* Same bargain as the Designer's home stage: while the empty state is what is
     on screen, its 140px mascot is the agent's body and the corner stands
     down — two copies of one creature is worse than no corner at all. */
  useAgentStandIn(!loading && mine.length === 0);
  const [error, setError] = useState("");
  const [doomed, setDoomed] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [importing, setImporting] = useState(false);
  const fileInput = useRef(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [agents, reachable] = await Promise.all([api.agents.mine(), api.agents.running()]);
      const withVersions = await Promise.all(
        agents.map(async (agent) => {
          try {
            const versions = await api.agents.versions(agent.id);
            return { ...agent, versions, latest: newest(versions) };
          } catch {
            return { ...agent, versions: [], latest: null };
          }
        })
      );
      setMine(withVersions);
      setRunning(reachable);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  /* Read a bundle off disk and hand it straight to the API.
   *
   * Nothing is inspected here beyond "is this JSON at all". Every other question
   * — is the format one this build reads, is the spec complete, is the graph a
   * graph — is answered by the schemas the endpoint already validates against,
   * and a second opinion in the browser is how a file starts being accepted here
   * and refused there. The one thing worth catching locally is a file that is
   * not JSON, because `JSON.parse` throws a sentence nobody should have to read.
   */
  async function importBundle(event) {
    const file = event.target.files && event.target.files[0];
    event.target.value = ""; // so re-picking the same file fires `change` again
    if (!file) return;

    setImporting(true);
    setError("");
    try {
      let bundle;
      try {
        bundle = JSON.parse(await file.text());
      } catch {
        throw new Error(t('"{name}" is not a JSON file.', { name: file.name }));
      }
      const result = await api.agents.importBundle(bundle);
      const remapped = Object.entries(result.remapped_models || {});
      notify(
        remapped.length
          ? t('Imported "{name}" — {n} step(s) had to change model on this deployment', { name: result.agent.name, n: remapped.length })
          : t('Imported "{name}" — open it to finish wiring it up', { name: result.agent.name }),
        remapped.length ? "error" : "ok"
      );
      await load();
      openAgent(result.agent, { id: result.version_id, status: "workflow_pending_confirmation" });
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setImporting(false);
    }
  }

  async function confirmDelete() {
    setDeleting(true);
    try {
      await api.agents.remove(doomed.id);
      notify(t('Deleted "{name}"', { name: doomed.name }));
      setDoomed(null);
      await load();
    } catch (err) {
      notify(err.message, "error");
      setError(err.message);
      setDoomed(null);
    } finally {
      setDeleting(false);
    }
  }

  /* Map version status to design-system tag classes */
  function statusTag(status) {
    if (!status) return { cls: "tag tag-neutral", label: "Draft" };
    const map = {
      spec_pending_confirmation: { cls: "tag tag-accent", label: "Spec to review" },
      spec_confirmed:            { cls: "tag tag-accent", label: "Spec confirmed" },
      workflow_pending_confirmation: { cls: "tag tag-accent", label: "Editing workflow" },
      workflow_confirmed:        { cls: "tag tag-accent", label: "Workflow confirmed" },
      draft:                     { cls: "tag tag-neutral", label: "Draft" },
      sandbox_passed:            { cls: "tag tag-accent", label: "Sandbox passed" },
      pending_approval:          { cls: "tag tag-accent", label: "Pending review" },
      approved:                  { cls: "tag tag-accent", label: "Approved" },
      rejected:                  { cls: "tag tag-neutral", label: "Changes requested" },
    };
    return map[status] || { cls: "tag tag-neutral", label: status };
  }

  return (
    <PageShell>
        {/* Header */}
        <PageHead title={t('Your agents')} hint={t("Describe a task, review the spec, then build, test, and submit for approval.")}>
        <input
          ref={fileInput}
          type="file"
          accept="application/json,.json"
          onChange={importBundle}
          style={{ display: "none" }}
        />
        <button type="button" onClick={() => fileInput.current?.click()} disabled={importing} className="btn btn-secondary" style={{ height: 38 }} title={t("Create an agent from an exported JSON bundle")}>
          {importing ? <Spinner size={14} /> : <Upload size={14} />}{t("Import JSON")}
        </button>
        <button type="button" onClick={startDesigner} className="btn btn-primary" style={{ height: 38 }}>{t('New agent')}</button>
        </PageHead>

        <ErrorNote>{error}</ErrorNote>

        {loading && <div style={{ padding: "var(--space-12) var(--space-6)", textAlign: "center" }}><AgentMascot state="exploring" size={78} /></div>}

        {/* Empty state */}
        {!loading && mine.length === 0 && (
          <div style={{ border: "1px dashed var(--color-divider)", padding: "var(--space-10) var(--space-6) var(--space-12)", textAlign: "center", animation: "agIn .3s ease both" }}>
            {/* The one screen in the product with nothing on it — which makes it
                the one place a mascot is doing work rather than decorating. */}
            <AgentMascot state="welcome" size={140} style={{ margin: "0 auto 6px" }} />
            <p style={{ margin: "0 0 6px", fontFamily: "var(--font-heading)", fontSize: "var(--text-2xl)" }}>{t('You have not built anything yet')}</p>
            <p style={{ margin: "0 auto 18px", maxWidth: "46ch", fontSize: "var(--text-md)", color: "color-mix(in srgb, var(--color-text) 58%, transparent)" }}><strong>{t('New agent')}</strong> starts the interview — describe the task in your own words.</p>
            <button type="button" onClick={startDesigner} className="btn btn-primary">{t('New agent')}</button>
          </div>
        )}

        {/* Agent cards — blueprint style */}
        {!loading && mine.length > 0 && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(304px, 1fr))", gap: 18 }}>
            {mine.map((agent) => {
              const st = statusTag(agent.latest && agent.latest.status);
              return (
                <div
                  key={agent.id}
                  className="blueprint"
                  style={{ padding: "var(--space-4)", display: "flex", flexDirection: "column", gap: 11, minHeight: 172, animation: "agUp .4s ease both", cursor: "pointer" }}
                  role="button"
                  tabIndex={0}
                  onClick={() => openAgent(agent, agent.latest)}
                  onKeyDown={(e) => e.key === "Enter" && openAgent(agent, agent.latest)}
                >
                  <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
                  <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10 }}>
                    <span style={{ width: 30, height: 30, flexShrink: 0, display: "grid", placeItems: "center", border: "1px solid var(--color-divider)", color: "var(--color-accent)" }} dangerouslySetInnerHTML={{ __html: botIcon }} />
                    <span className={st.cls}>{t(st.label)}</span>
                  </div>
                  <div>
                    <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)", lineHeight: 1.2 }}>{agent.name}</p>
                    <p style={{ margin: "2px 0 0", fontFamily: "ui-monospace, monospace", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)" }}>
                      {t("{n} version(s)", { n: agent.versions.length })}
                    </p>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, marginTop: "auto", paddingTop: "var(--space-3)", borderTop: "1px solid var(--color-divider)" }}>
                    <DeployedBadge deployed={agent.is_deployed} />
                    <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
                      <button
                        type="button"
                        className="btn btn-secondary btn-icon"
                        title={t("Delete this agent")}
                        style={{ width: 28, height: 28, padding: "var(--space-1)" }}
                        onClick={(e) => { e.stopPropagation(); setDoomed(agent); }}
                        dangerouslySetInnerHTML={{ __html: trashIcon }}
                      />
                      <button type="button" className="btn btn-ghost" style={{ fontSize: "var(--text-sm)" }} onClick={(e) => { e.stopPropagation(); openAgent(agent, agent.latest); }}>{t("Open →")}</button>
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* ── Running agents — list rows ──
            A section of this page, so it says so with the section rule rather
            than with 44px of margin nothing else in the app uses. */}
        <Section
          title={t('Agents you can use')}
          hint={t("Published agents this account is allowed to reach — yours and anyone else's.")}
        >

          {running.length === 0 ? (
            <div style={{ border: "1px dashed var(--color-divider)", padding: "var(--space-10) var(--space-6)", textAlign: "center", fontSize: "var(--text-md)", color: "color-mix(in srgb, var(--color-text) 58%, transparent)" }}>{t('Nothing published to you yet.')}</div>
          ) : (
            <div style={{ border: "1px solid var(--color-divider)" }}>
              {running.map((agent) => (
                <button
                  key={agent.id}
                  type="button"
                  onClick={() => openChat(agent)}
                  style={{ display: "flex", alignItems: "center", gap: 16, width: "100%", padding: "var(--space-3) var(--space-4)", border: 0, borderBottom: "1px solid color-mix(in srgb, var(--color-text) 8%, transparent)", background: "transparent", font: "inherit", textAlign: "left", cursor: "pointer", color: "var(--color-text)" }}
                >
                  <span style={{ width: 7, height: 7, flexShrink: 0, borderRadius: "50%", background: "var(--color-accent)", animation: "agPulse 1.8s ease-in-out infinite" }} />
                  <span style={{ flex: 1, minWidth: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>{agent.name}</span>
                  <span style={{ fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)" }}>{t("by")} {agent.owner_email}</span>
                  <span className="tag tag-accent" style={{ flexShrink: 0 }}>{t('Published')}</span>
                  <span style={{ fontSize: "var(--text-sm)", color: "var(--color-accent)", flexShrink: 0 }}>{t("Chat →")}</span>
                </button>
              ))}
            </div>
          )}
        </Section>

      <ConfirmDialog
        open={Boolean(doomed)}
        busy={deleting}
        title={doomed ? t('Delete "{name}"?', { name: doomed.name }) : ""}
        confirmLabel={t("Delete permanently")}
        onCancel={() => setDoomed(null)}
        onConfirm={confirmDelete}
        body={
          doomed && (
            <>
              <p style={{ margin: "0 0 8px" }}>
{t("This cannot be undone. Its {n} version(s), every sandbox transcript, every evaluation run and its cost history go with it.", { n: doomed.versions.length })}
              </p>
              {doomed.is_deployed && (
                <p style={{ margin: 0, color: "var(--color-accent)" }}>
{t("This agent is published. If it is still serving, pause it in Approval & Deploy first — deleting something people are talking to is a decision worth taking twice.")}
                </p>
              )}
              <p style={{ margin: "8px 0 0" }}>
{t("Your uploaded documents are not deleted — they belong to your library, not to this agent.")}
              </p>
            </>
          )
        }
      />
    </PageShell>
  );
}
