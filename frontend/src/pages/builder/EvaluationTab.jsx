import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ListChecks, CheckCircle2, XCircle, Wand2, ShieldAlert, Trash2, Upload, Download,
  AlertTriangle, X, ChevronDown, Filter, Ruler, Gavel, Unplug, BookOpenCheck,
} from "lucide-react";
import * as api from "../../lib/api";
import { ErrorNote, Note, PageShell, Section, Spinner } from "../../components/ui";
import { AgentMascot } from "../../components/mascot";
import { useAgentActivity } from "../../components/agent-presence";
import ConfirmDialog from "../../components/ConfirmDialog";
import { useLang } from "../../i18n";

/* ============================================================
   The agent's test suite — ADR-0054 §5 (EVAL-5 / #155), **one set, one way of
   measuring it** since ADR-0065 retired the scenario grid.

   ADR-0056 cut this screen into two facets because the suite was measured two
   ways and only one of them opened a gate. That premise is gone: a case is
   asked once and answered once, rule checks plus an LLM judge return one
   pass/fail, and the score is counted against the platform's single
   `eval_pass_threshold` — the number the approval gate and the admin dossier
   read (ADR-0011 §5 §7). There is no second number, so there is no switch, no
   second panel, and no second toolbar.

   `origin` is therefore back to what it always was: how far the creator
   vouches for a case (ADR-0011 §1) — the machine drafts, the person writes,
   the spreadsheet arrives, production hands one back — told apart by a filter
   chip. `grid` survives only as a chip on rows a retired feature left behind;
   they are listed, run and deleted like any other row.

   Nothing here derives a green light from the run: only the server's own
   `blocked_reason` may say whether this version can be submitted, because a
   superseded run that happened to pass would otherwise put a tick above a
   button that refuses (ADR-0011 §5). The score meter keeps ADR-0056 §8's rule
   — the figures ride the meter's `title` and `aria-label`, not a line of prose
   under the headline.

   The suite belongs to the AGENT and survives every refine (ADR-0054 §1);
   what changes per version is which cases still apply — an `orphan` chip for a
   criterion the spec no longer states, `ungrounded` for a rule naming an
   action this graph lost, which the run skips and says so.
   ============================================================ */

const ORIGIN_LABELS = {
  generated: "machine-written",
  manual: "hand-written",
  uploaded: "uploaded",
  promoted: "from a run",
  /* Nothing mints one any more (ADR-0065), but rows that carry it still exist
     and a chip that refused to name them would leave a row with no provenance
     at all. It is a filter like the other four. */
  grid: "from the old grid",
};

/* Every provenance a row can carry, in the order a creator meets them. */
const CASE_ORIGINS = ["generated", "manual", "uploaded", "promoted", "grid"];

const muted = "color-mix(in srgb, var(--color-text) 58%, transparent)";
const faint = "color-mix(in srgb, var(--color-text) 45%, transparent)";
const warn = "#b45309";

