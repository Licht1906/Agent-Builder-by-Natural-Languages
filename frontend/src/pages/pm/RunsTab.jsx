import { useMemo, useState } from "react";
import { ThumbsUp, ThumbsDown, BookOpen, ArrowRight } from "lucide-react";
import * as api from "../../lib/api";
import { Hint, Spinner } from "../../components/ui";
import { useLang } from "../../i18n";
import { ColumnWidths, Empty, HBars, Panel, PanelGrid, SectionLabel, Th, faint, muted, usd, useColumnSizing } from "./shared";

/* ============================================================
   Tab 2 — Runs & traces: the runs behind every number.

   Where failures start (slide 47), cut three ways, above the table of runs
   the current filter names. "read" opens the trace — the question, every tool
   call, the answer — with the label form; "→ case" turns a failure into a
   test the version must pass from now on. The filter is owned by the shell
   so the Overview's bars and alerts can set it before this tab is open.
   ============================================================ */
export default function RunsTab({ overview, runs, runsLoading, runFilter, pickRuns, clearFilter, series, notify, onChanged, onOpen }) {
  const { t } = useLang();
  return (
    <>
      <PanelGrid>
        <Panel n={7} title={t("Where failures start")} lede={t("Failures cut three ways: the node type the first failed tool ran in, the action called, and the expert's trace code. Eighty percent in one cut is a failure mode, not scattered bad luck.")} wide>
          <FailureSlices data={overview.failure_slices} codes={overview.trace_codes} series={series} onPick={(failureNodeType) => pickRuns(failureNodeType ? { failureNodeType } : {})} active={runFilter.failureNodeType} />
        </Panel>
      </PanelGrid>
      <RunsTable runs={runs} loading={runsLoading} filter={runFilter} clearFilter={clearFilter} notify={notify} onChanged={onChanged} onOpen={onOpen} series={series} />
    </>
  );
}

function FailureSlices({ data, codes, series, onPick, active }) {
  const { t } = useLang();
  if (!data.failed_runs && !data.by_trace_code.length) return <Empty>{t("Nothing went wrong in this window — or nothing with a trace did.")}</Empty>;
  const block = (label, rows, pick, activeKey, describe) => (
    <div>
      <SectionLabel>{label}</SectionLabel>
      {rows.length ? (
        <HBars rows={rows.map((s) => ({ key: s.key, label: s.key, value: s.runs, tooltip: describe ? describe(s.key) : s.key }))} series={series} onPick={pick} active={activeKey} format={(v) => v} labelWidth={150} />
      ) : <p style={{ margin: 0, fontSize: "var(--text-xs)", color: faint }}>{t("none yet")}</p>}
    </div>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <p style={{ margin: 0, fontSize: "var(--text-sm)" }}><strong>{data.failed_runs}</strong> {t("runs went wrong (not success, or success with a failed tool)")}</p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 18 }}>
        {block(t("by node type where it started"), data.by_node_type, onPick, active)}
        {block(t("by action called"), data.by_action)}
        {block(t("by trace code (expert)"), data.by_trace_code, null, null, (k) => codes[k] || k)}
      </div>
    </div>
  );
}

/** When · Agent · asked · trigger · outcome · cost · latency · graders · verdict.
 *  "asked" and "outcome" carry the free text, so those are the ones a reader
 *  usually widens; all nine are draggable. */
const RUN_COLUMNS = [150, 200, 260, 96, 190, 84, 84, 150, 230];

