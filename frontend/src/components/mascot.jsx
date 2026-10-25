import { useEffect, useRef } from "react";
import "./mascot.css";

/* The animated mascot — the JSX port of `design/AgentLabMascot.tsx`.

   The design package (`design/`) is the source: two transparent layers (robot,
   AL blueprint) plus CSS that animates one named state per product situation.
   This port keeps its markup and classes exactly — `mascot.css` is the package's
   stylesheet with one addition — and departs from the .tsx in two ways the app
   needs:

   - **Gaze.** `follow` makes the robot lean toward the pointer. The design
     package animates `transform` on the robot layer, so the lean lives on a
     wrapper of its own (`.al-mascot__gaze`) between the float animation and the
     artwork — the two compose instead of fighting over one property. Listening
     is on `window`, so the mascot watches the cursor cross the whole page, not
     just its own box; it stands down under reduced motion.

   - **Silence by default.** The package labels every state for screen readers.
     Everywhere this app puts the mascot, the words are already on the screen —
     "thinking" beside the thinking robot, the empty-state headline under the
     waving one — and announcing the picture too reads the page twice. So no
     `label` means hidden from assistive tech; passing one opts back in for the
     rare slot where the mascot stands alone. */

const STATES = new Set([
  "idle", "welcome", "thinking", "processing", "exploring",
  "success", "alert", "builder", "rest",
]);

/** How far the gaze lean goes at full deflection. Small on purpose: the robot
 *  should read as *watching*, not as pinned to the cursor.
 *
 *  A share of the mascot's own box rather than a flat number of pixels, capped
 *  where it used to sit. Seven pixels is a glance on the Designer's 190px hero
 *  and a shove on the 44px one in the shell's corner, where it stopped reading
 *  as a lean and started reading as a creature standing off-centre in its own
 *  frame. Proportional, both sizes lean by the same *amount of themselves*. */
const LEAN_RATIO = 0.05;
const LEAN_MAX_PX = 7;
const LEAN_DEG = 6;

export function AgentMascot({
  state = "idle",
  size = 220,
  follow = false,
  label,
  className = "",
  style,
}) {
  const rootRef = useRef(null);
  const gazeRef = useRef(null);

  useEffect(() => {
    if (!follow) return undefined;
    let reduced = false;
    try {
      reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch { /* no matchMedia — follow anyway */ }
    if (reduced) return undefined;

    let raf = 0;
    const onMove = (event) => {
      if (raf) return; // one reposition per frame, however fast the mouse moves
      raf = requestAnimationFrame(() => {
        raf = 0;
        const root = rootRef.current;
        const gaze = gazeRef.current;
        if (!root || !gaze) return;
        const box = root.getBoundingClientRect();
        const cx = box.left + box.width / 2;
        const cy = box.top + box.height / 2;
        /* Normalised over half the viewport, so the lean saturates when the
           cursor is far away instead of scaling without limit. */
        const nx = Math.max(-1, Math.min(1, (event.clientX - cx) / (window.innerWidth / 2)));
        const ny = Math.max(-1, Math.min(1, (event.clientY - cy) / (window.innerHeight / 2)));
        const lean = Math.min(LEAN_MAX_PX, box.width * LEAN_RATIO);
        gaze.style.transform =
          `translate(${(nx * lean).toFixed(1)}px, ${(ny * lean * 0.6).toFixed(1)}px) rotate(${(nx * LEAN_DEG).toFixed(1)}deg)`;
      });
    };
    const onLeave = () => {
      if (gazeRef.current) gazeRef.current.style.transform = "";
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseout", onLeave);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseout", onLeave);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [follow]);

  const named = STATES.has(state) ? state : "idle";
  return (
    <span
      ref={rootRef}
      className={`al-mascot al-mascot--${named} ${className}`.trim()}
      style={{ "--mascot-size": typeof size === "number" ? `${size}px` : size, ...style }}
      role={label ? "img" : undefined}
      aria-label={label || undefined}
      aria-hidden={label ? undefined : true}
    >
      <span className="al-mascot__halo" aria-hidden="true" />
      <span className="al-mascot__orbit" aria-hidden="true" />
      <span className="al-mascot__wave al-mascot__wave--one" aria-hidden="true" />
      <span className="al-mascot__wave al-mascot__wave--two" aria-hidden="true" />

      <span className="al-mascot__blueprint" aria-hidden="true">
        <img src="/agentlab-mascot/agentlab-blueprint.png" alt="" draggable={false} />
      </span>

      <span className="al-mascot__node-system" aria-hidden="true">
        <i /><i /><i /><b /><b />
      </span>

      <span className="al-mascot__robot" aria-hidden="true">
        <span ref={gazeRef} className="al-mascot__gaze">
          <img src="/agentlab-mascot/agentlab-mascot.png" alt="" draggable={false} />
          <span className="al-mascot__blink" />
        </span>
      </span>

      <span className="al-mascot__sparkles" aria-hidden="true">
        <i /><i /><i />
      </span>
      <span className="al-mascot__alert-ring" aria-hidden="true" />
      <span className="al-mascot__sleep-z al-mascot__sleep-z--one" aria-hidden="true">z</span>
      <span className="al-mascot__sleep-z al-mascot__sleep-z--two" aria-hidden="true">z</span>
    </span>
  );
}

/** The agent, inline, beside a sentence saying what it is doing.
 *
 *  For the places where the work is *about a spot on the page* rather than
 *  about the app — the bubble a reply will appear in, the panel a run's
 *  transcript will fill. Work that is not tied to a spot belongs to the shell's
 *  one agent instead (`agent-presence.jsx`), and screens declare it rather than
 *  drawing it.
 *
 *  One component so the Designer, agent chat, workflow chat and every run panel
 *  cannot drift into four dialects of "please wait". The label stays in the DOM
 *  as text — `Chat.test.jsx` greps for it, and so do people. */
export function AgentBusy({ state = "thinking", label, size = 44 }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <AgentMascot state={state} size={size} />
      <span className="ab-mono" style={{ fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
        {label}
        <span style={{ animation: "agBlink 1s steps(1) infinite" }}>…</span>
      </span>
    </span>
  );
}