export default function EvaluationTab({ agentId, versionId, readsDatabase = false, notify }) {
  const { t } = useLang();
  const [cases, setCases] = useState([]);
  const [result, setResult] = useState(null);
  const [doomed, setDoomed] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [filters, setFilters] = useState({ origin: "", status: "" });
  const [selected, setSelected] = useState(() => new Set());
  const [uploading, setUploading] = useState(false);
  const [runMenu, setRunMenu] = useState(false);
  const [sample, setSample] = useState(null);

  const load = useCallback(async () => {
    setError("");
    try {
      setCases(await api.evaluation.cases(agentId, versionId));
    } catch (err) {
      setError(err.message);
    }
    try {
      setResult(await api.evaluation.latest(agentId, versionId));
    } catch {
      setResult(null); // No run yet is an answer, not a failure.
    }
  }, [agentId, versionId]);

  useEffect(() => { load(); }, [load]);

  /* The template's own sample suite (DS-22) — offered only when it ships one. */
  useEffect(() => {
    let live = true;
    api.evaluation.sample(agentId).then((found) => live && setSample(found)).catch(() => live && setSample(null));
    return () => { live = false; };
  }, [agentId, versionId]);

  async function loadSample() {
    setBusy("sample");
    setError("");
    try {
      const report = await api.evaluation.loadSample(agentId);
      await load();
      notify(t("{n} case(s) imported, {s} skipped, {e} error(s)", { n: report.added_count, s: report.skipped_count, e: report.error_count }));
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy("");
    }
  }

  /** Who a case asks as (DS-05) — blank clears it, "nobody asking". */
  async function saveRunAs(testCase, runAs) {
    try {
      const saved = await api.evaluation.updateCase(agentId, versionId, testCase.id, { run_as: runAs });
      setCases((current) => current.map((entry) => (entry.id === saved.id ? saved : entry)));
    } catch (err) {
      notify(err.message, "error");
    }
  }

  async function generate() {
    setBusy("generate");
    setError("");
    try {
      const generated = await api.evaluation.generate(agentId, versionId);
      await load();
      notify(
        generated.skipped_criteria.length
          ? t("{n} case(s) written · {skipped} criterion could not be turned into one", { n: generated.cases.length, skipped: generated.skipped_criteria.length })
          : t("{n} test case(s) written from the specification", { n: generated.cases.length })
      );
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy("");
    }
  }

  async function run(caseIds = null) {
    setBusy("run");
    setError("");
    setRunMenu(false);
    try {
      const evaluated = await api.evaluation.run(agentId, versionId, caseIds);
      setResult(evaluated);
      notify(t("{passed}/{total} checks passed", { passed: evaluated.passed_count, total: evaluated.total_count }));
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
    } finally {
      setBusy("");
    }
  }

  async function exportSuite(format) {
    try {
      const text = await api.evaluation.exportSuite(agentId, format);
      const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `bo-de-agent-${agentId}.${format}`;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (err) {
      notify(err.message, "error");
    }
  }

  async function confirmDelete() {
    setDeleting(true);
    try {
      await api.evaluation.removeCase(agentId, versionId, doomed.id);
      setCases((current) => current.filter((entry) => entry.id !== doomed.id));
      notify(t("Test case deleted — run the evaluation again to rescore"));
      setDoomed(null);
    } catch (err) {
      setError(err.message);
      notify(err.message, "error");
      setDoomed(null);
    } finally {
      setDeleting(false);
    }
  }

  const verdicts = useMemo(
    () => new Map((result?.results || []).map((entry) => [entry.suite_case_id ?? entry.case_id, entry])),
    [result]
  );
  const skippedReasons = useMemo(
    () => new Map((result?.skipped || []).map((entry) => [entry.suite_case_id, entry.reason])),
    [result]
  );
  useAgentActivity("thinking", busy === "generate", t("writing the eval cases"));
  useAgentActivity("thinking", busy === "run", t("judging the run"));

  /* Counts ride on the filter chips themselves rather than on a separate line
     of header facts: Vanta's compliance "Tests" screen puts the population size
     inside the control that narrows to it, which removes a whole row and stops
     a reader adding up two different populations by eye. */
  const originCounts = useMemo(() => {
    const tally = {};
    for (const entry of cases) tally[entry.origin] = (tally[entry.origin] || 0) + 1;
    return tally;
  }, [cases]);
  const statusCounts = useMemo(() => {
    const tally = { failed: 0, orphan: 0, ungrounded: 0 };
    for (const entry of cases) {
      const verdict = verdicts.get(entry.id);
      if (verdict && !verdict.passed) tally.failed += 1;
      if (verdict?.applicability === "orphan") tally.orphan += 1;
      if (skippedReasons.has(entry.id)) tally.ungrounded += 1;
    }
    return tally;
  }, [cases, verdicts, skippedReasons]);

  const shown = cases.filter((entry) => {
    if (filters.origin && entry.origin !== filters.origin) return false;
    if (filters.status) {
      const verdict = verdicts.get(entry.id);
      if (filters.status === "orphan" && verdict?.applicability !== "orphan") return false;
      if (filters.status === "ungrounded" && !skippedReasons.has(entry.id)) return false;
      if (filters.status === "failed" && !(verdict && !verdict.passed)) return false;
    }
    return true;
  });

  const failedIds = (result?.results || []).filter((r) => !r.passed).map((r) => r.suite_case_id ?? r.case_id);
  const toggleSelected = (id) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });

  /* — What the meter is worth in figures. Deliberately not printed: these are
       the meter's own title, so the numbers are one hover — and one
       screen-reader stop — away instead of two lines of prose under the
       headline. `pass rate %` is absent entirely: it is `passed/total` said a
       second way, and two names for one measure are two numbers that drift. — */
  const scoreFacts = result
    ? [
        `${result.passed_count}/${result.total_count} ${t("passed")}`,
        t("bar {n}%", { n: Math.round(result.threshold * 100) }),
        ...(result.meets_threshold ? [] : [t("{n} case(s) short", { n: result.cases_short })]),
        ...(result.skipped?.length ? [t("{n} skipped (ungrounded)", { n: result.skipped.length })] : []),
      ].join(" · ")
    : t("no run yet — nothing has been scored");

  return (
    <PageShell>
      <Section
        title={t("The agent's test suite")}
        hint={t("One suite per agent — it survives every new version. Each case is asked once and answered once, and the score against the bar is what the approval gate reads.")}
        actions={
          /* Export writes the whole suite, filters and all — one file. */
          <button className="btn btn-secondary btn-icon" onClick={() => exportSuite("jsonl")} title={t("Export the whole suite (JSONL; re-importable)")}>
            <Download size={14} />
          </button>
        }
      >

        {/* — The score, as one figure and one meter (ADR-0056 §8 kept) ————
            ADR-0056 put two of these side by side because the suite had two
            regimes. It has one, so this is a headline and a bar, not a tab:
            the fill is the pass rate, the notch is `eval_pass_threshold`, and
            a fill short of the notch IS the refusal, read in one glance. The
            figures behind it live in the meter's title and aria-label. */}
        <div
          style={{
            display: "flex", alignItems: "flex-end", gap: 14, flexWrap: "wrap",
            border: "1px solid var(--color-divider)", padding: "var(--space-3) var(--space-4)", marginBottom: 14,
          }}
        >
          <div style={{ minWidth: 190, flex: "1 1 190px" }}>
            <span
              className="ab-mono"
              style={{
                display: "flex", alignItems: "center", gap: 7, fontSize: "var(--text-2xs)", letterSpacing: ".13em",
                textTransform: "uppercase", color: faint,
              }}
            >
              <ListChecks size={13} />
              <span>{t("Score")}</span>
              <span style={{ marginLeft: "auto", flex: "none" }}>{cases.length}</span>
            </span>
            <span
              title={scoreFacts}
              aria-label={scoreFacts}
              style={{
                display: "block", fontFamily: "var(--font-heading)", fontSize: "var(--text-2xl)", lineHeight: 1.05,
                color: result ? (result.meets_threshold ? "var(--color-accent)" : warn) : "var(--color-text)",
              }}
            >
              {result ? `${result.passed_count}/${result.total_count}` : "—"}
            </span>
            {result && (
              <Meter
                title={scoreFacts}
                segments={[{
                  key: "passed",
                  frac: result.total_count ? result.passed_count / result.total_count : 0,
                  color: result.meets_threshold ? "var(--color-accent)" : warn,
                }]}
                notch={result.threshold}
              />
            )}
          </div>
        </div>

        <ErrorNote>{error}</ErrorNote>

        {/* ── The gate's refusal ──
            Version-level: this sentence answers
            "may this version be submitted", which the gate answers from the
            latest run only — never from the run on screen (ADR-0011 §5).

            It was already clamped to one line and it was still read as "this
            always shows in full", which says the clamp was never what was
            wrong: a filled amber band the width of the page reads as an alarm
            whatever it is holding, and nothing on it said it could be opened.
            So it is a Note now — a hairline, a mark, one line, and a caret
            that says there is more. Not one word is dropped: ADR-0011 §5 still
            buys the score, the bar, the distance in whole cases and the next
            move, all of them one click or one hover away, and the meter above
            already draws the same refusal without words.
            The fold resets when the sentence changes, so a refusal left open
            last time does not un-fold the next one. */}
        <Note tone="warn" fold text={result?.blocked_reason} />

        <div>
            {/* Every verb in this toolbar adds to, or scores, the one set. */}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
              <button className="btn btn-secondary" onClick={generate} disabled={!!busy} style={{ height: 38 }}>
                {busy === "generate" ? <Spinner size={13} /> : <Wand2 size={13} />}{t("Generate cases")}
              </button>
              <button className="btn btn-secondary" onClick={() => setUploading(true)} disabled={!!busy} style={{ height: 38 }}>
                <Upload size={13} />{t("Upload cases")}
              </button>
              {sample?.available && (
                <button
                  className="btn btn-secondary"
                  onClick={loadSample}
                  disabled={!!busy}
                  style={{ height: 38 }}
                  title={t("{name} — {n} cases, each asked by a demo employee", { name: sample.name, n: sample.case_count })}
                >
                  {busy === "sample" ? <Spinner size={13} /> : <BookOpenCheck size={13} />}{t("Load sample suite")}
                </button>
              )}
              <div style={{ position: "relative", marginLeft: "auto" }}>
                <button className="btn btn-primary" onClick={() => setRunMenu((v) => !v)} disabled={!!busy || cases.length === 0} style={{ height: 38 }}>
                  {busy === "run" ? <Spinner size={13} /> : <ListChecks size={13} />}{t("Run evaluation")}<ChevronDown size={12} />
                </button>
                {runMenu && (
                  <div style={{ position: "absolute", right: 0, top: 42, zIndex: 20, background: "var(--color-bg)", border: "1px solid var(--color-divider)", display: "flex", flexDirection: "column", minWidth: 190 }}>
                    <button className="btn btn-ghost" style={{ justifyContent: "flex-start" }} onClick={() => run(null)}>{t("All cases")}</button>
                    <button className="btn btn-ghost" style={{ justifyContent: "flex-start" }} disabled={!failedIds.length} onClick={() => run(failedIds)}>{t("Only the failed ones")}</button>
                    <button className="btn btn-ghost" style={{ justifyContent: "flex-start" }} disabled={!selected.size} onClick={() => run([...selected])}>{t("Selected ({n})", { n: selected.size })}</button>
                  </div>
                )}
              </div>
            </div>

            {cases.length > 0 && (
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", margin: "0 0 12px", alignItems: "center" }}>
                <Filter size={12} aria-hidden="true" style={{ color: faint, flex: "none" }} />
                {CASE_ORIGINS.filter((origin) => originCounts[origin]).map((origin) => (
                  <button
                    key={origin}
                    type="button"
                    className={`btn btn-sm ${filters.origin === origin ? "btn-primary" : "btn-ghost"}`}
                    onClick={() => setFilters((f) => ({ ...f, origin: f.origin === origin ? "" : origin }))}
                  >
                    {t(ORIGIN_LABELS[origin])} {originCounts[origin]}
                  </button>
                ))}
                <span style={{ width: 10 }} />
                {[["failed", t("failing")], ["orphan", t("criterion gone")], ["ungrounded", t("ungrounded")]]
                  /* A bucket with nothing in it is a chip that filters to an
                     empty list — a control that can only disappoint. */
                  .filter(([key]) => statusCounts[key])
                  .map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    className={`btn btn-sm ${filters.status === key ? "btn-primary" : "btn-ghost"}`}
                    onClick={() => setFilters((f) => ({ ...f, status: f.status === key ? "" : key }))}
                  >
                    {label} {statusCounts[key]}
                  </button>
                ))}
              </div>
            )}

            {cases.length === 0 ? (
              <div style={{ border: "1px dashed var(--color-divider)", padding: "var(--space-12) var(--space-6)", textAlign: "center", fontSize: "var(--text-md)", color: muted }}>
                {t("No test cases yet —")} <b style={{ color: "var(--color-text)" }}>{t("Generate cases")}</b> {t("writes them from the criteria in the specification, or upload your own from a spreadsheet.")}
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {shown.map((testCase) => (
                  <CaseRow
                    key={testCase.id}
                    testCase={testCase}
                    verdict={verdicts.get(testCase.id)}
                    skippedReason={skippedReasons.get(testCase.id)}
                    checked={selected.has(testCase.id)}
                    onToggle={() => toggleSelected(testCase.id)}
                    onDelete={() => setDoomed(testCase)}
                    readsDatabase={readsDatabase}
                    onRunAs={(runAs) => saveRunAs(testCase, runAs)}
                    busy={!!busy}
                  />
                ))}
                {shown.length === 0 && (
                  <p style={{ margin: 0, padding: "var(--space-6) var(--space-1)", textAlign: "center", fontSize: "var(--text-sm)", color: muted }}>
                    {t("No case matches this filter.")}
                  </p>
                )}
              </div>
            )}
        </div>
      </Section>

      {uploading && (
        <UploadDialog
          agentId={agentId}
          onClose={() => setUploading(false)}
          onImported={async (report) => {
            setUploading(false);
            await load();
            notify(t("{n} case(s) imported, {s} skipped, {e} error(s)", { n: report.added_count, s: report.skipped_count, e: report.error_count }));
          }}
          notify={notify}
        />
      )}

      <ConfirmDialog
        open={Boolean(doomed)}
        busy={deleting}
        title={t("Delete this test case?")}
        confirmLabel={t("Delete test case")}
        onCancel={() => setDoomed(null)}
        onConfirm={confirmDelete}
        body={doomed && (
          <>
            <p style={{ margin: "0 0 8px" }}>{doomed.criterion}</p>
            <p style={{ margin: 0 }}>
              {t("Evaluation runs already scored keep their own copy of this case, so past scores do not change. Run the evaluation again to score without it.")}
            </p>
          </>
        )}
      />
    </PageShell>
  );
}