function RunsTable({ runs, loading, filter, clearFilter, notify, onChanged, onOpen, series }) {
  const { t } = useLang();
  const [promoting, setPromoting] = useState(null);
  const [busy, setBusy] = useState(null);
  const sizing = useColumnSizing("pm.runs.columns", RUN_COLUMNS);

  async function thumbs(run, up) {
    setBusy(run.id);
    try {
      await api.pm.feedback(run.id, up, "");
      notify(up ? t("Thumbs up recorded") : t("Thumbs down recorded"));
      onChanged();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(null);
    }
  }

  const filterLabel = useMemo(() => Object.values(filter).filter(Boolean).join(" · "), [filter]);

  return (
    <section className="blueprint" style={{ padding: "var(--space-4)", marginTop: 18, display: "flex", flexDirection: "column", gap: 12 }}>
      <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", gap: 12, flexWrap: "wrap" }}>
        <div>
          <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>{t("Runs behind the numbers")}<Hint text={t("\"read\" opens the trace — the question, every tool call, the answer — and the label form. \"→ case\" turns a failure into a test the version must pass from now on, with the input already filled in.")} /></p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {filterLabel && <span className="tag tag-accent">{filterLabel}</span>}
          {filterLabel && <button type="button" className="btn btn-ghost btn-sm" onClick={clearFilter}>{t("clear filter")}</button>}
          {loading && <Spinner size={14} />}
        </div>
      </div>
      {runs.length === 0 ? (
        <Empty>{t("No runs match.")}</Empty>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="ab-table ab-table-sized" style={{ minWidth: sizing.widths.reduce((a, b) => a + b, 0) }}>
            <ColumnWidths sizing={sizing} />
            <thead>
              <tr>
                <Th sizing={sizing} index={0}>{t("When")}</Th>
                <Th sizing={sizing} index={1}>{t("Agent")}</Th>
                <Th sizing={sizing} index={2}>{t("asked")}</Th>
                <Th sizing={sizing} index={3}>{t("trigger")}</Th>
                <Th sizing={sizing} index={4}>{t("outcome")}</Th>
                <Th sizing={sizing} index={5} align="right">{t("cost")}</Th>
                <Th sizing={sizing} index={6} align="right">{t("latency")}</Th>
                <Th sizing={sizing} index={7}>{t("graders")}</Th>
                <Th sizing={sizing} index={8}>{t("verdict")}</Th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <RunRow
                  key={run.id}
                  run={run}
                  busy={busy === run.id}
                  thumbs={(up) => thumbs(run, up)}
                  promoting={promoting === run.id}
                  openPromote={() => setPromoting(promoting === run.id ? null : run.id)}
                  onPromoted={() => { setPromoting(null); onChanged(); }}
                  onOpen={() => onOpen(run.id)}
                  notify={notify}
                  series={series}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function GraderMarks({ run, series }) {
  const { t } = useLang();
  const mark = (value, label) => value == null ? null : (
    <span key={label} className="ab-mono" title={label} style={{ fontSize: "var(--text-2xs)", color: value ? series.blue : series.red, marginRight: 6 }}>{label} {value ? "✓" : "✗"}</span>
  );
  return (
    <span style={{ whiteSpace: "nowrap" }}>
      {mark(run.tool_grounded, t("code"))}
      {mark(run.judge_pass, t("judge"))}
      {run.expert_passed != null && mark(run.expert_passed ? 1 : 0, t("expert"))}
      {run.tool_failures > 0 && <span className="ab-mono" style={{ fontSize: "var(--text-2xs)", color: series.red }} title={t("{n} failed tool call(s)", { n: run.tool_failures })}>{t("tool ✗")}</span>}
      {!run.has_trace && <span className="ab-mono" style={{ fontSize: "var(--text-2xs)", color: faint }}>{t("no trace")}</span>}
    </span>
  );
}

function RunRow({ run, busy, thumbs, promoting, openPromote, onPromoted, onOpen, notify, series }) {
  const { t } = useLang();
  const failed = run.outcome !== "success";
  return (
    <>
      <tr>
        <td className="ab-mono" style={{ whiteSpace: "nowrap", fontSize: "var(--text-xs)" }}>{new Date(run.created_at).toLocaleString()}{run.simulated && <span className="tag tag-neutral" style={{ marginLeft: 6, fontSize: "var(--text-2xs)" }}>{t("sim")}</span>}</td>
        <td style={{ fontFamily: "var(--font-heading)", fontSize: "var(--text-md)" }}>{run.agent_name}<div className="ab-mono" style={{ fontSize: "var(--text-xs)", color: faint }}>v{run.version_key} · #{run.id}{run.pipeline === "uc1" ? " · UC-1" : ""}</div></td>
        <td style={{ fontSize: "var(--text-xs)", color: muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={run.input_message || ""}>{run.input_message || "—"}</td>
        <td className="ab-mono">{run.trigger}</td>
        <td><span className={`tag ${failed ? "tag-neutral" : "tag-accent"}`}>{run.outcome}</span>{run.error && <div style={{ fontSize: "var(--text-xs)", color: muted }}>{run.error}</div>}</td>
        <td className="ab-mono" style={{ textAlign: "right" }}>{usd(run.cost_usd)}</td>
        <td className="ab-mono" style={{ textAlign: "right" }}>{run.latency_seconds.toFixed(1)}s</td>
        <td><GraderMarks run={run} series={series} /></td>
        <td style={{ whiteSpace: "nowrap" }}>
          <button type="button" className="btn btn-sm btn-secondary" onClick={onOpen} style={{ marginRight: 6, display: "inline-flex", alignItems: "center", gap: 4 }}><BookOpen size={12} />{t("read")}</button>
          <button type="button" className={`btn btn-sm btn-icon ${run.thumbs === 1 ? "btn-primary" : "btn-secondary"}`} disabled={busy} onClick={() => thumbs(true)} title={t("Thumbs up")} style={{ width: 30, height: 30 }}><ThumbsUp size={13} /></button>{" "}
          <button type="button" className={`btn btn-sm btn-icon ${run.thumbs === 0 ? "btn-primary" : "btn-secondary"}`} disabled={busy} onClick={() => thumbs(false)} title={t("Thumbs down")} style={{ width: 30, height: 30 }}><ThumbsDown size={13} /></button>{" "}
          <button type="button" className="btn btn-ghost btn-sm" onClick={openPromote} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><ArrowRight size={12} />{t("case")}</button>
        </td>
      </tr>
      {promoting && <PromoteRow run={run} onDone={onPromoted} notify={notify} />}
    </>
  );
}

function PromoteRow({ run, onDone, notify }) {
  const { t } = useLang();
  const [input, setInput] = useState(run.input_message || "");
  const [expected, setExpected] = useState("");
  const [busy, setBusy] = useState(false);
  const [line, setLine] = useState("");
  async function submit() {
    setBusy(true);
    try {
      const out = await api.pm.promoteRun(run.id, input, expected);
      setLine(out.golden_line);
      notify(out.case_id != null ? t("Case #{id} added to v#{v} — paste the line into golden/ through a reviewed diff if it belongs there too", { id: out.case_id, v: out.version_id }) : out.note);
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  }
  return (
    <tr>
      <td colSpan={9} style={{ background: "color-mix(in srgb, var(--color-text) 3%, transparent)" }}>
        <div style={{ display: "grid", gap: 8, gridTemplateColumns: "1fr 1fr auto", alignItems: "start" }}>
          <textarea className="ab-textarea" rows={2} value={input} onChange={(e) => setInput(e.target.value)} placeholder={t("What the user asked (input)")} />
          <textarea className="ab-textarea" rows={2} value={expected} onChange={(e) => setExpected(e.target.value)} placeholder={t("What a correct answer must do (expected)")} />
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <button type="button" className="btn btn-sm btn-primary" disabled={busy || !input.trim() || !expected.trim()} onClick={submit}>{busy ? <Spinner size={12} /> : t("Add as eval case")}</button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={onDone}>{line ? t("Done") : t("Cancel")}</button>
          </div>
        </div>
        {line && <pre className="ab-code" style={{ margin: "8px 0 0", fontSize: "var(--text-2xs)", whiteSpace: "pre-wrap" }}>{line}</pre>}
      </td>
    </tr>
  );
}
