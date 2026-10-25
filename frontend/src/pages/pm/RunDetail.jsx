import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, XCircle, ThumbsUp, ThumbsDown, ExternalLink } from "lucide-react";
import * as api from "../../lib/api";
import { ErrorNote, Spinner } from "../../components/ui";
import { useLang } from "../../i18n";
import { Drawer, Empty, SectionLabel, faint, muted, usd } from "./shared";

/* ============================================================
   The review screen (slide 62): one run, evidence first, one decision.

   Drawn in two places — as a drawer over the runs table ("read"), and as the
   whole Review tab, where it walks the queue — so the screen is one component
   (`RunReview`) and the two hosts only decide where it sits. The rule it
   exists to keep: the judge's opinion stays out of sight until the person has
   decided (slide 61), and G/B are the only keys it listens to.
   ============================================================ */

/** Loads one run (by id) or the next one to review; `detail === null` after
 *  `loaded` means the queue is empty. */
export function useRunDetail({ runId, reviewMode = false, includeSimulated = false }) {
  const [detail, setDetail] = useState(undefined);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    setError("");
    try {
      setDetail((await (reviewMode ? api.pm.reviewNext(includeSimulated) : api.pm.run(runId))) ?? null);
    } catch (err) {
      setError(err.message);
    }
  }, [reviewMode, includeSimulated, runId]);
  useEffect(() => { load(); }, [load]);
  return { detail, error, loaded: detail !== undefined, reload: load };
}

export function TranscriptLine({ line, series }) {
  const { t } = useLang();
  const id = line.node_id || line.step_id;
  if (line.kind === "tool") {
    return (
      <div style={{ display: "flex", gap: 8, fontSize: "var(--text-xs)", padding: "var(--space-1) var(--space-2)", borderLeft: `3px solid ${series.gray}` }}>
        <span className="ab-mono" style={{ color: faint, flex: "none" }}>{id}</span>
        <span><strong>{t("called")}</strong> <span className="ab-mono">{line.tool}</span>{line.simulated ? <span className="tag tag-neutral" style={{ marginLeft: 6, fontSize: "var(--text-2xs)" }}>{t("simulated")}</span> : null}<pre className="ab-code" style={{ margin: "4px 0 0", fontSize: "var(--text-2xs)", whiteSpace: "pre-wrap" }}>{JSON.stringify(line.args || {}, null, 1)}</pre></span>
      </div>
    );
  }
  if (line.kind === "tool_result") {
    return (
      <div style={{ display: "flex", gap: 8, fontSize: "var(--text-xs)", padding: "var(--space-1) var(--space-2)", borderLeft: `3px solid ${line.ok === false ? series.red : series.blue}` }}>
        <span className="ab-mono" style={{ color: faint, flex: "none" }}>{id}</span>
        <span style={{ minWidth: 0 }}><strong style={{ color: line.ok === false ? series.red : undefined }}>{line.ok === false ? t("tool failed") : t("tool returned")}</strong> <span className="ab-mono">{line.tool}</span><pre className="ab-code ab-scrollbar" style={{ margin: "4px 0 0", fontSize: "var(--text-2xs)", whiteSpace: "pre-wrap", maxHeight: 220, overflow: "auto" }}>{line.content}</pre></span>
      </div>
    );
  }
  if (line.kind === "budget_exceeded") {
    return <div style={{ fontSize: "var(--text-xs)", padding: "var(--space-1) var(--space-2)", borderLeft: `3px solid ${series.red}` }}><strong>{t("stopped by budget")}</strong> — {line.content}</div>;
  }
  const spoken = line.kind === "step" || ["agent_llm", "collect"].includes(line.node_type);
  return (
    <div style={{ display: "flex", gap: 8, fontSize: "var(--text-xs)", padding: "var(--space-1) var(--space-2)", borderLeft: `3px solid ${spoken ? series.blue : series.gray}` }}>
      <span className="ab-mono" style={{ color: faint, flex: "none" }}>{id}<div style={{ fontSize: "var(--text-2xs)" }}>{line.node_type || line.title || "step"}{line.port_taken ? ` → ${line.port_taken}` : ""}</div></span>
      <span style={{ whiteSpace: "pre-wrap", minWidth: 0 }}>{line.content}</span>
    </div>
  );
}

/** The screen itself. `onSaved` runs after a label is filed — the drawer
 *  reloads the same run, the Review tab asks for the next one. */