/** A figure drawn instead of written.
 *
 *  `segments` are fractions of one track; `notch` puts a tick where a
 *  threshold sits, so "the fill stops short of the tick" is the refusal read
 *  without reading. The exact numbers are the `title` and the `aria-label`:
 *  the eye is spared them, the pointer and the screen reader are not — which
 *  is the whole bargain `Hint` struck for this app in d81dfe4. */
function Meter({ segments, notch, title }) {
  return (
    <span
      role="img"
      title={title}
      aria-label={title}
      style={{
        position: "relative", display: "flex", height: 7, marginTop: 3,
        background: "color-mix(in srgb, var(--color-text) 8%, transparent)",
        border: "1px solid var(--color-divider)",
      }}
    >
      {segments.map((segment) => (
        <span
          key={segment.key}
          style={{
            width: `${Math.max(0, Math.min(1, segment.frac || 0)) * 100}%`,
            /* "no evidence" is hatched rather than coloured: the one state
               that is an absence must not read as a fifth colour, and it keeps
               the four states told apart without relying on hue alone. */
            background: segment.hatched
              ? "repeating-linear-gradient(135deg, color-mix(in srgb, var(--color-text) 22%, transparent) 0 2px, transparent 2px 5px)"
              : segment.color,
          }}
        />
      ))}
      {notch != null && (
        <span
          aria-hidden="true"
          style={{
            position: "absolute", left: `${Math.max(0, Math.min(1, notch)) * 100}%`,
            top: -3, bottom: -3, width: 2, transform: "translateX(-1px)",
            background: "color-mix(in srgb, var(--color-text) 75%, transparent)",
          }}
        />
      )}
    </span>
  );
}

