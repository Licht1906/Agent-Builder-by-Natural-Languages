import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Archive, Inbox, Lock, ShieldCheck } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, Hint, PageHead, PageShell, Spinner, StatusBadge } from "../components/ui";
import { lookOf } from "../lib/nodes";
import { useLang } from "../i18n";
import { AgentMascot } from "../components/mascot";

/* ============================================================
   The admin's review surface (#14, widened by ADR-0064).

   The queue includes versions the eval bar is holding shut, marked
   `blocked_by_eval`: they cannot be approved and are not asking to be, but the
   override that opens them is the admin's and an admin who cannot see one has no
   way to reach it.

   The second tab is what ADR-0064 adds. A dossier used to be reachable only
   *from* the queue, so deciding a version put its evidence out of everyone's
   reach — including the admin who decided it. "Đang chờ" stays the default and
   stays exactly as noisy as it was; "Đã quyết" is a different list, read for a
   different reason, and it opens the same dossier with every button gone.
   ============================================================ */

/** Whether a version's review is over. The API serves the dossier either way —
 *  it always has — so this is the one place that decides whether the screen may
 *  offer a decision, and it answers from `status` rather than from a flag that
 *  could disagree with it. `approve`/`reject` refuse anything but
 *  `pending_approval`, and neither of these two transitions back. */
export const isSettled = (status) => status === "approved" || status === "rejected";

