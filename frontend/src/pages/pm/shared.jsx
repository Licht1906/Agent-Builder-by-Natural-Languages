import { useCallback, useEffect, useRef, useState } from "react";
import { X, FlaskConical, BookOpen } from "lucide-react";
import { Hint } from "../../components/ui";
import { useLang } from "../../i18n";

/* ============================================================
   What every PM tab shares — docs/PM-PLAYBOOK.md §5, ADR-0053 §4.

   The control room is split into tabs on the sidebar (one question per
   screen), but the pieces they are drawn from are one vocabulary: a rate
   always travels with its n, a panel always has a number and a lede, a
   chart uses one hue for magnitude and blue/red only for a polarity, and a
   drawer always closes on Escape. Kept here so the tabs cannot drift into
   six dialects of the same widget.
   ============================================================ */

/** Quick jumps for the window picker — a starting point, never the whole range.
 *  The backend accepts 1…365 days and so does the input beside these. */
export const WINDOWS = [7, 28, 90];
const MAX_WINDOW = 365;

/** How many days the numbers cover: type any span, or take a quick one.
 *
 *  The typed value is committed on Enter or on blur rather than per keystroke:
 *  every change refetches the whole overview, and "2" on the way to "28" is a
 *  request for a window nobody asked to see. */
export function WindowPicker({ days, onChange }) {
  const { t } = useLang();
  const [draft, setDraft] = useState(String(days));
  useEffect(() => { setDraft(String(days)); }, [days]);

  const commit = () => {
    const parsed = Number.parseInt(draft, 10);
    if (!Number.isFinite(parsed)) { setDraft(String(days)); return; }
    const clamped = Math.min(MAX_WINDOW, Math.max(1, parsed));
    setDraft(String(clamped));
    if (clamped !== days) onChange(clamped);
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <label style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: "var(--text-xs)", color: muted }}>
        <input
          className="ab-input ab-mono"
          type="number"
          min={1}
          max={MAX_WINDOW}
          value={draft}
          aria-label={t("Window, in days")}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } }}
          style={{ width: 64, textAlign: "right" }}
        />
        {t("days")}
      </label>
      {WINDOWS.map((preset) => (
        <button
          key={preset}
          type="button"
          className={`btn btn-sm ${days === preset ? "btn-primary" : "btn-secondary"}`}
          onClick={() => onChange(preset)}
          title={t("Last {n} days", { n: preset })}
        >
          {t("{n}d", { n: preset })}
        </button>
      ))}
    </div>
  );
}

/* ── Columns the reader can drag, spreadsheet-style ───────────────────── */

const MIN_COLUMN = 56;

/** Per-table column widths, dragged by the reader and remembered per browser.
 *
 *  A PM's tables carry free text of wildly different shapes — one approval's
 *  message is a line, the next is a whole email — so no set of widths the
 *  designer picks is right for the row in front of *this* reader. They drag;
 *  `localStorage` keeps it for next time (per table, by `storageKey`), and a
 *  double-click on a grip puts that column back where it started.
 *
 *  Widths live in pixels because that is what a drag produces; the last column
 *  is left unsized so it soaks up whatever is left over. */