/* ── One golden-set case ──────────────────────────────────────────────── */

function CaseRow({ testCase, verdict, skippedReason, checked, onToggle, onDelete, readsDatabase, onRunAs, busy }) {
  const { t } = useLang();
  const ruleBased = Boolean(testCase.expected_check);
  const orphan = verdict?.applicability === "orphan";
  return (
    <div
      className="ab-eval-item"
      style={{ borderColor: verdict && !verdict.passed ? "rgba(239,68,68,.4)" : "var(--color-divider)", animation: "agUp .3s ease both" }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "center" }}>
        <input type="checkbox" checked={checked} onChange={onToggle} aria-label={t("Select this case")} />
        {verdict ? (
          verdict.passed
            ? <CheckCircle2 size={17} style={{ color: "var(--color-accent)" }} />
            : <XCircle size={17} style={{ color: "#b91c1c" }} />
        ) : (
          <div style={{ width: 17, height: 17, borderRadius: 99, border: "1.5px solid var(--color-divider)" }} />
        )}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
          <p style={{ flex: 1, minWidth: 0, fontSize: "var(--text-sm)", margin: 0, lineHeight: 1.5 }}>{testCase.criterion}</p>
          <button
            className="btn btn-secondary btn-icon"
            style={{ width: 26, height: 26, padding: "var(--space-1)", flexShrink: 0 }}
            onClick={onDelete}
            disabled={busy}
            title={t("Delete this test case")}
            aria-label={t("Delete this test case")}
          >
            <Trash2 size={13} />
          </button>
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 6 }}>
          {/* Which grader read this case is one bit; a ruler and a gavel
              carry it, and the words stay on the mark. */}
          <span
            className="ab-chip"
            title={ruleBased ? t("rule-based") : t("LLM-graded")}
            aria-label={ruleBased ? t("rule-based") : t("LLM-graded")}
            style={{ padding: "var(--space-1) var(--space-2)" }}
          >
            {ruleBased ? <Ruler size={12} /> : <Gavel size={12} />}
          </span>
          {testCase.category && <span className="ab-chip">{t(testCase.category.replace(/_/g, " "))}</span>}
          <span className="ab-chip">{t(ORIGIN_LABELS[testCase.origin] || testCase.origin)}</span>
          {(testCase.tags || []).map((tag) => <span key={tag} className="ab-chip">#{tag}</span>)}
          {testCase.run_as && <span className="ab-chip" title={t("Who asks this case's question")}>{t("as {who}", { who: testCase.run_as })}</span>}
          {readsDatabase && <RunAsEditor value={testCase.run_as} disabled={busy} onSave={onRunAs} />}
          {orphan && (
            <span className="ab-chip" style={{ color: warn, borderColor: "rgba(245,158,11,.4)" }} title={t("Still run and judged against its own expected answer")}>
              <AlertTriangle size={11} />{t("criterion no longer in this version")}
            </span>
          )}
          {skippedReason && (
            <span className="ab-chip" style={{ color: warn, borderColor: "rgba(245,158,11,.4)" }} title={skippedReason}>
              <AlertTriangle size={11} />{t("not run — names something this graph does not have")}
            </span>
          )}
          {verdict && verdict.safety_violation && (
            <span className="ab-chip" style={{ color: "#b91c1c", borderColor: "rgba(239,68,68,.35)" }}>
              <ShieldAlert size={11} />{t("safety — cannot be waived")}
            </span>
          )}
        </div>
        <p style={{ fontSize: "var(--text-sm)", color: muted, margin: "8px 0 0", lineHeight: 1.5 }}>
          <b>{t("Input:")}</b> {testCase.input_message}
        </p>
        {/* A case that passed has already said everything the eye came for
            with its tick: what it did and why the judge was satisfied are
            detail, and they fold. A case that FAILED is the one d81dfe4 opened
            up in the first place — that stays open. */}
        {verdict && (verdict.passed ? (
          <details style={{ marginTop: 6 }}>
            <summary style={{ fontSize: "var(--text-xs)", cursor: "pointer", color: muted }}>{t("what it did and answered")}</summary>
            <Outcome verdict={verdict} />
          </details>
        ) : (
          <Outcome verdict={verdict} />
        ))}
      </div>
    </div>
  );
}

