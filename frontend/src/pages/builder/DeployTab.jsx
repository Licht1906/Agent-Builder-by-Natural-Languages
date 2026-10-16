import { useCallback, useEffect, useState } from "react";
import { Rocket, Copy, Check, KeyRound, Gauge, Send, GitCompare, Users, ExternalLink, Download } from "lucide-react";
import * as api from "../../lib/api";
import { ErrorNote, Hint, PageShell, Spinner, StatusBadge } from "../../components/ui";
import VersionCompare from "./VersionCompare";
import { useLang } from "../../i18n";
import { useAgentActivity } from "../../components/agent-presence";

/* ============================================================
   Everything between a finished workflow and somebody using it.

   Four decisions, deliberately four separate calls (ADR-0029): *which* version
   serves, *whether* it serves, *who* may reach it, and *how often* one caller
   may. One form that sent all four would let an owner pausing a deployment also
   widen it by sending a field they never looked at — so each panel below has
   its own Save, and each Save posts only its own field.
   ============================================================ */
export default function DeployTab({ agentId, versionId, versionStatus, onVersionsChanged, notify }) {
  const { t } = useLang();
  const [versions, setVersions] = useState([]);
  const [deployment, setDeployment] = useState(null);
  const [approval, setApproval] = useState(null);
  const [freshKey, setFreshKey] = useState(null);
  const [copied, setCopied] = useState(false);
  const [linkCopied, setLinkCopied] = useState(false);
  const [accessMode, setAccessMode] = useState("private");
  const [emails, setEmails] = useState("");
  const [rateLimit, setRateLimit] = useState(60);
  const [comparing, setComparing] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  /* One published Deployment, adopted whole. Every editable field on this screen
     is seeded from the response rather than kept across a save, so what is on
     screen after a save is what the server actually stored. */
  const adopt = useCallback((published) => {
    setDeployment(published);
    setAccessMode(published.access_mode);
    setEmails(published.allowed_users.map((user) => user.email).join(", "));
    setRateLimit(published.rate_limit_per_user);
  }, []);

  const load = useCallback(async () => {
    setError("");
    try {
      setVersions(await api.agents.versions(agentId));
    } catch (err) {
      setError(err.message);
    }
    try {
      adopt(await api.deployment.read(agentId));
    } catch {
      setDeployment(null); // Never published is a state, not a failure.
    }
    try {
      setApproval(await api.approvals.status(agentId, versionId));
    } catch {
      setApproval(null);
    }
  }, [agentId, versionId, adopt]);

  useEffect(() => { load(); }, [load]);

  async function act(kind, action, success) {
    setBusy(kind);
    setError("");
    try {
      await action();
      if (success) notify(success);
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy("");
    }
  }

  useAgentActivity("processing", busy === "submit", t("submitting for approval"));
  useAgentActivity("processing", busy === "status", t("changing what is served"));
  useAgentActivity("processing", Boolean(busy) && !["submit", "status"].includes(busy), t("saving the deployment"));

  const submitForApproval = () => act("submit", async () => {
    setApproval(await api.approvals.submit(agentId, versionId));
    await load();
    onVersionsChanged();
  }, t("Submitted for approval"));

  const publish = (targetVersionId) => act(`publish-${targetVersionId}`, async () => {
    const published = await api.deployment.publish(agentId, targetVersionId);
    adopt(published);
    // The plaintext key exists in this one response and nowhere else, so it is
    // held on screen rather than re-read later — there is nothing to re-read.
    if (published.api_key) setFreshKey(published.api_key);
    await load();
    onVersionsChanged();
  }, t("Published"));

  const toggleServing = () => act("status", async () => {
    adopt(await api.deployment.setStatus(agentId, deployment.status === "active" ? "disabled" : "active"));
  }, deployment && deployment.status === "active" ? t("Deployment paused") : t("Deployment is serving again"));

  const saveAccess = () => act("access", async () => {
    const list = accessMode === "selected_users"
      ? emails.split(",").map((entry) => entry.trim()).filter(Boolean)
      : [];
    adopt(await api.deployment.setAccess(agentId, accessMode, list));
  }, t("Access updated"));

  const saveRateLimit = () => act("rate", async () => {
    adopt(await api.deployment.setRateLimit(agentId, Number(rateLimit)));
  }, t("Rate limit updated"));

  const regenerateKey = () => act("key", async () => {
    setFreshKey((await api.deployment.regenerateKey(agentId)).api_key);
    setCopied(false);
  }, t("New API key minted — the old one stopped working"));

  /* Download this version as a bundle.
   *
   * The file is written from the response byte for byte — no re-serialising on
   * this side — so what lands on disk is what the server said the version is,
   * and `POST /agents/import` reads back exactly what `GET .../export` wrote. */
  const exportVersion = () => act("export", async () => {
    const bundle = await api.agents.exportVersion(agentId, versionId);
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    const shown = (versions.find((entry) => entry.id === versionId) || {}).number ?? versionId;
    link.download = `${(bundle.name || "agent").replace(/[^\w.-]+/g, "-").slice(0, 60)}-v${shown}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }, t("Bundle downloaded"));

  async function copy(text, mark) {
    try {
      await navigator.clipboard.writeText(text);
      mark(true);
      setTimeout(() => mark(false), 2000);
    } catch {
      notify(t("Could not reach the clipboard — select the text and copy it by hand."), "error");
    }
  }

  /* Where this agent lives on the web, for a person.
   *
   * Built from the page's own origin rather than a configured hostname: this
   * app is served from wherever it is served from, and a base URL kept in a
   * setting is a base URL that is wrong the first time somebody moves it. The
   * query string is the whole address because there is no router — `App` reads
   * `?agent=` on boot and lands on this agent's chat.
   *
   * Who may actually open it is a separate question, answered above by access
   * mode: the link is not the permission, and sending it to somebody outside
   * the allowlist gets them a refusal rather than a conversation. */
  const webUrl = `${window.location.origin}${window.location.pathname}?agent=${agentId}`;
  /* The API door, which is a different door: `/invoke` is the one endpoint the
     Deployment's own key opens (ADR-0029 §5) — the streaming chat endpoint above
     takes a signed-in session only. Naming the streaming one here would hand
     somebody a key and an address that refuses it. */
  const apiUrl = `${window.location.origin}/api/v1/agents/${agentId}/invoke`;

  const canSubmit = versionStatus === "workflow_confirmed" || versionStatus === "sandbox_passed";
  const serving = deployment && deployment.status === "active";

  /* What the owner has typed but not sent yet. Each Save is disabled until its
     own field differs from what the server last said, so a Save that is lit is
     always a Save that would change something. */
  const accessDirty = deployment && (
    accessMode !== deployment.access_mode ||
    emails.split(",").map((entry) => entry.trim()).filter(Boolean).join(",") !==
      deployment.allowed_users.map((user) => user.email).join(",")
  );
  const rateDirty = deployment && Number(rateLimit) !== deployment.rate_limit_per_user;

  return (
    <PageShell width="narrow">
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--gap-section)" }}>
        <ErrorNote>{error}</ErrorNote>

        {/* ---- Approval ---- */}
        <div className="blueprint" style={{ padding: "var(--space-5)" }}>
          <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
          <h2 style={{ margin: "0 0 4px", fontSize: "var(--text-2xl)" }}>{t('Approval')}<Hint text={t("A version reaches real users only after an admin has read its dossier. Submitting sends the graph, the permissions it asks for, and the evaluation score together.")} /></h2>
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <StatusBadge status={versionStatus} />
            {canSubmit && (
              <button className="btn btn-primary" onClick={submitForApproval} disabled={!!busy} style={{ height: 38 }}>
                {busy === "submit" ? <Spinner size={13} /> : <Send size={13} />}{t("Submit for approval")}
              </button>
            )}
            <span style={{ fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
              {versionStatus === "pending_approval" && t("An admin has this version in their queue.")}
              {versionStatus === "approved" && t("Approved — ready to publish below.")}
              {versionStatus === "rejected" && t("Changes requested by an admin.")}
              {!canSubmit && versionStatus !== "pending_approval" && versionStatus !== "approved" && versionStatus !== "rejected" && t("Confirm the workflow first.")}
            </span>
          </div>
          {approval && approval.rejection_reason && (
            <p style={{ fontSize: "var(--text-sm)", color: "#b91c1c", margin: "10px 0 0", lineHeight: 1.5 }}>“{approval.rejection_reason}”</p>
          )}
        </div>

        {/* ---- Publish ---- */}
        <div className="blueprint" style={{ padding: "var(--space-5)" }}>
          <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, marginBottom: 14 }}>
            <h2 style={{ margin: 0, fontSize: "var(--text-2xl)" }}>{t('Publish')}</h2>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span style={{ fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 60%, transparent)" }}>
                {deployment ? (serving ? t("Serving") : t("Paused")) : t("Not published")}
              </span>
              {deployment && (
                <button type="button" onClick={toggleServing} disabled={!!busy} className="btn btn-secondary" style={{ height: 36 }}>
                  {busy === "status" ? <Spinner size={13} /> : null}
                  {serving ? t("Pause deployment") : t("Resume deployment")}
                </button>
              )}
            </div>
          </div>

          {!deployment && (
            <p style={{ margin: 0, fontSize: "var(--text-sm)", lineHeight: 1.6, color: "color-mix(in srgb, var(--color-text) 60%, transparent)" }}>
              {t("Nothing is published yet. Publish an approved version from the history below — the first publish is also the one that mints this agent's API key.")}
            </p>
          )}

          {deployment && (
            <>
              {/* ---- Where it lives ---- */}
              <div className="field" style={{ marginBottom: 14 }}>
                <label>{t("Link to this agent")}</label>
                <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <a
                    href={webUrl}
                    target="_blank"
                    rel="noreferrer"
                    style={{
                      flex: 1, minWidth: 240, display: "flex", alignItems: "center", gap: 8,
                      padding: "var(--space-2) var(--space-3)", border: "1px solid var(--color-divider)", background: "var(--color-surface)",
                      fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)", textDecoration: "none",
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}
                    title={webUrl}
                  >
                    <ExternalLink size={14} style={{ flexShrink: 0 }} />
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{webUrl}</span>
                  </a>
                  <button
                    className="btn btn-secondary"
                    onClick={() => copy(webUrl, setLinkCopied)}
                    title={t("Opens this agent's chat. Anyone you send it to still has to sign in and be on the access list below — the link is an address, not a permission.")}
                    style={{ flex: "none", height: 36 }}
                  >
                    {linkCopied ? <Check size={13} /> : <Copy size={13} />}{linkCopied ? t("Copied") : t("Copy link")}
                  </button>
                </div>
                {!serving && (
                  <p style={{ margin: "5px 0 0", fontSize: "var(--text-xs)", color: "#b45309", lineHeight: 1.5 }}>
                    {t("Right now the deployment is paused, so the link answers with a refusal.")}
                  </p>
                )}
              </div>

              {/* Who may reach it */}
              <div className="field" style={{ marginBottom: 14 }}>
                <label>{t("Who may reach it")}<Hint text={t("Comma separated. The list is kept when you switch back to “only me”, so switching is not losing it.")} /></label>
                <div className="seg" style={{ marginBottom: 8 }}>
                  <label className="seg-opt">
                    <input type="radio" name="access" checked={accessMode === "private"} onChange={() => setAccessMode("private")} />
                    {t("Only me")}
                  </label>
                  <label className="seg-opt">
                    <input type="radio" name="access" checked={accessMode === "selected_users"} onChange={() => setAccessMode("selected_users")} />
                    {t("Named people")}
                  </label>
                </div>
                <textarea
                  id="allow"
                  className="input"
                  rows={2}
                  disabled={accessMode !== "selected_users"}
                  value={emails}
                  onChange={(e) => setEmails(e.target.value)}
                  placeholder="colleague@company.com, another@company.com"
                />
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 8, flexWrap: "wrap" }}>
                  <button className="btn btn-secondary" onClick={saveAccess} disabled={!!busy || !accessDirty} style={{ height: 34 }}>
                    {busy === "access" ? <Spinner size={13} /> : <Users size={13} />}{t("Save access")}
                  </button>
                </div>
              </div>

              {/* Rate limit + API key */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                <div className="field">
                  <label htmlFor="rate">{t("Requests per caller, per minute")}</label>
                  <div style={{ display: "flex", gap: 8 }}>
                    <input
                      id="rate"
                      className="input"
                      type="number"
                      min={1}
                      value={rateLimit}
                      onChange={(e) => setRateLimit(e.target.value)}
                      style={{ fontFamily: "ui-monospace, monospace" }}
                    />
                    <button className="btn btn-secondary" onClick={saveRateLimit} disabled={!!busy || !rateDirty} style={{ flex: "none" }}>
                      {busy === "rate" ? <Spinner size={13} /> : <Gauge size={13} />}{t("Save")}
                    </button>
                  </div>
                </div>
                <div className="field">
                  <label>{t('API key')}</label>
                  <button className="btn btn-secondary" onClick={regenerateKey} disabled={!!busy} style={{ width: "100%", height: 36 }}>
                    {busy === "key" ? <Spinner size={13} /> : <KeyRound size={13} />}{t("Mint a new key")}
                  </button>
                  <p style={{ margin: "5px 0 0", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)" }}>
                    {t("The old key stops working the moment a new one exists.")}
                  </p>
                </div>
              </div>

              {/* The plaintext key, for as long as this screen is open */}
              {freshKey && (
                <div style={{ marginTop: 14, border: "1px solid var(--color-accent)", background: "var(--color-accent-100)", color: "var(--color-accent-800)", padding: "var(--space-3)" }}>
                  <p style={{ margin: "0 0 8px", fontSize: "var(--text-sm)" }}>
                    {t("Copy this now — the plaintext exists in this response and nowhere else. Leaving this screen loses it.")}
                  </p>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <code style={{ flex: 1, minWidth: 0, fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)", overflowX: "auto", whiteSpace: "nowrap", padding: "var(--space-2)", background: "var(--color-bg)", border: "1px solid var(--color-divider)", color: "var(--color-text)" }}>
                      {freshKey}
                    </code>
                    <button className="btn btn-secondary" onClick={() => copy(freshKey, setCopied)} style={{ flex: "none", height: 34 }}>
                      {copied ? <Check size={13} /> : <Copy size={13} />}{copied ? t("Copied") : t("Copy")}
                    </button>
                  </div>
                  {/* A key with no address to send it to is not usable, and this
                      is the moment somebody has the key in hand. */}
                  <p style={{ margin: "10px 0 0", fontSize: "var(--text-xs)", lineHeight: 1.6 }}>
                    {t("Send it as")} <span style={{ fontFamily: "ui-monospace, monospace" }}>X-API-Key</span> {t("to")}{" "}
                    <span style={{ fontFamily: "ui-monospace, monospace", wordBreak: "break-all" }}>{apiUrl}</span>
                  </p>
                </div>
              )}
            </>
          )}
        </div>

        {/* ---- Version history ---- */}
        <div className="blueprint" style={{ padding: "var(--space-5)" }}>
          <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, marginBottom: 10, flexWrap: "wrap" }}>
            <h2 style={{ margin: 0, fontSize: "var(--text-2xl)" }}>{t('Version history')}</h2>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button className="btn btn-secondary" onClick={exportVersion} disabled={!!busy} style={{ height: 34 }}>
                {busy === "export" ? <Spinner size={13} /> : <Download size={13} />}{t("Export this version")}
              </button>
              {versions.length > 1 && (
                <button className="btn btn-secondary" onClick={() => setComparing((shown) => !shown)} style={{ height: 34 }}>
                  <GitCompare size={13} />{comparing ? t("Hide comparison") : t("Compare two versions")}
                </button>
              )}
            </div>
          </div>

          {comparing && (
            <div style={{ marginBottom: 16, paddingBottom: "var(--space-4)", borderBottom: "1px solid var(--color-divider)" }}>
              <VersionCompare agentId={agentId} versions={versions} />
            </div>
          )}

          <div style={{ overflowX: "auto" }}>
            <table className="table">
              <thead>
                <tr>
                  <th>{t('Version')}</th>
                  <th>{t('Status')}</th>
                  <th>{t('Created')}</th>
                  <th style={{ textAlign: "right" }}>{t('Action')}</th>
                </tr>
              </thead>
              <tbody>
                {[...versions].reverse().map((version) => (
                  <tr key={version.id}>
                    <td style={{ fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)" }}>
                      #{version.number}
                      {deployment && deployment.agent_version_id === version.id && (
                        <span className="tag tag-accent" style={{ marginLeft: 8 }}>{t('serving')}</span>
                      )}
                    </td>
                    <td><StatusBadge status={version.status} /></td>
                    <td style={{ fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
                      {new Date(version.created_at).toLocaleString()}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {version.status === "approved" && !version.is_deployed && (
                        <button className="btn btn-primary" onClick={() => publish(version.id)} disabled={!!busy} style={{ height: 30, fontSize: "var(--text-sm)" }}>
                          {busy === `publish-${version.id}` ? <Spinner size={12} /> : <Rocket size={12} />}{t("Publish")}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </PageShell>
  );
}
