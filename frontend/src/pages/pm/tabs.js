/* The PM control room's tabs — one per question, each its own entry on the
 * sidebar (`AppLayout`) and its own `view` in `App`.
 *
 * Ordered the way a PM reads a morning: is anything on fire (overview) → which
 * runs (runs) → is the new version better (quality) → what do I owe the judge
 * (review) → what is it costing (cost) → who is waiting on us and what are
 * people saying (people). `view` is the string `App` switches on; `label` is an
 * i18n key, translated where it is drawn. */
export const PM_TABS = [
  { id: "overview", view: "pm-overview", label: "Overview" },
  { id: "runs", view: "pm-runs", label: "Runs & traces" },
  { id: "quality", view: "pm-quality", label: "Quality by version" },
  { id: "review", view: "pm-review", label: "Review" },
  { id: "cost", view: "pm-cost", label: "Cost & latency" },
  { id: "people", view: "pm-people", label: "Approvals & feedback" },
];

export const PM_VIEWS = new Set(PM_TABS.map((tab) => tab.view));

/** The tab a `view` string names; the legacy `pm-dashboard` lands on the overview. */
export function pmTabOf(view) {
  return PM_TABS.find((tab) => tab.view === view)?.id ?? "overview";
}
