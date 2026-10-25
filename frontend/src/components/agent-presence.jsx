import { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState } from "react";
import { AgentMascot } from "./mascot";
import "./agent-dock.css";

/* ============================================================
   One agent, present the whole time — and the only thing that knows it.

   The mascot used to be a picture a screen decided to draw. That cannot survive
   a page change: every screen is swapped out when the view changes, so anything
   they own blinks out with them, and the agent reads as a decoration that comes
   and goes rather than as the thing working for you.

   So presence is inverted. The mascot is mounted **once**, in the shell, beside
   everything that swaps (`AppLayout`, at the shell's root). Screens never
   render it. They *declare what is happening* — "I am composing a workflow",
   "I am judging a run" — and this module decides what the one agent should
   therefore be doing.

   That inversion is what buys three things at once:

   - **Continuity.** Nothing unmounts on a page change, so the agent is
     literally the same element before and after; it carries on animating
     across the transition instead of being rebuilt.
   - **Coverage.** A screen wires an operation up in one line
     (`useAgentActivity`), so "every operation is animated" is a property of the
     mechanism rather than forty places remembering to do it.
   - **One truth.** Two things happening at once cannot produce two mascots
     disagreeing: the activities are ranked and the most urgent one wins.

   Where it is drawn — and why it moves the way it does — is `agent-dock.css`.
   ============================================================ */

/** Which state wins when several are live at once.
 *
 *  Ranked by what a person most needs to know, not by what started last: a
 *  permission prompt outranks a running job, a running job outranks idle
 *  browsing. Equal ranks fall back to the newest, which is the one the person
 *  just caused. */
const RANK = {
  alert: 6,
  processing: 5,
  builder: 5,
  thinking: 4,
  exploring: 3,
  success: 2,
  welcome: 1,
  rest: 0,
  idle: 0,
};

const AgentPresenceContext = createContext(null);

/* A context with working defaults rather than a thrown error, so a screen can
   be rendered on its own — in a test, in the SSR smoke run — without dragging
   the whole shell in. Declaring an activity with nobody listening is not a
   bug; it just means nothing is showing it. */
const SILENT = {
  activities: {},
  declare: () => {},
  withdraw: () => {},
  standIns: {},
  embody: () => {},
  release: () => {},
  poses: {},
  pose: () => {},
  unpose: () => {},
  sides: {},
  face: () => {},
  unface: () => {},
};

