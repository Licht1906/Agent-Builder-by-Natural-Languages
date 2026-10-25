import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Eye, EyeOff } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, PageHead, PageShell, Spinner } from "../components/ui";
import { useLang } from "../i18n";
import { WindowPicker, useSeries } from "./pm/shared";
import { RunDrawer } from "./pm/RunDetail";
import OverviewTab from "./pm/OverviewTab";
import RunsTab from "./pm/RunsTab";
import QualityTab from "./pm/QualityTab";
import ReviewTab from "./pm/ReviewTab";
import CostTab from "./pm/CostTab";
import PeopleTab from "./pm/PeopleTab";
import { AgentMascot } from "../components/mascot";

/* ============================================================
   The PM control room — docs/PM-PLAYBOOK.md §5 and ADR-0053 §4.

   Built to the shape the AI Evaluation deck (COMP3011 D10) gives a PM's
   instrument panel, one question per tab on the sidebar (`pm/tabs.js`):

     · Overview — the four production questions (slide 19) as alerts with
       numbers and pre-registered thresholds, not "để ý xem sao";
     · Runs & traces — where failures start (slide 47), cut three ways, and
       the runs themselves with their trace (and, when a backend is
       configured, a link into Langfuse under the same trace_id);
     · Quality by version — offline beside online (slide 18, 65), the
       Baseline · Candidate · Delta report, the meta agent's gate;
     · Review — the expert's screen (slide 62): evidence first, the judge's
       opinion hidden until the person has decided, G/B keys; and the
       calibration those labels buy (slide 53);
     · Cost & latency — and the pause switch;
     · Approvals & feedback — the people in the loop.

   This file is the shell: the window and the simulated toggle apply to every
   tab, so they live here with the one `overview` call, the runs list and the
   filter the Overview's bars set before the Runs tab is even open. `App`
   keeps this component mounted across the pm-* views, so switching tabs
   never refetches or loses a filter.

   Navigation between tabs is the sidebar's alone. The page used to repeat the
   six as a strip under the heading; two lists of the same six, one right
   above the other, only asked the reader which one was in charge.
   ============================================================ */

const TAB_COPY = {
  overview: ["Overview", "Is anything on fire? Four production questions against pre-registered thresholds, and the two cuts that say where to look next."],
  runs: ["Runs & traces", "The runs behind every number. \"read\" opens the trace — question, every tool call, answer — and the label form."],
  quality: ["Quality by version", "Prove a new version is better before it replaces the old one: offline beside online, per version, with the regressions named."],
  review: ["Review", "One run, evidence first, one decision. Every label is also a calibration point for the judge."],
  cost: ["Cost & latency", "Cost per success tells a cheap agent from one that keeps failing; the pause switch is the kill switch."],
  people: ["Approvals & feedback", "Who is waiting on a person, and what people say afterwards."],
};

export default function PMDashboard({ tab = "overview", openTab = () => {}, notify }) {
  const { t } = useLang();
  const series = useSeries();
  const [windowDays, setWindowDays] = useState(28);
  const [includeSimulated, setIncludeSimulated] = useState(false);
  const [overview, setOverview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [runFilter, setRunFilter] = useState({});
  const [runs, setRuns] = useState([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const [openRunId, setOpenRunId] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setOverview(await api.pm.overview(windowDays, includeSimulated));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [windowDays, includeSimulated]);

  const loadRuns = useCallback(async () => {
    setRunsLoading(true);
    try {
      setRuns(await api.pm.runs({ windowDays, includeSimulated, ...runFilter }));
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setRunsLoading(false);
    }
  }, [windowDays, includeSimulated, runFilter, notify]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadRuns(); }, [loadRuns]);

  const refreshAll = useCallback(() => { load(); loadRuns(); }, [load, loadRuns]);

  /* A bar, an alert or an agent name anywhere on the board means "show me
     those runs": set the filter and go where the runs are. */
  const pickRuns = useCallback((filter) => { setRunFilter(filter); openTab("runs"); }, [openTab]);
  const closeRun = useCallback(() => setOpenRunId(null), []);

  const [title, lede] = TAB_COPY[tab] || TAB_COPY.overview;
  const shared = { overview, series, notify, onChanged: refreshAll, pickRuns, onOpen: setOpenRunId };

  return (
    <PageShell width="wide">
        <PageHead title={t(title)} hint={t(lede)}>
          <WindowPicker days={windowDays} onChange={setWindowDays} />
          <button type="button" className={`btn btn-sm ${includeSimulated ? "btn-primary" : "btn-secondary"}`} onClick={() => setIncludeSimulated((v) => !v)} title={t("Show or hide the seed script's simulated rows")} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            {includeSimulated ? <Eye size={13} /> : <EyeOff size={13} />}{t("simulated")}
          </button>
          <button className="btn btn-secondary btn-icon" onClick={refreshAll} disabled={loading} title={t("Reload")}>
            {loading ? <Spinner size={14} /> : <RefreshCw size={14} />}
          </button>
        </PageHead>

        <ErrorNote>{error}</ErrorNote>
        {loading && !overview && <div className="ab-empty"><AgentMascot state="exploring" size={78} /></div>}

        {overview && tab === "overview" && <OverviewTab {...shared} runFilter={runFilter} openTab={openTab} />}
        {overview && tab === "runs" && <RunsTab {...shared} runs={runs} runsLoading={runsLoading} runFilter={runFilter} clearFilter={() => setRunFilter({})} />}
        {overview && tab === "quality" && <QualityTab {...shared} windowDays={windowDays} />}
        {overview && tab === "review" && <ReviewTab {...shared} includeSimulated={includeSimulated} />}
        {overview && tab === "cost" && <CostTab {...shared} />}
        {overview && tab === "people" && <PeopleTab {...shared} />}

      {openRunId != null && (
        <RunDrawer runId={openRunId} codes={overview?.trace_codes || {}} onClose={closeRun} notify={notify} onChanged={refreshAll} series={series} />
      )}
    </PageShell>
  );
}