export function AdminQueue({ openVersion, tab = "pending", setTab }) {
  const { t } = useLang();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const decidedView = tab === "decided";

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      // Asked for explicitly rather than by filtering one payload: the two lists
      // do not share a row shape, and a screen that fetched both to show one
      // would pay for the whole decision history to render the queue.
      setRows(decidedView ? await api.approvals.decided() : await api.approvals.queue());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [decidedView]);

  useEffect(() => { load(); }, [load]);

  return (
    <PageShell>
        <PageHead
          title={t('Review queue')}
          hint={decidedView
            ? t("Reviews that are over, most recently decided first. Read-only — a dossier here shows what was decided, not a way to decide again.")
            : t("Versions waiting on a decision, oldest first. Every row opens the dossier — nothing gets approved from the list.")}
        />

        <div className="ab-tabs" style={{ padding: 0, marginBottom: 18 }}>
          {[{ id: "pending", label: t("Awaiting you"), icon: Inbox }, { id: "decided", label: t("Decided"), icon: Archive }].map((entry) => (
            <button
              key={entry.id}
              type="button"
              className={`ab-tab${tab === entry.id ? " active" : ""}`}
              onClick={() => setTab && setTab(entry.id)}
            >
              <entry.icon size={14} />{entry.label}
            </button>
          ))}
        </div>

        <ErrorNote>{error}</ErrorNote>
        {loading && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}

        {!loading && rows.length === 0 && (
          <div style={{ border: "1px dashed var(--color-divider)", padding: "var(--space-12) var(--space-6)", textAlign: "center", fontSize: "var(--text-md)", color: "color-mix(in srgb, var(--color-text) 58%, transparent)" }}>
            {decidedView
              ? t("Nothing has been decided yet. Approved and rejected versions stay readable here.")
              : t("Nothing is waiting on you. Versions appear here when a creator submits one.")}
          </div>
        )}

        {decidedView && (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {rows.map((row) => <DecidedRow key={row.version_id} row={row} open={() => openVersion(row.version_id)} />)}
          </div>
        )}

        {!decidedView && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {rows.map((row) => {
            const blocked = row.state === "blocked_by_eval";
            return (
              <div
                key={row.version_id}
                className="blueprint"
                style={{ padding: "var(--space-4)", borderColor: blocked ? "rgba(245,158,11,.45)" : "var(--color-divider)", animation: "agUp .35s ease both" }}
              >
                <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
                <div style={{ display: "flex", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
                  <p style={{ margin: 0, flex: "1 1 auto", minWidth: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)", lineHeight: 1.25 }}>
                    {row.agent_name}{" "}
                    <span style={{ fontFamily: "var(--font-body)", fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)" }}>
— {t("version")} #{row.version_id}
                    </span>
                  </p>
                  {blocked ? (
                    <span className="tag" style={{ marginLeft: "auto", color: "#b45309", background: "rgba(245,158,11,.12)", border: "1px solid rgba(245,158,11,.35)", gap: 5 }}>
                      <AlertTriangle size={11} />{t("blocked by eval")}
                    </span>
                  ) : (
                    <span className="tag tag-outline" style={{ marginLeft: "auto" }}>{t('Awaiting decision')}</span>
                  )}
                </div>

                <p style={{ margin: "6px 0 0", fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
                  {row.owner_email}
                </p>
                <p style={{ margin: "8px 0 0", fontSize: "var(--text-sm)" }}>
                  {t("Evaluation")} {row.score ? `${row.score.passed_count}/${row.score.total_count}` : t("— not run")}
                </p>

                {row.blocked_reason && (
                  <p style={{ margin: "8px 0 0", padding: "var(--space-2) var(--space-3)", fontSize: "var(--text-sm)", lineHeight: 1.55, border: "1px solid rgba(245,158,11,.4)", background: "rgba(245,158,11,.08)", color: "#b45309" }}>
                    {row.blocked_reason}
                  </p>
                )}

                {row.high_risk_tools.length > 0 && (
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
                    <span style={{ fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>{t('High-risk permissions:')}</span>
                    {row.high_risk_tools.map((tool) => (
                      <span key={tool} className="tag tag-outline" style={{ fontFamily: "ui-monospace, monospace", fontSize: "var(--text-xs)" }}>{tool}</span>
                    ))}
                  </div>
                )}

                <div style={{ display: "flex", gap: 10, marginTop: 14, paddingTop: "var(--space-3)", borderTop: "1px solid var(--color-divider)" }}>
                  <button type="button" className="btn btn-primary" onClick={() => openVersion(row.version_id)}>{t('Open dossier')}</button>
                </div>
              </div>
            );
          })}
        </div>
        )}
      </PageShell>
  );
}

/* One review that is over.
 *
 * Says what was decided, by whom and when — and says so plainly when nobody
 * knows. `decided_by` is null for a version that reached `approved` without an
 * `ApprovalDecision` behind it, which is a real state of this platform's data,
 * and the row prints "không có bản ghi quyết định" rather than the version's
 * creation date dressed up as a decision date. A made-up timestamp is
 * indistinguishable from a real one, which is exactly what makes it worse than
 * a blank. */
function DecidedRow({ row, open }) {
  const { t } = useLang();
  const rejected = row.status === "rejected";
  return (
    <div
      className="blueprint"
      style={{ padding: "var(--space-4)", borderColor: rejected ? "rgba(239,68,68,.35)" : "var(--color-divider)", animation: "agUp .35s ease both" }}
    >
      <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
        <p style={{ margin: 0, flex: "1 1 auto", minWidth: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)", lineHeight: 1.25 }}>
          {row.agent_name}{" "}
          <span style={{ fontFamily: "var(--font-body)", fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)" }}>
— {t("version")} #{row.version_id}
          </span>
        </p>
        <span style={{ marginLeft: "auto" }}><StatusBadge status={row.status} /></span>
      </div>

      <p style={{ margin: "6px 0 0", fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
        {row.owner_email}
      </p>
      <p style={{ margin: "8px 0 0", fontSize: "var(--text-sm)" }}>
        {row.decided_by
          ? `${t("Decided by")} ${row.decided_by} · ${new Date(row.decided_at).toLocaleString()}`
          : t("No decision record — nobody can say who decided this or when.")}
        {row.score ? ` · ${t("Evaluation")} ${row.score.passed_count}/${row.score.total_count}` : ""}
      </p>

      {row.note && (
        <p style={{ margin: "8px 0 0", padding: "var(--space-2) var(--space-3)", fontSize: "var(--text-sm)", lineHeight: 1.55, border: "1px solid var(--color-divider)", background: "color-mix(in srgb, var(--color-text) 4%, transparent)" }}>
          {row.note}
        </p>
      )}

      {row.eval_override_reason && (
        <p style={{ margin: "8px 0 0", padding: "var(--space-2) var(--space-3)", fontSize: "var(--text-sm)", lineHeight: 1.55, border: "1px solid rgba(245,158,11,.4)", background: "rgba(245,158,11,.08)", color: "#b45309" }}>
          {t("Eval bar waived:")} {row.eval_override_reason}
        </p>
      )}

      <div style={{ display: "flex", gap: 10, marginTop: 14, paddingTop: "var(--space-3)", borderTop: "1px solid var(--color-divider)" }}>
        <button type="button" className="btn btn-secondary" onClick={open}>{t('Open dossier')}</button>
      </div>
    </div>
  );
}

export function AdminDossier({ versionId, back, notify }) {
  const { t } = useLang();
  const [dossier, setDossier] = useState(null);
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setDossier(await api.approvals.dossier(versionId));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [versionId]);

  useEffect(() => { load(); }, [load]);

  async function decide(kind) {
    if ((kind === "reject" || kind === "override") && !reason.trim()) {
      setError(t("A rejection and an override both need a reason on the record."));
      return;
    }
    setBusy(kind);
    setError("");
    try {
      if (kind === "approve") await api.approvals.approve(versionId);
      if (kind === "reject") await api.approvals.reject(versionId, reason.trim());
      if (kind === "override") await api.approvals.overrideEval(versionId, reason.trim());
      notify(kind === "approve" ? t("Version approved") : kind === "reject" ? t("Sent back to the creator") : t("Eval bar waived for this version"));
      setReason("");
      if (kind === "override") await load();
      else back();
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy("");
    }
  }

  const blocked = dossier && dossier.blocked_reason;
  //: Read off `status`, so the screen and the API agree by construction about
  //  whether a decision is still possible here (ADR-0011 §5).
  const settled = !!dossier && isSettled(dossier.status);

  return (
    <PageShell>
        <button type="button" onClick={back} className="btn btn-ghost" style={{ fontSize: "var(--text-sm)", marginBottom: 12 }}>{t('← Review queue')}</button>

        <ErrorNote>{error}</ErrorNote>
        {loading && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}

        {dossier && (
          <>
            <PageHead title={dossier.agent_name} />
            <div className="ab-row" style={{ marginBottom: "var(--space-5)" }}>
              <StatusBadge status={dossier.status} />
              <span className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)" }}>
                {t("version")} #{dossier.version_id} · {dossier.owner_email} · {new Date(dossier.created_at).toLocaleString()}
              </span>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "1.2fr .8fr", gap: 20, alignItems: "start" }}>
              {/* ── The case being made ── */}
              <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
                {dossier.solution_spec && (
                  <div className="blueprint" style={{ padding: "var(--space-4)" }}>
                    <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
                    <h2 style={{ margin: "0 0 10px", fontSize: "var(--text-xl)" }}>{t('What it is for')}</h2>
                    <p style={{ margin: 0, fontSize: "var(--text-md)", lineHeight: 1.6 }}>{dossier.solution_spec.goal}</p>
                    <SpecList label="Actions" items={dossier.solution_spec.actions} />
                    <SpecList label="Prohibitions" items={dossier.solution_spec.prohibitions} danger />
                  </div>
                )}

                {dossier.actions && dossier.actions.length > 0 && (
                  <div className="blueprint" style={{ padding: "var(--space-4)" }}>
                    <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
                    <h2 style={{ margin: "0 0 3px", fontSize: "var(--text-xl)" }}>{t('Permissions it is asking for')}<Hint text={t("Risk is the registry's verdict, not the creator's — high risk first.")} /></h2>
                    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                      {dossier.actions.map((action) => {
                        const risky = action.risk === "high";
                        return (
                          <div key={action.name} style={{ display: "flex", alignItems: "flex-start", gap: 12, border: `1px solid ${risky ? "rgba(239,68,68,.4)" : "var(--color-divider)"}`, padding: "var(--space-2) var(--space-3)" }}>
                            <div style={{ flex: "1 1 auto", minWidth: 0 }}>
                              <p style={{ margin: 0, fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{action.name}</p>
                              <p style={{ margin: "2px 0 0", fontSize: "var(--text-sm)", lineHeight: 1.45, color: "color-mix(in srgb, var(--color-text) 58%, transparent)" }}>
                                {action.description || t("The registry does not know this action")}
                              </p>
                            </div>
                            <span
                              className="tag"
                              style={{ flex: "none", ...(risky
                                ? { color: "#b91c1c", background: "rgba(239,68,68,.1)", border: "1px solid rgba(239,68,68,.3)" }
                                : { background: "var(--color-neutral-100)", color: "var(--color-neutral-800)" }) }}
                            >
                              {t("risk")}: {t(action.risk || "unknown")}
                            </span>
                            {action.requires_human_approval && <span className="tag tag-outline" style={{ flex: "none" }}>{t("needs approval")}</span>}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                {dossier.workflow && (
                  <div className="blueprint" style={{ padding: "var(--space-4)" }}>
                    <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
                    <h2 style={{ margin: "0 0 3px", fontSize: "var(--text-xl)" }}>{t('The graph it runs')}</h2>
                    <p style={{ margin: "0 0 12px", fontFamily: "ui-monospace, monospace", fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
                      {t("starts at")} {dossier.workflow.start_node_id} · {t("{n} nodes", { n: dossier.workflow.nodes.length })} · {t("{n} connections", { n: dossier.workflow.edges.length })}
                    </p>
                    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start" }}>
                      {dossier.workflow.nodes.map((node) => {
                        const look = lookOf(node.type);
                        return (
                          <div key={node.id} style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", width: "100%" }}>
                            <div style={{ width: "100%", display: "flex", alignItems: "center", gap: 11, border: "1px solid var(--color-divider)", padding: "var(--space-2) var(--space-3)" }}>
                              <span style={{ width: 22, height: 22, flex: "none", display: "grid", placeItems: "center", color: "var(--color-accent)" }}>
                                <look.icon size={14} />
                              </span>
                              <span style={{ flex: 1, minWidth: 0, fontSize: "var(--text-md)" }}>{t(look.label)}</span>
                              <span style={{ fontFamily: "ui-monospace, monospace", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 48%, transparent)" }}>{node.id}</span>
                            </div>
                            <span style={{ width: 1, height: 16, marginLeft: 23, background: "var(--color-divider)" }} />
                          </div>
                        );
                      })}
                      <span style={{ fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)", marginLeft: 16 }}>{t('End')}</span>
                    </div>
                  </div>
                )}
              </div>

              {/* ── The evidence, and the decision ── */}
              <div style={{ display: "flex", flexDirection: "column", gap: 18, position: "sticky", top: 0 }}>
                <div className="blueprint" style={{ padding: "var(--space-4)" }}>
                  <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
                  <h2 style={{ margin: "0 0 12px", fontSize: "var(--text-xl)" }}>
                    {t('Evidence')}
                    <Hint text={t("Sandbox transcript and every graded case are attached to this version — the score above is the same number the gate reads.")} />
                  </h2>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 1, background: "var(--color-divider)", border: "1px solid var(--color-divider)", marginBottom: 14 }}>
                    <Evidence label="latest trial run" value={dossier.sandbox_run ? dossier.sandbox_run.status : "none"} />
                    <Evidence label="eval" value={dossier.eval_run ? `${dossier.eval_run.passed_count}/${dossier.eval_run.total_count}` : "—"} />
                    {dossier.eval_breakdown && (
                      <>
                        <Evidence
                          label="read off the transcript"
                          value={`${dossier.eval_breakdown.rule_based.passed_count}/${dossier.eval_breakdown.rule_based.total_count}`}
                        />
                        <Evidence
                          label="safety failures"
                          value={dossier.eval_breakdown.safety_failures}
                          tone={dossier.eval_breakdown.safety_failures ? "#b91c1c" : undefined}
                        />
                      </>
                    )}
                  </div>
                  {blocked && (
                    <p style={{ margin: "12px 0 0", padding: "var(--space-2) var(--space-3)", fontSize: "var(--text-sm)", lineHeight: 1.6, border: "1px solid rgba(245,158,11,.4)", background: "rgba(245,158,11,.08)", color: "#b45309" }}>
                      {dossier.blocked_reason}
                    </p>
                  )}
                  {dossier.suite_changes && <SuiteChanges changes={dossier.suite_changes} />}
                </div>

                {/* Settled reviews get the record instead of the controls — not
                    the controls disabled. A greyed-out Approve still says the
                    review is a thing that could be reopened here, and it cannot:
                    `approve` and `reject` refuse anything but `pending_approval`.
                    Re-deciding is not a feature of this screen (ADR-0064). */}
                {settled ? (
                  <div className="blueprint" style={{ padding: "var(--space-4)" }}>
                    <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
                    <h2 style={{ margin: "0 0 10px", fontSize: "var(--text-xl)" }}>
                      {t('The decision')}
                      <Hint text={t("This review is closed. The dossier stays readable so the decision can be checked against what it rested on.")} />
                    </h2>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <StatusBadge status={dossier.status} />
                      <span className="tag tag-outline" style={{ gap: 5 }}><Lock size={11} />{t("read-only")}</span>
                    </div>
                    {dossier.rejection_reason && (
                      <p style={{ margin: "12px 0 0", padding: "var(--space-2) var(--space-3)", fontSize: "var(--text-sm)", lineHeight: 1.6, border: "1px solid rgba(239,68,68,.35)", background: "rgba(239,68,68,.07)", color: "#b91c1c" }}>
                        {dossier.rejection_reason}
                      </p>
                    )}
                    {dossier.decisions.length === 0 && (
                      <p style={{ margin: "12px 0 0", fontSize: "var(--text-sm)", lineHeight: 1.6, color: "color-mix(in srgb, var(--color-text) 62%, transparent)" }}>
                        {t("No decision record — nobody can say who decided this or when.")}
                      </p>
                    )}
                    <DecisionHistory decisions={dossier.decisions} />
                  </div>
                ) : (
                <div className="blueprint" style={{ padding: "var(--space-4)" }}>
                  <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
                  <h2 style={{ margin: "0 0 10px", fontSize: "var(--text-xl)" }}>{t('Decide')}</h2>
                  <textarea
                    className="input"
                    rows={4}
                    aria-label={t("Reason")}
                    placeholder={t("Reason — required to reject or to waive the eval bar. The creator reads this.")}
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                  />
                  <div style={{ display: "flex", gap: 10, marginTop: 12 }}>
                    <button className="btn btn-primary" style={{ flex: 1 }} onClick={() => decide("approve")} disabled={!!busy}>
                      {busy === "approve" ? <Spinner size={13} /> : <ShieldCheck size={13} />}{t("Approve")}
                    </button>
                    <button className="btn btn-secondary" style={{ flex: 1 }} onClick={() => decide("reject")} disabled={!!busy}>
                      {busy === "reject" ? <Spinner size={13} /> : null}{t("Request changes")}
                    </button>
                  </div>
                  {blocked && (
                    <button className="btn btn-secondary btn-block" onClick={() => decide("override")} disabled={!!busy}>
                      {busy === "override" ? <Spinner size={13} /> : <AlertTriangle size={13} />}{t("Waive the eval bar")}
                    </button>
                  )}

                  <DecisionHistory decisions={dossier.decisions} />
                </div>
                )}
              </div>
            </div>
          </>
        )}
      </PageShell>
  );
}

/* The audit trail, oldest first — the same list under the Decide panel and under
 * a closed review's record. One component because it is one thing: an admin
 * about to decide reads it to see whether this version has been round the loop
 * before, and an auditor reads it to see what happened. Two copies would be two
 * places to remember the next time a decision gains a field.
 *
 * `eval_score` is the snapshot the decision was made against, not a fresh
 * reading, which is the whole reason it is stored — and it is shown beside an
 * override because "this bar was waived, and here is how far short it was" is
 * the one sentence that record exists to be able to say. */
function DecisionHistory({ decisions }) {
  const { t } = useLang();
  if (!decisions || decisions.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 16, paddingTop: "var(--space-3)", borderTop: "1px solid var(--color-divider)" }}>
      <p style={{ margin: 0, fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: "color-mix(in srgb, var(--color-text) 50%, transparent)" }}>{t('Already decided')}</p>
      {decisions.map((decision) => (
        <div key={decision.id} className="ab-version-item" style={{ alignItems: "flex-start" }}>
          <span className="tag tag-neutral" style={{ flex: "none" }}>{decision.kind.replace(/_/g, " ")}</span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <p style={{ fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.5 }}>{decision.reason || t("No note")}</p>
            <p style={{ fontFamily: "ui-monospace, monospace", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 52%, transparent)", margin: "2px 0 0" }}>
              {decision.admin_email} · {new Date(decision.created_at).toLocaleString()}
              {decision.eval_score ? ` · eval ${decision.eval_score.passed_count}/${decision.eval_score.total_count}` : ""}
            </p>
          </div>
        </div>
      ))}
    </div>
  );
}

function Evidence({ label, value, tone }) {
  const { t } = useLang();
  return (
    <div style={{ background: "var(--color-bg)", padding: "var(--space-3)" }}>
      <p style={{ margin: 0, fontSize: "var(--text-2xs)", letterSpacing: ".13em", textTransform: "uppercase", color: "color-mix(in srgb, var(--color-text) 52%, transparent)" }}>{t(label)}</p>
      <p style={{ margin: "4px 0 0", fontFamily: "var(--font-heading)", fontSize: "var(--text-2xl)", lineHeight: 1, color: tone || "var(--color-text)" }}>{value}</p>
    </div>
  );
}

function SpecList({ label, items, danger }) {
  const { t } = useLang();
  if (!items || items.length === 0) return null;
  return (
    <div style={{ marginTop: 12 }}>
      <p style={{ margin: "0 0 4px", fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>{t(label)}</p>
      {items.map((item, index) => (
        <p key={index} style={{ fontSize: "var(--text-sm)", color: danger ? "#b91c1c" : "color-mix(in srgb, var(--color-text) 70%, transparent)", margin: "3px 0", lineHeight: 1.5 }}>· {item}</p>
      ))}
    </div>
  );
}


/* EV-05 / ADR-0069 §6: what happened to the suite since the baseline, in two
   groups — what the system retired (and how many of those were failing at the
   baseline: the road around the gate is rewording a criterion to shed failing
   cases), and what the creator edited or deleted by hand — plus the cases with
   no baseline yet. */
function SuiteChanges({ changes }) {
  const { t } = useLang();
  const creatorEdits = changes.creator_changes.filter((change) => change.kind === "edited").length;
  const creatorDeletes = changes.creator_changes.filter((change) => change.kind === "deleted").length;
  return (
    <div style={{ marginTop: 14 }}>
      <h3 style={{ margin: "0 0 8px", fontSize: "var(--text-base)" }}>
        {t("Suite changes")}
        <span style={{ fontWeight: 400, fontSize: "var(--text-xs)", marginLeft: 8, opacity: 0.7 }}>
          {changes.baseline_version_id != null ? t("since baseline v{v}", { v: changes.baseline_version_id }) : t("no baseline yet")}
        </span>
      </h3>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 1, background: "var(--color-divider)", border: "1px solid var(--color-divider)" }}>
        <Evidence
          label="retired by the system"
          value={t("{n} ({k} failing at the baseline)", { n: changes.retired.length, k: changes.retired_failing_count })}
          tone={changes.retired_failing_count ? "#b45309" : undefined}
        />
        <Evidence label="edited / deleted by the creator" value={`${creatorEdits} / ${creatorDeletes}`} />
        <Evidence label="new cases, no baseline" value={changes.new_cases.length} />
      </div>
      {(changes.retired.length > 0 || changes.creator_changes.length > 0) && (
        <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: "var(--text-sm)", lineHeight: 1.6 }}>
          {changes.retired.map((entry) => (
            <li key={`r${entry.id}`}>
              {t("retired")}: <b>{entry.criterion}</b> — {entry.input_message}
              {entry.retired_failing_at_baseline && <span style={{ color: "#b45309" }}> · {t("was failing at the baseline")}</span>}
            </li>
          ))}
          {changes.creator_changes.map((entry, index) => (
            <li key={`c${index}`}>
              {t(entry.kind === "deleted" ? "deleted" : "edited")} {t("by {who}", { who: entry.actor_email || "—" })}: <b>{entry.criterion}</b> — {entry.input_message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