/** What the run did and what the judge made of it, in that order. */
function Outcome({ verdict }) {
  const { t } = useLang();
  return (
    <>
      <AgentRun verdict={verdict} />
      <p style={{ fontSize: "var(--text-sm)", color: verdict.passed ? muted : "#b91c1c", margin: "4px 0 0", lineHeight: 1.5 }}>
        <b>{t("Verdict:")}</b> {verdict.reason}
      </p>
    </>
  );
}

/* ── The upload dialog: file → dry-run report → import ────────────────── */

function UploadDialog({ agentId, onClose, onImported, notify }) {
  const { t } = useLang();
  const [file, setFile] = useState(null);
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef(null);

  async function preview(selectedFile) {
    setFile(selectedFile);
    setReport(null);
    setBusy(true);
    try {
      setReport(await api.evaluation.importSuite(agentId, selectedFile, true));
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function commit() {
    setBusy(true);
    try {
      onImported(await api.evaluation.importSuite(agentId, file, false));
    } catch (err) {
      notify(err.message, "error");
      setBusy(false);
    }
  }

  async function template() {
    try {
      const text = await api.evaluation.template(agentId);
      const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "bo-de-mau.csv";
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (err) {
      notify(err.message, "error");
    }
  }

  return (
    <div role="dialog" aria-modal="true" style={{ position: "fixed", inset: 0, zIndex: 60, display: "grid", placeItems: "center", background: "color-mix(in srgb, #000 45%, transparent)" }} onClick={onClose}>
      <div style={{ width: "min(680px, 92vw)", maxHeight: "84vh", overflowY: "auto", background: "var(--color-bg)", border: "1px solid var(--color-divider)", padding: "var(--space-5)", display: "flex", flexDirection: "column", gap: 14 }} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>{t("Upload your own test set")}</p>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="close"><X size={16} /></button>
        </div>
        <p style={{ margin: 0, fontSize: "var(--text-sm)", color: muted, lineHeight: 1.6 }}>
          {t("CSV, JSONL or XLSX — one row per case, columns `input` and `expected` required. Nothing is written until you confirm: the report below is a preview.")}{" "}
          <button type="button" className="btn btn-ghost btn-sm" onClick={template}>{t("download the template")}</button>
        </p>
        <input
          ref={inputRef}
          type="file"
          accept=".csv,.jsonl,.xlsx"
          aria-label={t("Choose a file")}
          onChange={(e) => e.target.files?.[0] && preview(e.target.files[0])}
        />
        {busy && <div style={{ padding: "var(--space-3)" }}><AgentMascot state="exploring" size={82} /></div>}
        {report && (
          <>
            <div style={{ display: "flex", gap: 14, fontSize: "var(--text-sm)", flexWrap: "wrap" }}>
              <span className="tag tag-accent">{t("{n} will be added", { n: report.added_count })}</span>
              <span className="tag tag-neutral">{t("{n} skipped (already there)", { n: report.skipped_count })}</span>
              <span className="tag tag-neutral" style={{ color: report.error_count ? "#b91c1c" : undefined }}>{t("{n} error(s)", { n: report.error_count })}</span>
            </div>
            <div style={{ overflowX: "auto", maxHeight: 300, overflowY: "auto", border: "1px solid var(--color-divider)" }}>
              <table className="ab-table">
                <thead><tr><th>{t("row")}</th><th>{t("status")}</th><th>{t("why")}</th></tr></thead>
                <tbody>
                  {report.rows.map((row) => (
                    <tr key={row.row}>
                      <td className="ab-mono">{row.row}</td>
                      <td><span className={`tag ${row.status === "added" ? "tag-accent" : "tag-neutral"}`} style={{ color: row.status === "error" ? "#b91c1c" : undefined }}>{row.status}</span></td>
                      <td style={{ fontSize: "var(--text-xs)" }}>{row.reason || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button type="button" className="btn btn-secondary" onClick={onClose}>{t("Cancel")}</button>
              <button type="button" className="btn btn-primary" disabled={busy || !report.added_count} onClick={commit}>
                {t("Import {n} valid row(s)", { n: report.added_count })}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** What the agent actually did and said on this case.
 *
 *  A verdict alone ("không đạt: thiếu ngày họp") tells a creator that something
 *  went wrong, not *what the agent did*. So the run itself comes out beside it:
 *  the actions it took, in order, with a tick or a cross each — then its answer,
 *  clamped to a few lines because most answers are long and the verdict is what
 *  the eye came for. Both expand; the raw record stays one fold further down,
 *  unchanged (ADR-0052: a person can always check the exact evidence the judge
 *  was shown). */
function AgentRun({ verdict }) {
  const { t } = useLang();
  const [openAnswer, setOpenAnswer] = useState(false);
  const calls = verdict.evidence || [];
  const answer = (verdict.answer || "").trim();
  if (!calls.length && !answer) return null;
  return (
    <div style={{ margin: "8px 0 0", display: "flex", flexDirection: "column", gap: 5 }}>
      {calls.length > 0 && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", fontSize: "var(--text-xs)" }}>
          <b style={{ fontSize: "var(--text-sm)" }}>{t("Did:")}</b>
          {calls.map((call, index) => (
            <span
              key={index}
              className="ab-chip ab-mono"
              title={`${JSON.stringify(call.args || {})}
→ ${call.result || ""}`.slice(0, 400)}
              style={{ fontSize: "var(--text-2xs)", color: call.ok === false ? "#b91c1c" : undefined }}
            >
              {call.tool}{call.ok === false ? " ✗" : call.ok ? " ✓" : ""}
              {call.simulated ? " ~" : ""}
            </span>
          ))}
        </div>
      )}
      {answer && (
        <p
          onClick={() => setOpenAnswer((v) => !v)}
          title={openAnswer ? t("Click to collapse") : t("Click to read it all")}
          style={{
            fontSize: "var(--text-sm)", color: muted, margin: 0, lineHeight: 1.5, cursor: "pointer",
            display: "-webkit-box", WebkitBoxOrient: "vertical", WebkitLineClamp: openAnswer ? "unset" : 3,
            overflow: openAnswer ? "visible" : "hidden", whiteSpace: "pre-wrap",
          }}
        >
          <b style={{ color: "var(--color-text)" }}>{t("Answered:")}</b> {answer}
        </p>
      )}
      {calls.length > 0 && <Evidence calls={calls} />}
    </div>
  );
}

/** What the judge read beside the answer (ADR-0052): each tool call the case's
 *  run made, with its result. Folded by default — the verdict is the headline,
 *  this is how to check it — and rendered raw: the point is that a person can
 *  see exactly the record the verdict was based on. */
function Evidence({ calls }) {
  const { t } = useLang();
  return (
    <details style={{ marginTop: 6 }}>
      <summary style={{ fontSize: "var(--text-xs)", cursor: "pointer", color: "color-mix(in srgb, var(--color-text) 58%, transparent)" }}>
        {t("Evidence: {n} tool call(s)", { n: calls.length })}
      </summary>
      <ul style={{ margin: "6px 0 0", paddingLeft: "var(--space-4)", display: "flex", flexDirection: "column", gap: 6 }}>
        {calls.map((call, index) => (
          <li key={index} style={{ fontSize: "var(--text-xs)", lineHeight: 1.5 }}>
            <code className="ab-mono">{call.tool}</code>
            {" "}
            <span className="ab-mono" style={{ opacity: 0.75 }}>{JSON.stringify(call.args || {})}</span>
            {call.simulated && <span className="ab-chip" style={{ marginLeft: 6, fontSize: "var(--text-2xs)" }}>{t("simulated")}</span>}
            {call.ok === false && <span className="ab-chip" style={{ marginLeft: 6, fontSize: "var(--text-2xs)", color: "#b91c1c" }}>{t("failed")}</span>}
            <pre className="ab-mono" style={{ margin: "4px 0 0", whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: "var(--text-xs)", opacity: 0.85 }}>{call.result}</pre>
          </li>
        ))}
      </ul>
    </details>
  );
}


/** "Run as…" on one case: a button that opens one input. Enter or leaving the
 *  box saves what was typed; Escape leaves the case as it was. Blank saves
 *  "nobody", which every database read denies. */
function RunAsEditor({ value, disabled, onSave }) {
  const { t } = useLang();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(value || "");
  if (!open) {
    return (
      <button type="button" className="ab-chip" disabled={disabled} onClick={() => { setDraft(value || ""); setOpen(true); }}>
        {t("Run as…")}
      </button>
    );
  }
  return (
    <input
      autoFocus
      className="ab-input"
      style={{ fontSize: "var(--text-xs)", width: 200 }}
      aria-label={t("Run as")}
      placeholder={t("An employee's email — whose data the run may read")}
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter") event.currentTarget.blur();
        if (event.key === "Escape") { setDraft(value || ""); setOpen(false); }
      }}
      onBlur={() => {
        setOpen(false);
        if (draft.trim() !== (value || "")) onSave(draft.trim());
      }}
    />
  );
}