export function useColumnSizing(storageKey, defaults) {
  const [widths, setWidths] = useState(() => {
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (Array.isArray(stored) && stored.length === defaults.length) return stored;
    } catch {
      /* storage blocked or corrupt — the defaults still make a usable table */
    }
    return defaults;
  });
  const drag = useRef(null);

  const persist = useCallback((next) => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(next));
    } catch {
      /* a viewer with storage off simply loses the memory, not the table */
    }
  }, [storageKey]);

  useEffect(() => {
    const onMove = (event) => {
      const state = drag.current;
      if (!state) return;
      const next = Math.max(MIN_COLUMN, state.startWidth + event.clientX - state.startX);
      setWidths((current) => current.map((width, index) => (index === state.index ? next : width)));
    };
    const onUp = () => {
      if (!drag.current) return;
      drag.current.grip?.classList.remove("dragging");
      drag.current = null;
      setWidths((current) => { persist(current); return current; });
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [persist]);

  const startDrag = useCallback((index, event) => {
    event.preventDefault();
    event.currentTarget.classList.add("dragging");
    drag.current = { index, startX: event.clientX, startWidth: widths[index], grip: event.currentTarget };
  }, [widths]);

  const reset = useCallback((index) => {
    setWidths((current) => {
      const next = current.map((width, i) => (i === index ? defaults[i] : width));
      persist(next);
      return next;
    });
  }, [defaults, persist]);

  return { widths, startDrag, reset };
}

/** The `<colgroup>` that makes those widths authoritative. */
export function ColumnWidths({ sizing }) {
  return (
    <colgroup>
      {sizing.widths.map((width, index) => (
        <col key={index} style={index === sizing.widths.length - 1 ? undefined : { width }} />
      ))}
    </colgroup>
  );
}

/** One header cell with a drag grip on its right edge. */
export function Th({ sizing, index, children, align }) {
  const { t } = useLang();
  const last = index === sizing.widths.length - 1;
  return (
    <th style={align ? { textAlign: align } : undefined}>
      {children}
      {!last && (
        <span
          className="ab-col-grip"
          role="separator"
          aria-orientation="vertical"
          title={t("Drag to resize · double-click to reset")}
          onPointerDown={(event) => sizing.startDrag(index, event)}
          onDoubleClick={() => sizing.reset(index)}
        />
      )}
    </th>
  );
}

const SERIES = {
  light: { blue: "#2a78d6", red: "#e34948", gray: "#74797e", track: "color-mix(in srgb, #1d1f20 9%, transparent)" },
  dark: { blue: "#3987e5", red: "#e66767", gray: "#9aa0a6", track: "color-mix(in srgb, #e8eaed 12%, transparent)" },
};

export function useSeries() {
  const [mode, setMode] = useState(() => (document.documentElement.dataset.theme === "dark" ? "dark" : "light"));
  useEffect(() => {
    const observer = new MutationObserver(() => setMode(document.documentElement.dataset.theme === "dark" ? "dark" : "light"));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return SERIES[mode];
}

export const pct = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);
export const usd = (x, digits = 4) => (x == null ? "—" : `$${x.toFixed(digits)}`);
export const secs = (x) => (x == null ? "—" : `${x.toFixed(x < 10 ? 1 : 0)}s`);
export const minutes = (m) => {
  if (m == null) return "—";
  if (m < 60) return `${Math.round(m)}m`;
  if (m < 60 * 24) return `${(m / 60).toFixed(1)}h`;
  return `${(m / 60 / 24).toFixed(1)}d`;
};
export const muted = "color-mix(in srgb, var(--color-text) 58%, transparent)";
export const faint = "color-mix(in srgb, var(--color-text) 45%, transparent)";

/** `rate` with its `n` — never the one without the other (slide 18). */
export function RateCell({ rate, smallN, series, bad, title }) {
  if (!rate || rate.n === 0) return <span className="ab-mono" style={{ color: faint }}>—</span>;
  const small = rate.n < smallN;
  return (
    <span className="ab-mono" title={title || `${rate.passed}/${rate.n}`} style={{ color: bad ? series.red : undefined, whiteSpace: "nowrap" }}>
      {pct(rate.rate)} <span style={{ color: faint, fontSize: "var(--text-2xs)" }}>n={rate.n}{small ? "·" : ""}</span>
    </span>
  );
}

/** One widget: a number, a heading, and its explanation one hover away.
 *
 *  `lede` used to be a paragraph under every heading. Six tabs of those is a
 *  wall of prose between a PM and the numbers they opened the room for, so it
 *  became the `Hint` mark beside the title — same words, on demand. */
export function Panel({ n, title, lede, children, wide, actions }) {
  return (
    <section className="blueprint" style={{ padding: "var(--space-4)", gridColumn: wide ? "1 / -1" : undefined, display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
      <i className="corner tl" /><i className="corner tr" /><i className="corner bl" /><i className="corner br" />
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)", display: "flex", alignItems: "center", gap: 8 }}>
          {n != null && <span className="tag tag-neutral" style={{ fontSize: "var(--text-2xs)" }}>#{n}</span>}
          {title}
          <Hint text={lede} />
        </p>
        {actions}
      </div>
      {children}
    </section>
  );
}

/** The two-column grid every tab lays its panels on; `wide` panels span both. */
export function PanelGrid({ children }) {
  return <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(440px, 1fr))", gap: 18 }}>{children}</div>;
}

export function Empty({ children }) {
  return <div style={{ border: "1px dashed var(--color-divider)", padding: "var(--space-6) var(--space-5)", textAlign: "center", fontSize: "var(--text-sm)", color: muted }}>{children}</div>;
}

export function SectionLabel({ children }) {
  return <p className="ab-mono" style={{ margin: "0 0 8px", fontSize: "var(--text-xs)", letterSpacing: ".1em", textTransform: "uppercase", color: faint }}>{children}</p>;
}

export function SimulationBanner({ sim, traces }) {
  const { t } = useLang();
  if (!sim.simulated_runs && traces.runs === traces.with_trace) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, padding: "var(--space-3)", border: "1px dashed var(--color-accent)", marginBottom: 18, fontSize: "var(--text-sm)", lineHeight: 1.5 }}>
      {sim.simulated_runs > 0 && (
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <FlaskConical size={16} style={{ flex: "none", color: "var(--color-accent)" }} />
          <strong>{sim.shown ? t("{sim} simulated runs are included in these numbers", { sim: sim.simulated_runs.toLocaleString() }) : t("{sim} simulated runs exist and are hidden", { sim: sim.simulated_runs.toLocaleString() })}</strong>
          <Hint text={t("seeded by scripts/simulate_pm_runs.py. Toggle \"simulated\" to include them; remove them with --reset.")} />
        </div>
      )}
      {traces.runs > traces.with_trace && (
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <BookOpen size={16} style={{ flex: "none", color: "var(--color-accent)" }} />
          <strong>{t("{n} of {total} runs in this window have a trace", { n: traces.with_trace, total: traces.runs })}</strong>
          <Hint text={t("runs recorded before ADR-0053 kept only their bill. Everything since leaves the question, the answer and every tool call.")} />
        </div>
      )}
    </div>
  );
}