export function AgentPresenceProvider({ children }) {
  const [activities, setActivities] = useState({});
  /* Screens that are drawing the agent themselves, full size — see
     `useAgentStandIn`. Kept apart from the activities rather than folded in as
     a pseudo-state: this says nothing about what the agent is *doing*, only
     about where it is being drawn, and mixing the two would put it in the
     ranking where it would have to beat or lose to real work. */
  const [standIns, setStandIns] = useState({});
  /* ── Two more per-screen declarations, in the same shape as the activities
        and for the same reason: the screen knows, the shell does not, and a
        list inside AppLayout of "which views want what" would have to be
        edited by whoever is not editing that file.

     `poses` is what the agent does when it is *not* working — the Canvas tab
     wants the blueprint out for as long as it is open, which is a fact about
     where you are standing, not about a job in flight. Kept out of the
     activity ranking on purpose: declared as an activity, "builder" ranks 5
     and would mask a real thinking or exploring state underneath it.

     `sides` is which corner it stands in. A screen with a composer at the
     bottom declares the side away from it, so the agent is beside the
     conversation instead of sitting on the thing you are typing into. */
  const [poses, setPoses] = useState({});
  const [sides, setSides] = useState({});
  /* A counter, not a clock: `Date.now()` ties on fast machines, and all this
     has to do is order two activities that arrived in the same millisecond. */
  const order = useRef(0);

  const declare = useCallback((id, entry) => {
    order.current += 1;
    const seq = order.current;
    setActivities((current) => ({
      ...current,
      /* `at` is a clock, and it is the one thing here that must *not* be
         refreshed on a re-declaration: it is what the dock counts seconds from,
         and StrictMode's double-invoked effects would otherwise restart every
         operation's stopwatch a beat after it started. */
      [id]: { ...entry, seq, at: current[id]?.at ?? Date.now() },
    }));
  }, []);

  const withdraw = useCallback((id) => {
    setActivities((current) => {
      if (!(id in current)) return current; // no re-render for a no-op cleanup
      const next = { ...current };
      delete next[id];
      return next;
    });
  }, []);

  const embody = useCallback((id) => {
    setStandIns((current) => (id in current ? current : { ...current, [id]: true }));
  }, []);

  const release = useCallback((id) => {
    setStandIns((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
  }, []);

  const pose = useCallback((id, state) => {
    setPoses((current) => (current[id] === state ? current : { ...current, [id]: state }));
  }, []);
  const unpose = useCallback((id) => {
    setPoses((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
  }, []);

  const face = useCallback((id, side) => {
    setSides((current) => (current[id] === side ? current : { ...current, [id]: side }));
  }, []);
  const unface = useCallback((id) => {
    setSides((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
  }, []);

  const value = useMemo(
    () => ({ activities, declare, withdraw, standIns, embody, release, poses, pose, unpose, sides, face, unface }),
    [activities, declare, withdraw, standIns, embody, release, poses, pose, unpose, sides, face, unface]
  );
  return <AgentPresenceContext.Provider value={value}>{children}</AgentPresenceContext.Provider>;
}

const usePresence = () => useContext(AgentPresenceContext) || SILENT;

/** Tell the shell's agent what this screen is doing, for as long as it does it.
 *
 *  The whole API a screen needs for "this operation is animated":
 *
 *      useAgentActivity("builder", busy === "generate", t("composing the workflow"));
 *
 *  Withdrawn on unmount as well as when `active` goes false, so a screen the
 *  person navigates away from mid-request cannot leave the agent stuck
 *  working on something nobody is waiting for any more. */
export function useAgentActivity(state, active, label) {
  const { declare, withdraw } = usePresence();
  const id = useId();

  useEffect(() => {
    if (!active) return undefined;
    declare(id, { state, label });
    return () => withdraw(id);
  }, [active, state, label, id, declare, withdraw]);
}

/** "This screen is the agent's body right now — corner, stand down."
 *
 *  The home stage sits the agent in the middle of the page at 190px, watching
 *  the cursor; the empty states draw it at 120–170px. On those screens the
 *  corner dock is a *second copy of the same creature*, which is worse than no
 *  presence at all: continuity is the whole point, and two of something cannot
 *  be one continuous thing.
 *
 *  Declared by the screen rather than decided by the shell, for the same reason
 *  activities are. `AppLayout` carrying a list of which views draw their own
 *  mascot would be exactly the coupling this module exists to avoid — it would
 *  have to be edited every time a screen gains or loses one, by someone who is
 *  not editing this file.
 *
 *  The dock is faded, never unmounted: it is still the same element it was
 *  before the page changed and will be after, which is what makes the agent one
 *  creature rather than several.
 */
export function useAgentStandIn(active = true) {
  const { embody, release } = usePresence();
  const id = useId();

  useEffect(() => {
    if (!active) return undefined;
    embody(id);
    return () => release(id);
  }, [active, id, embody, release]);
}

/** "While this screen is open, the agent's resting pose is this."
 *
 *  The Canvas tab wants the agent wearing the blueprint for as long as Canvas
 *  is the open tab — not only while a workflow is actually being composed:
 *
 *      useAgentPose("builder", tab === "canvas");
 *
 *  A pose is a floor, not a competitor. Real work always shows through it,
 *  because a pose is only consulted when no activity is running; declaring
 *  "builder" as an activity instead would rank it 5 and quietly mask the
 *  thinking and exploring states of anything that ran underneath it.
 *
 *  It also outranks the doze: the screen is still open, so the declaration is
 *  still true, and "always wearing the blueprint" has to survive somebody
 *  reading the canvas for two minutes without touching the mouse. */
export function useAgentPose(state, active = true) {
  const { pose, unpose } = usePresence();
  const id = useId();

  useEffect(() => {
    if (!active) return undefined;
    pose(id, state);
    return () => unpose(id);
  }, [active, state, id, pose, unpose]);
}

/** "This screen has a chat bar — stand on the other side of it."
 *
 *  Two screens put a composer at the bottom of the page: the Builder canvas's
 *  graph chat, bottom-centre over the graph, and the agent chat. On those, a
 *  dock in the bottom-right corner sits opposite the thing the person is
 *  typing into, and its plate unfurls across the bar's own right edge.
 *
 *  Declared by the screen rather than sniffed by the dock: the dock cannot
 *  know what a page has drawn, and any rule it guessed from the DOM would be
 *  a second, silent definition of "has a chat bar". Last declaration wins,
 *  which is the same tie-break the activities use. */
export function useAgentSide(side, active = true) {
  const { face, unface } = usePresence();
  const id = useId();

  useEffect(() => {
    if (!active) return undefined;
    face(id, side);
    return () => unface(id);
  }, [active, side, id, face, unface]);
}

/** Which side the dock should stand on right now. */
export function useAgentSideChoice() {
  const { sides } = usePresence();
  const declared = Object.values(sides);
  return declared.length ? declared[declared.length - 1] : "right";
}

/** Whether any screen is currently doing the agent's job for it. */
export function useAgentStoodDown() {
  const { standIns } = usePresence();
  return Object.keys(standIns).length > 0;
}

/** A moment rather than a stretch — a page change, a finished run.
 *
 *  Returns a stable `beat(state, label, ms)`. Separate from `useAgentActivity`
 *  because the thing being shown has already finished by the time it is shown:
 *  there is no `active` that could go false, only a length of time to hold the
 *  pose for. */
export function useAgentBeat() {
  const { declare, withdraw } = usePresence();
  const id = useId();
  const timer = useRef(null);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
    withdraw(id);
  }, [id, withdraw]);

  return useCallback((state, label, ms = 900) => {
    if (timer.current) clearTimeout(timer.current);
    declare(id, { state, label });
    timer.current = setTimeout(() => {
      timer.current = null;
      withdraw(id);
    }, ms);
  }, [declare, withdraw, id]);
}

/** Everything running right now, most urgent first.
 *
 *  The ranking used to be private, because only the winner was ever drawn. It
 *  is exported now for the one thing the old header slot could not say: that
 *  the operation you are watching is not the only one in flight. */
export function useAgentActivities() {
  const { activities } = usePresence();
  return useMemo(
    () => Object.entries(activities)
      .map(([id, entry]) => ({ id, ...entry }))
      .sort((a, b) => (RANK[b.state] ?? 0) - (RANK[a.state] ?? 0) || b.seq - a.seq),
    [activities]
  );
}

/** What the one agent should be doing right now, and why.
 *
 *  `posed` says the state came from a screen's standing declaration rather
 *  than from work in flight — which is what lets the dock keep the blueprint
 *  out through the doze without also claiming something is running. */
export function useAgentState() {
  const live = useAgentActivities();
  const { poses } = usePresence();
  return useMemo(() => {
    const winner = live[0];
    if (winner) return { state: winner.state, label: winner.label || null, busy: true, since: winner.at ?? null, posed: false };
    const declared = Object.values(poses);
    const standing = declared.length ? declared[declared.length - 1] : null;
    return { state: standing || "idle", label: null, busy: false, since: null, posed: Boolean(standing) };
  }, [live, poses]);
}

/* ── How long the agent has been at it ──
   Silent for the first few seconds. A stopwatch that starts on every click is
   noise; one that appears only once an operation is *taking a while* is the
   answer to the question the person was about to ask. */
const ELAPSED_AFTER_MS = 3000;

export function elapsedLabel(ms) {
  if (!(ms >= ELAPSED_AFTER_MS)) return null;
  const total = Math.round(ms / 1000);
  if (total < 60) return `${total}s`;
  return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s`;
}

/* How near the pointer has to get before the dock fades out from under it.
   Generous, because the point is to be gone *before* you arrive, not to flicker
   as you land on it. */
const YIELD_PAD = 14;

/* Nothing at all for this long — no activity, no pointer, no key — and the
   agent dozes. Long enough that reading a dossier does not put it to sleep
   mid-sentence, short enough that an abandoned tab is not animating at you. */
const REST_AFTER_MS = 75000;

/* ── Idle is a stretch of time, not one drawing ──
   Nine poses are drawn and the dock was reaching for two of them: "idle" for
   every quiet moment and "rest" once it dozed. A creature that holds exactly
   one shape for as long as nothing happens stops reading as alive and starts
   reading as an icon of a robot, which is the same complaint as it being too
   faint — both are it not being *present*.

   So while nothing is running it glances about: every LOOK_EVERY_MS it takes
   one of these for LOOK_FOR_MS and goes back to idle. Both are honest poses of
   doing nothing — neither claims work is happening, which is the one thing an
   idle animation must never do. Skipped entirely under reduced motion, and
   never while an activity or a screen's own pose is in charge. */
const IDLE_LOOKS = ["exploring", "welcome"];
const LOOK_EVERY_MS = 13000;
const LOOK_FOR_MS = 2400;

/** The agent itself, mounted once by the shell.
 *
 *  Idle is not "hidden": the point is that it is the same creature the whole
 *  time, so when nothing is happening it stays on screen and idles. What comes
 *  and goes is the sentence beside it, which doubles as the app's global "what
 *  is running right now" line — the thing that used to be a spinner buried on
 *  whichever tab started the work.
 *
 *  Everything here is a *readout*. There is no button, no menu, nothing to
 *  click, and the whole dock is `pointer-events: none`, so it cannot take a
 *  click meant for the screen underneath it however badly the two overlap. That
 *  is deliberate and it is what makes a fixed corner safe: the Builder's
 *  inspector runs the full right edge of the canvas when a node is selected,
 *  and the dock has to be able to sit on top of it without stealing anything. */
export function AgentPresence({ size = 44 }) {
  const live = useAgentActivities();
  const { state, label, busy, since, posed } = useAgentState();
  const stoodDown = useAgentStoodDown();

  const side = useAgentSideChoice();

  const dockRef = useRef(null);
  const [ghost, setGhost] = useState(false);
  const [resting, setResting] = useState(false);
  const [look, setLook] = useState(null);
  const [now, setNow] = useState(() => Date.now());
  const lastStir = useRef(Date.now());

  /* Watching the pointer does two jobs at once, so it is one listener.
     Anywhere on the page: the agent is awake. Near the corner: the agent gets
     out of the way — that is the whole answer to "is it in front of something
     I need", and it is why there is no dismiss control to find. */
  useEffect(() => {
    let raf = 0;
    let point = null;

    const stir = (event) => {
      lastStir.current = Date.now();
      setResting(false);
      if (event.clientX == null) return; // a key, not a pointer
      point = event;
      if (raf) return; // one rect read per frame, however fast the mouse moves
      raf = requestAnimationFrame(() => {
        raf = 0;
        const box = dockRef.current?.getBoundingClientRect();
        if (!box) return;
        setGhost(
          point.clientX >= box.left - YIELD_PAD && point.clientX <= box.right + YIELD_PAD &&
          point.clientY >= box.top - YIELD_PAD && point.clientY <= box.bottom + YIELD_PAD
        );
      });
    };
    /* A pointer that leaves the window never reports leaving the dock, so
       without this the agent stays faded for as long as the tab is unfocused. */
    const restore = () => setGhost(false);

    window.addEventListener("mousemove", stir, { passive: true });
    window.addEventListener("keydown", stir);
    document.addEventListener("mouseleave", restore);
    window.addEventListener("blur", restore);
    return () => {
      window.removeEventListener("mousemove", stir);
      window.removeEventListener("keydown", stir);
      document.removeEventListener("mouseleave", restore);
      window.removeEventListener("blur", restore);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  /* Work is a reason to be awake too — a run that finishes while the person is
     reading should find the agent on its feet, not asleep. */
  useEffect(() => {
    lastStir.current = Date.now();
    setResting(false);
  }, [live.length]);

  /* One coarse timer for the doze rather than a timeout reset on every mouse
     move: at 60Hz that would be sixty timer churns a second, forever. */
  useEffect(() => {
    const tick = setInterval(() => {
      if (Date.now() - lastStir.current >= REST_AFTER_MS) setResting(true);
    }, 5000);
    return () => clearInterval(tick);
  }, []);

  /* The idle glance. Two timers rather than one interval that does both jobs,
     because the look is much shorter than the gap between looks and folding
     them together means either a coarse gap or a jittery look. */
  useEffect(() => {
    if (busy || resting || posed) { setLook(null); return undefined; }
    let reduced = false;
    try { reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch { /* no matchMedia */ }
    if (reduced) return undefined;

    let hold = null;
    const every = setInterval(() => {
      setLook(IDLE_LOOKS[Math.floor(Math.random() * IDLE_LOOKS.length)]);
      hold = setTimeout(() => setLook(null), LOOK_FOR_MS);
    }, LOOK_EVERY_MS);
    return () => {
      clearInterval(every);
      if (hold) clearTimeout(hold);
      setLook(null);
    };
  }, [busy, resting, posed]);

  /* The stopwatch runs only while something is running. */
  useEffect(() => {
    if (!busy) return undefined;
    setNow(Date.now());
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [busy]);

  /* A screen's standing declaration outranks the doze: the Canvas tab is still
     open, so "the agent is wearing the blueprint here" is still true however
     long nobody has touched the mouse. */
  const dozing = resting && !busy && !posed;
  const pose = dozing ? "rest" : (look && !busy && !posed ? look : state);
  const elapsed = busy && since ? elapsedLabel(now - since) : null;
  /* Only the ones that brought a sentence: a page-change beat declares a pose
     with no words, and a blank row saying nothing is worse than no row. */
  const others = live.slice(1).filter((entry) => entry.label);

  return (
    <div
      ref={dockRef}
      className="ag-dock"
      /* The handle a browser check holds on to when it asks the question this
         whole module exists to answer: is the agent after a page change the
         same element it was before it? */
      data-agent-presence=""
      data-busy={busy ? "1" : "0"}
      data-state={pose}
      data-ghost={ghost ? "1" : "0"}
      data-resting={dozing ? "1" : "0"}
      data-standin={stoodDown ? "1" : "0"}
      data-side={side}
      data-posed={posed ? "1" : "0"}
    >
      {others.length > 0 && (
        <div className="ag-dock__stack" aria-hidden="true">
          {others.slice(0, 2).map((entry) => (
            <span key={entry.id} className="ag-dock__also">{entry.label}</span>
          ))}
        </div>
      )}

      <div className="ag-dock__row">
        {label && (
          /* Hidden from assistive tech because the live region below is the
             same words: drawn once, announced once. */
          <span className="ag-dock__plate" aria-hidden="true">
            <span className="ag-dock__label">{label}</span>
            {elapsed && <span className="ag-dock__elapsed ab-mono">{elapsed}</span>}
            {others.length > 0 && <span className="ag-dock__count ab-mono">+{others.length}</span>}
          </span>
        )}
        <span className="ag-dock__puck">
          {/* The gaze is the movement that makes the corner feel occupied
              rather than decorated: the agent leans toward the pointer, which
              is to say toward whatever the person is doing. `mascot.jsx` stands
              it down under `prefers-reduced-motion`. */}
          <AgentMascot state={pose} size={size} follow />
          <span className="ag-dock__scan" />
        </span>
      </div>

      {/* The one live region in the app, always mounted so that what lands in
          it later is actually announced. Empty unless there is something to
          say: the page-change beat has no words, and "the agent is idle" is not
          news worth interrupting anybody for. */}
      <span className="ag-dock__live" role="status" aria-live="polite">{busy && label && !stoodDown ? label : ""}</span>
    </div>
  );
}
