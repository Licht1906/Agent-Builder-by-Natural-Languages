import { useCallback, useRef, useState } from "react";

/* ============================================================
   Draggable pane sizes, remembered per surface.

   A fixed 214px palette is right until the creator renames a node past it, and
   wrong forever after — so every pane that holds user content is adjustable,
   and the number survives a reload because a width someone set is a decision,
   not a session artefact. Double-clicking a divider puts the default back:
   the escape hatch for a pane dragged somewhere unusable.
   ============================================================ */

/** A pane dimension persisted under `pane:<key>`, clamped to [min, max]. */
export function usePaneSize(key, fallback, min, max) {
  const clamp = useCallback((value) => Math.min(max, Math.max(min, value)), [min, max]);
  const [size, setSize] = useState(() => {
    const stored = Number(localStorage.getItem(`pane:${key}`));
    return Number.isFinite(stored) && stored > 0 ? clamp(stored) : fallback;
  });
  const resize = useCallback(
    (value) => {
      const next = clamp(Math.round(value));
      localStorage.setItem(`pane:${key}`, String(next));
      setSize(next);
    },
    [key, clamp]
  );
  const reset = useCallback(() => {
    localStorage.removeItem(`pane:${key}`);
    setSize(fallback);
  }, [key, fallback]);
  return [size, resize, reset];
}

/**
 * The divider between two panes. `grow` says which way dragging enlarges the
 * pane: +1 when the pane sits before the divider (palette), -1 when it sits
 * after (inspector) — so the pane always follows the pointer, whichever side
 * it is on.
 */
export function PaneResizer({ size, onResize, onReset, axis = "x", grow = 1, label }) {
  const start = useRef(null);
  const [active, setActive] = useState(false);

  const at = (event) => (axis === "x" ? event.clientX : event.clientY);

  return (
    <div
      role="separator"
      aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      aria-label={label}
      className={`pane-resizer pane-resizer-${axis}${active ? " active" : ""}`}
      onPointerDown={(event) => {
        event.preventDefault();
        start.current = { from: at(event), size };
        setActive(true);
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (start.current) onResize(start.current.size + grow * (at(event) - start.current.from));
      }}
      onPointerUp={() => {
        start.current = null;
        setActive(false);
      }}
      onDoubleClick={onReset}
      title={label}
    />
  );
}