/* ── Charts ───────────────────────────────────────────────────────────── */

export function HBars({ rows, series, emphasis, onPick, active, format = (v) => v, max, labelWidth = 140 }) {
  const top = max ?? Math.max(...rows.map((r) => r.value), 0.0001);
  return (
    <div role="img" style={{ display: "grid", gridTemplateColumns: `minmax(${labelWidth}px, 1fr) 4fr auto`, rowGap: 8, columnGap: 12, alignItems: "center", fontSize: "var(--text-sm)" }}>
      {rows.map((row) => {
        const on = active != null && active === row.key;
        const fill = emphasis == null || emphasis === row.key ? series.blue : series.gray;
        const w = Math.max(0, Math.min(1, row.value / top));
        return (
          <RowButton key={row.key} onClick={onPick ? () => onPick(on ? null : row.key) : undefined} active={on} title={row.tooltip}>
            <span className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "var(--color-text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{row.label}</span>
            <svg width="100%" height="18" style={{ display: "block" }} aria-hidden="true">
              <rect x="0" y="3" width="100%" height="12" fill={series.track} />
              <rect x="0" y="3" width={`${w * 100}%`} height="12" rx="2" fill={fill} />
            </svg>
            <span className="ab-mono" style={{ fontSize: "var(--text-xs)", color: "var(--color-text)", whiteSpace: "nowrap" }}>{format(row.value)}{row.suffix ? <span style={{ color: faint }}> {row.suffix}</span> : null}</span>
          </RowButton>
        );
      })}
    </div>
  );
}

function RowButton({ onClick, active, title, children }) {
  const style = { display: "contents" };
  if (!onClick) return <div style={style} title={title}>{children}</div>;
  return (
    <button type="button" onClick={onClick} title={title} style={{ ...style, cursor: "pointer", font: "inherit", color: "inherit", background: "transparent", border: 0, padding: 0, outline: active ? "1px solid var(--color-accent)" : "none" }}>
      {children}
    </button>
  );
}

/* ── Drawer ───────────────────────────────────────────────────────────── */

export function Drawer({ title, onClose, children, width = 760 }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div role="dialog" aria-modal="true" style={{ position: "fixed", inset: 0, zIndex: 60, display: "flex", justifyContent: "flex-end", background: "color-mix(in srgb, #000 45%, transparent)" }} onClick={onClose}>
      <div className="ab-scrollbar" style={{ width: `min(${width}px, 100vw)`, height: "100%", overflowY: "auto", background: "var(--color-bg)", borderLeft: "1px solid var(--color-divider)", padding: "var(--space-5)", display: "flex", flexDirection: "column", gap: 16 }} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>{title}</p>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="close"><X size={16} /></button>
        </div>
        {children}
      </div>
    </div>
  );
}