export function RunReview({ detail, reviewMode = false, codes, notify, onChanged, onSaved, series }) {
  const { t } = useLang();
  const [passed, setPassed] = useState(null);
  const [picked, setPicked] = useState([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [showJudge, setShowJudge] = useState(false);
  const noteRef = useRef(null);

  // A fresh verdict form for every run that arrives on this screen.
  useEffect(() => { setPassed(null); setPicked([]); setNote(""); setShowJudge(false); }, [detail.id]);

  // G = good (Đạt), B = bad (Không đạt) — one decision per screen (slide 62).
  useEffect(() => {
    const onKey = (e) => {
      if (document.activeElement === noteRef.current) return;
      if (e.key === "g" || e.key === "G") setPassed(true);
      if (e.key === "b" || e.key === "B") setPassed(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  async function submit() {
    if (passed == null) return;
    setBusy(true);
    try {
      const out = await api.pm.label(detail.id, passed, passed ? [] : picked, note);
      notify(passed ? t("Labelled: pass") : t("Labelled: fail ({codes})", { codes: out.codes.join(", ") }));
      setPassed(null); setPicked([]); setNote(""); setShowJudge(false);
      onChanged();
      await onSaved();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  }

  const judgeHidden = detail.judge_hidden_until_labelled && !showJudge && passed == null;
  const latestLabel = detail.labels[detail.labels.length - 1];
  const thumbs = detail.scores.filter((s) => s.name === "thumbs_up");
  const codeScores = detail.scores.filter((s) => s.source === "code");

  return (
    <>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: "var(--text-xs)", alignItems: "center" }}>
        <span style={{ fontFamily: "var(--font-heading)", fontSize: "var(--text-md)" }}>{detail.agent_name}</span>
        <span className="ab-mono" style={{ color: faint }}>v{detail.version_key} · {detail.pipeline} · {detail.trigger} · {new Date(detail.created_at).toLocaleString()}</span>
        <span className={`tag ${detail.outcome === "success" ? "tag-accent" : "tag-neutral"}`}>{detail.outcome}</span>
        <span className="ab-mono" style={{ color: muted }}>{usd(detail.cost_usd)} · {detail.latency_seconds.toFixed(1)}s · {detail.tokens.toLocaleString()} tok</span>
        {detail.tool_failures > 0 && <span className="tag tag-neutral" style={{ color: series.red }}>{t("{n} failed tool call(s)", { n: detail.tool_failures })}</span>}
        {detail.first_failure && <span className="ab-mono" style={{ color: series.red, fontSize: "var(--text-xs)" }}>{t("first failure at")} {detail.first_failure}</span>}
        {detail.trace_url && (
          <a className="btn btn-ghost btn-sm" href={detail.trace_url} target="_blank" rel="noreferrer" style={{ display: "inline-flex", alignItems: "center", gap: 4, marginLeft: "auto" }} title={t("Same trace_id, in the trace backend — spans, timings, raw inputs. Runs from before tracing was switched on have nothing there.")}>
            <ExternalLink size={12} />{t("open in Langfuse")}
          </a>
        )}
      </div>
      {detail.error && <div style={{ fontSize: "var(--text-xs)", color: series.red }}>{detail.error}</div>}

      <div>
        <SectionLabel>{t("the person asked")}</SectionLabel>
        <div style={{ padding: "var(--space-2) var(--space-3)", border: "1px solid var(--color-divider)", whiteSpace: "pre-wrap", fontSize: "var(--text-sm)" }}>{detail.input_message || <span style={{ color: faint }}>{t("(not recorded — run predates the ledger)")}</span>}</div>
      </div>

      <div>
        <SectionLabel>{t("what happened — the trace")}</SectionLabel>
        {detail.transcript.length ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {detail.transcript.map((line, i) => <TranscriptLine key={i} line={line} series={series} />)}
          </div>
        ) : <Empty>{t("No trace for this run — it was recorded before ADR-0053.")}</Empty>}
      </div>

      <div>
        <SectionLabel>{t("the agent answered")}</SectionLabel>
        <div style={{ padding: "var(--space-2) var(--space-3)", border: `1px solid ${series.blue}`, whiteSpace: "pre-wrap", fontSize: "var(--text-sm)" }}>{detail.answer || <span style={{ color: faint }}>{t("(no spoken answer)")}</span>}</div>
      </div>

      {(codeScores.length > 0 || thumbs.length > 0) && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", fontSize: "var(--text-xs)" }}>
          {codeScores.map((s) => <span key={s.name + s.created_at} className="tag tag-neutral" title={s.comment || ""} style={{ color: s.value >= 1 ? undefined : series.red }}>{t("code")} · {s.name} {s.value >= 1 ? "✓" : "✗"}</span>)}
          {thumbs.map((s) => <span key={s.created_at} className="tag tag-neutral" title={s.comment || ""}>{s.value >= 1 ? <ThumbsUp size={11} /> : <ThumbsDown size={11} />} {s.given_by_email || t("user")}{s.comment ? ` — ${s.comment}` : ""}</span>)}
        </div>
      )}

      <div style={{ borderTop: "1px solid var(--color-divider)", paddingTop: "var(--space-3)", display: "flex", flexDirection: "column", gap: 10 }}>
        <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-lg)" }}>{t("Your verdict")} <span className="ab-mono" style={{ fontSize: "var(--text-xs)", color: faint }}>{t("G = pass · B = fail")}</span></p>
        {latestLabel && <p style={{ margin: 0, fontSize: "var(--text-xs)", color: muted }}>{t("Last label: {v} by {who}{codes}", { v: latestLabel.passed ? t("pass") : t("fail"), who: latestLabel.reviewer_email || "?", codes: latestLabel.codes.length ? ` · ${latestLabel.codes.join(", ")}` : "" })}{latestLabel.note ? ` — ${latestLabel.note}` : ""}</p>}
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" className={`btn btn-sm ${passed === true ? "btn-primary" : "btn-secondary"}`} onClick={() => setPassed(true)} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><CheckCircle2 size={13} />{t("Pass")}</button>
          <button type="button" className={`btn btn-sm ${passed === false ? "btn-primary" : "btn-secondary"}`} onClick={() => setPassed(false)} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><XCircle size={13} />{t("Fail")}</button>
        </div>
        {passed === false && (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {Object.entries(codes).map(([code, meaning]) => {
              const on = picked.includes(code);
              return (
                <button key={code} type="button" className={`btn btn-sm ${on ? "btn-primary" : "btn-ghost"}`} title={meaning} onClick={() => setPicked(on ? picked.filter((c) => c !== code) : [...picked, code])}>{code}</button>
              );
            })}
          </div>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input ref={noteRef} className="ab-input" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("Why? One sentence the next reviewer can check.")} style={{ flex: 1 }} />
          <button type="button" className="btn btn-sm btn-primary" disabled={busy || passed == null} onClick={submit}>{busy ? <Spinner size={12} /> : reviewMode ? t("Save & next") : t("Save label")}</button>
        </div>
      </div>

      <div style={{ borderTop: "1px solid var(--color-divider)", paddingTop: "var(--space-3)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <p className="ab-mono" style={{ margin: 0, fontSize: "var(--text-xs)", letterSpacing: ".1em", textTransform: "uppercase", color: faint }}>{t("the judge's opinion")} <span style={{ textTransform: "none", letterSpacing: 0 }}>({detail.judge_verdicts.length})</span></p>
          {judgeHidden && detail.judge_verdicts.length > 0 && <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowJudge(true)}>{t("show anyway")}</button>}
        </div>
        {detail.judge_verdicts.length === 0 ? (
          <p style={{ margin: "6px 0 0", fontSize: "var(--text-xs)", color: faint }}>{t("Not judged. The sampled judge is run from the Quality tab.")}</p>
        ) : judgeHidden ? (
          <p style={{ margin: "6px 0 0", fontSize: "var(--text-xs)", color: muted }}>{t("Hidden until you decide — a score shown first pulls the reviewer toward it (slide 61).")}</p>
        ) : (
          <ul style={{ margin: "6px 0 0", padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 6, fontSize: "var(--text-xs)" }}>
            {detail.judge_verdicts.map((v) => (
              <li key={v.name + v.created_at} style={{ display: "flex", gap: 8 }}>
                <span className="ab-mono" style={{ color: v.value >= 1 ? series.blue : series.red, flex: "none" }}>{v.name} {v.value >= 1 ? "✓" : "✗"}</span>
                <span style={{ color: muted }}>{v.comment}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}

/** The review screen as a drawer over a table — one run by id, or the queue. */
export function RunDrawer({ runId, reviewMode = false, includeSimulated = false, codes, onClose, notify, onChanged, series }) {
  const { t } = useLang();
  const { detail, error, loaded, reload } = useRunDetail({ runId, reviewMode, includeSimulated });
  useEffect(() => { if (loaded && detail === null) onClose(); }, [loaded, detail, onClose]);

  if (error) return <Drawer title={t("Run")} onClose={onClose}><ErrorNote>{error}</ErrorNote></Drawer>;
  if (!detail) return <Drawer title={t("Run")} onClose={onClose}><div className="ab-empty"><Spinner size={18} /></div></Drawer>;
  return (
    <Drawer title={reviewMode ? t("Review — {n} left", { n: detail.queue_remaining ?? "?" }) : t("Run #{id}", { id: detail.id })} onClose={onClose} width={820}>
      <RunReview detail={detail} reviewMode={reviewMode} codes={codes} notify={notify} onChanged={onChanged} onSaved={reload} series={series} />
    </Drawer>
  );
}
