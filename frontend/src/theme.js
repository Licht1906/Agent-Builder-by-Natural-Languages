/* Industry Design System — Source of truth for Agentlab UI.
   Supports Light Mode (default) & Dark Mode via [data-theme="dark"]. */

export const GLOBAL_CSS = `
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@400;600;700&family=Barlow:ital,wght@0,400;0,500;0,600;0,700;1,400&family=Outfit:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --color-bg: #f2f2f3;
  --color-surface: #e9e9ea;
  --color-text: #1d1f20;
  --color-accent: #5980a6;
  --color-accent-2: #728fab;
  --color-divider: color-mix(in srgb, #1d1f20 16%, transparent);

  --color-neutral-100: #f5f5f8;
  --color-neutral-200: #e7e7ea;
  --color-neutral-300: #d4d4d7;
  --color-neutral-400: #b7b7ba;
  --color-neutral-500: #98989b;
  --color-neutral-600: #7a7a7d;
  --color-neutral-700: #5d5d60;
  --color-neutral-800: #424244;
  --color-neutral-900: #2b2b2d;

  --color-accent-100: #eef6ff;
  --color-accent-200: #d6ebff;
  --color-accent-300: #b5d9fd;
  --color-accent-400: #94bce3;
  --color-accent-500: #749dc4;
  --color-accent-600: #597ea3;
  --color-accent-700: #416180;
  --color-accent-800: #2c455d;
  --color-accent-900: #1d2d3d;

  /* The identity board's four colours, exact — the values the mark is drawn
     in, taken from the identity board itself (design/assets/). There is no
     BRAND.md in the repo; these four literals are the record.

     Deliberately *not* wired into --color-accent. The board's blue is the mark's
     own blue — it is what the mascot is drawn in, and it has to stay that value
     or the logo stops matching the page it sits on. The accent ramp above is a
     different job: it tints every button, tab and active row in the product, and
     re-pointing it at #1d72fc would restyle the whole app, which is a decision
     about the product's look, not about the logo. So the brand colours live here
     under their own names, for brand surfaces to reach for by hand. Adopting
     them app-wide is a decision about the product's look, not about the logo.
     Same values in both themes:
     a logo that changes colour with the theme is a different logo. */
  --brand-ink: #05264f;
  --brand-blue: #1d72fc;
  --brand-cyan: #5ee8fd;
  --brand-paper: #f8fbfe;

  --font-heading: "Barlow Condensed", system-ui, sans-serif;
  --font-heading-weight: 600;
  --font-body: "Barlow", system-ui, sans-serif;

  /* ── The scale (ADR-0057 §1) ──
     One 4px grid, and nothing between the rungs. The old values were a 3.4px
     grid nothing in JSX ever reached for, so every screen invented its own
     number instead and the app ended up with 86 distinct paddings. These are
     the only spacings a page is allowed to name. */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --space-10: 40px;
  --space-12: 48px;
  --space-16: 64px;
  --space-20: 80px;

  /* ── The type ladder (ADR-0057 §2) ──
     Eight rungs for thirty-four literals. A size that is not on the ladder is
     a size the reader has to sort out from the ones that are, and at 1.5px
     apart they cannot — so the differences that carried no meaning are gone. */
  --text-2xs: 11px;
  --text-xs: 12.5px;
  --text-sm: 14px;
  --text-md: 15.5px;
  --text-lg: 17px;
  --text-xl: 20px;
  --text-2xl: 26px;
  --text-3xl: 40px;

  /* ── The page frame (ADR-0057 §3) ──
     Where a page starts and how wide it runs. Named here so that changing the
     app's margins is one edit and not thirty-seven. */
  --page-pad-x: 24px;
  --page-pad-top: 20px;
  --page-pad-bottom: 40px;
  --page-max: 1180px;
  --page-max-narrow: 860px;
  --page-max-wide: 1440px;
  --gap-section: 28px;
  --gap-stack: 12px;

  --radius-sm: 2px;
  --radius-md: 4px;
  --radius-lg: 7px;

  --shadow-sm: 0 1px 2px color-mix(in srgb, #2b2b2d 14%, transparent);
  --shadow-md: 0 3px 10px color-mix(in srgb, #2b2b2d 16%, transparent);
  --shadow-lg: 0 12px 32px color-mix(in srgb, #2b2b2d 22%, transparent);

  /* Legacy app variable aliases for backward compatibility */
  --ab-bg: var(--color-bg);
  --ab-bg-deep: var(--color-surface);
  --ab-fg: var(--color-text);
  --ab-card: var(--color-surface);
  --ab-secondary: var(--color-neutral-200);
  --ab-muted: color-mix(in srgb, var(--color-text) 55%, transparent);
  --ab-primary: var(--color-accent);
  --ab-primary-fg: var(--color-bg);
  --ab-accent: var(--color-accent-700);
  --ab-destructive: #ef4444;
  --ab-warn: #f59e0b;
  --ab-sky: var(--color-accent);
  --ab-pink: #ec4899;
  --ab-border: var(--color-divider);
}

[data-theme="dark"] {
  --color-bg: #14171a;
  --color-surface: #1c2024;
  --color-text: #e8eaed;
  --color-accent: #8fb3d9;
  --color-accent-2: #a6c4e2;
  --color-divider: color-mix(in srgb, #e8eaed 18%, transparent);
  --color-accent-100: #22323f;
  --color-accent-200: #2c4053;
  --color-accent-600: #a6c4e2;
  --color-accent-700: #b5d0e9;
  --color-accent-800: #cfe1f2;
  --color-accent-900: #0f1a24;
  --color-neutral-100: #24272b;
  --color-neutral-800: #cfd2d6;

  --ab-bg: #14171a;
  --ab-bg-deep: #0f1215;
  --ab-fg: #e8eaed;
  --ab-card: #1c2024;
  --ab-secondary: #24272b;
  --ab-muted: #9aa0a6;
  --ab-primary: #8fb3d9;
  --ab-primary-fg: #14171a;
  --ab-accent: #8fb3d9;
  --ab-border: color-mix(in srgb, #e8eaed 18%, transparent);
}

@keyframes agUp { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
@keyframes agIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes agPulse { 0%,100% { opacity: 1; transform: scale(1); } 50% { opacity: .3; transform: scale(.8); } }
@keyframes agBlink { 0%,49% { opacity: 1; } 50%,100% { opacity: 0; } }
@keyframes agDrift { from { background-position: 0 0, 0 0; } to { background-position: 46px 46px, 46px 46px; } }
@keyframes agRing { 0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--color-accent) 45%, transparent); } 100% { box-shadow: 0 0 0 12px transparent; } }
@keyframes agSlideIn { from { opacity: 0; transform: translate(-50%, 14px); } to { opacity: 1; transform: translate(-50%, 0); } }
@keyframes agDash { to { stroke-dashoffset: -24; } }
@keyframes spin { to { transform: rotate(360deg); } }

/* The base size the design system always meant to set and this file never did —
   so anything not carrying an explicit size fell through to the browser's
   default instead of the system's. Everything above is scaled from here. */
html, body { margin: 0; padding: 0; height: 100%; background: var(--color-bg); color: var(--color-text); font-family: var(--font-body); font-size: var(--text-md); line-height: 1.55; }
#root { min-height: 100vh; }

.ab-root { font-family: var(--font-body); background: var(--color-bg); color: var(--color-text); min-height: 100vh; position: relative; }
.ab-root * { box-sizing: border-box; }
.ab-mono { font-family: 'JetBrains Mono', monospace; }
/* A bare element selector, deliberately, and it has to stay one.
   The UA stylesheet gives every button "color: buttontext" — black, and it does
   not follow the theme. This sheet styled buttons three ways and never set
   colour, so anything inside a button that said "color: inherit" (the sidebar's
   nav icons, most of all) inherited black and vanished on the dark rail: the
   inactive icons measured 1.17:1 against #14171a, against 8.24:1 for the active
   one that names its own colour.
   Author rules beat UA rules at any specificity, so "button" alone is enough to
   displace the default — and being the weakest possible selector is the point:
   .btn-primary, .tag and every other class that states a colour still win. The
   moment this carries a class (.ab-root button) it starts outranking them and
   every filled button in the app loses its foreground. */
button { color: inherit; }
.ab-root button { font-family: var(--font-heading); font-weight: var(--font-heading-weight); cursor: pointer; }
.ab-root input, .ab-root textarea, .ab-root select { font-family: var(--font-body); }

h1, h2, h3, h4, h5, h6 { font-family: var(--font-heading); font-weight: var(--font-heading-weight); line-height: 1.12; letter-spacing: -0.015em; margin: 0 0 var(--space-2); }
/* An in-app heading labels a group; it is not a banner. 44.5px was a
   marketing size, and the page's own name is printed once in the shell header
   anyway (ADR-0057 s4), so the rungs here start where a console's do. The
   three entrances — Landing, sign-in, the Designer's home stage — are the
   exception and say so out loud with .ab-display rather than by picking a
   number no other screen uses. */
h1 { font-size: var(--text-xl); }
h2 { font-size: var(--text-lg); }
h3 { font-size: var(--text-md); }
.ab-display {
  font-family: var(--font-heading); font-weight: var(--font-heading-weight);
  font-size: clamp(28px, 4.2vw, var(--text-3xl)); line-height: 1.06;
  letter-spacing: -0.02em; text-wrap: balance; margin: 0;
}
.ab-display-xl { font-size: clamp(34px, 6.4vw, 64px); }

/* Blueprint Wireframe Objects */
.blueprint {
  position: relative;
  border: 1px solid var(--color-divider);
  border-radius: 0;
}
.blueprint > .corner {
  position: absolute; width: 11px; height: 11px;
  color: color-mix(in srgb, var(--color-text) 55%, transparent);
  pointer-events: none;
}
.blueprint > .corner::before, .blueprint > .corner::after {
  content: ""; position: absolute; background: currentColor;
}
.blueprint > .corner::before { left: 5px; top: 0; width: 1px; height: 100%; }
.blueprint > .corner::after  { top: 5px; left: 0; width: 100%; height: 1px; }
.blueprint > .corner.tl { top: -6px; left: -6px; }
.blueprint > .corner.tr { top: -6px; right: -6px; }
.blueprint > .corner.bl { bottom: -6px; left: -6px; }
.blueprint > .corner.br { bottom: -6px; right: -6px; }

/* Component Styles */
.btn, .ab-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  cursor: pointer; text-decoration: none;
  font-family: var(--font-heading); font-weight: var(--font-heading-weight);
  font-size: var(--text-sm); line-height: 1.2; color: var(--color-text);
  background: transparent; border: 1px solid var(--color-divider);
  padding: var(--space-2) calc(var(--space-3) * 1.2);
  border-radius: 0; transition: all .15s ease; white-space: nowrap;
}
.btn:disabled, .ab-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.btn-primary, .ab-btn-primary { background: var(--color-accent); color: var(--color-bg); border-color: var(--color-accent); }
.btn-primary:hover:not(:disabled), .ab-btn-primary:hover:not(:disabled) { background: var(--color-accent-600); }
.btn-primary:active:not(:disabled), .ab-btn-primary:active:not(:disabled) { background: var(--color-accent-700); }
.btn-secondary, .ab-btn-outline { border-color: var(--color-divider); background: transparent; color: var(--color-text); }
.btn-secondary:hover:not(:disabled), .ab-btn-outline:hover:not(:disabled) { background: color-mix(in srgb, var(--color-text) 7%, transparent); }
.btn-ghost, .ab-btn-ghost { color: var(--color-accent); border-color: transparent; background: transparent; }
.btn-ghost:hover:not(:disabled), .ab-btn-ghost:hover:not(:disabled) { background: color-mix(in srgb, var(--color-accent) 10%, transparent); }
.btn-icon, .ab-btn-icon { width: 36px; height: 36px; padding: 0; }
.btn-block { width: 100%; margin-top: var(--space-2); }
/* btn-sm / btn-danger are the names the rest of the btn-* family uses; the
   ab- spellings stay as aliases for the screens still on them. */
.btn-sm, .ab-btn-sm { padding: var(--space-1) var(--space-2); font-size: var(--text-xs); }
.btn-danger, .ab-btn-danger { background: color-mix(in srgb, #ef4444 10%, transparent); color: #b32828; border-color: color-mix(in srgb, #ef4444 35%, transparent); }
.btn-danger:hover:not(:disabled), .ab-btn-danger:hover:not(:disabled) { background: color-mix(in srgb, #ef4444 18%, transparent); }
[data-theme="dark"] .btn-danger, [data-theme="dark"] .ab-btn-danger { color: #f87171; }

.field > label { display: block; font-size: var(--text-xs); margin-bottom: 5px; color: color-mix(in srgb, var(--color-text) 70%, transparent); }
.input, .ab-input, select, .ab-select {
  width: 100%; box-sizing: border-box; min-height: 34px; padding: var(--space-1) var(--space-2); font: inherit;
  font-size: var(--text-sm); color: var(--color-text); caret-color: var(--color-accent);
  background: var(--color-surface);
  border: 1px solid var(--color-divider); border-radius: 0; outline: none;
}
select, .ab-select { height: 36px; cursor: pointer; }
.input:hover, .ab-input:hover, select:hover, .ab-select:hover { border-color: color-mix(in srgb, var(--color-text) 45%, transparent); }
.input:focus-visible, .ab-input:focus, select:focus-visible, .ab-select:focus { border-color: var(--color-accent); outline-offset: 0; }
textarea.input, textarea, .ab-textarea {
  width: 100%; box-sizing: border-box; min-height: 80px; resize: vertical;
  background: var(--color-surface); border: 1px solid var(--color-divider);
  border-radius: 0; color: var(--color-text); padding: var(--space-2) var(--space-3); outline: none; font: inherit; font-size: var(--text-sm);
}

.card, .ab-card {
  display: flex; flex-direction: column; gap: var(--space-2);
  padding: var(--space-3); border-radius: 0; background: var(--color-surface); border: 1px solid var(--color-divider);
}
.card-kicker { font-size: var(--text-2xs); letter-spacing: 0.1em; text-transform: uppercase; color: var(--color-accent); }
.card-title { font-family: var(--font-heading); font-weight: var(--font-heading-weight); font-size: var(--text-lg); line-height: 1.2; }
.card-body { margin: 0; font-size: var(--text-sm); opacity: 0.8; flex: 1; }

.ab-table { width: 100%; border-collapse: collapse; font-size: var(--text-xs); }
.ab-table th { text-align: left; padding: var(--space-2) var(--space-3); color: var(--ab-muted); border-bottom: 1px solid var(--ab-border); font-weight: 600; }
.ab-table td { padding: var(--space-2) var(--space-3); border-bottom: 1px solid var(--ab-border); vertical-align: top; }

/* A table whose columns the reader drags, spreadsheet-style. table-layout:
   fixed is what makes a <colgroup> width authoritative — without it the
   browser keeps re-measuring content and a dragged column springs back. The
   grip is a hit area wider than the line it draws, because a 1px target is
   a target nobody hits. (No backticks in here: this whole sheet is one
   template literal.) */
.ab-table-sized { table-layout: fixed; }
.ab-table-sized th { position: relative; overflow: hidden; text-overflow: ellipsis; }
.ab-table-sized td { overflow-wrap: anywhere; }
.ab-col-grip {
  position: absolute; top: 0; right: -4px; width: 9px; height: 100%;
  cursor: col-resize; touch-action: none; user-select: none; z-index: 2;
}
.ab-col-grip::after {
  content: ''; position: absolute; top: 20%; bottom: 20%; left: 4px; width: 1px;
  background: var(--ab-border); transition: background .15s;
}
.ab-col-grip:hover::after, .ab-col-grip.dragging::after { background: var(--color-accent); }

.tag, .ab-badge {
  display: inline-flex; align-items: center; gap: var(--space-1); font-size: var(--text-xs);
  letter-spacing: 0.02em; padding: 3px 10px; border-radius: 0; border: 1px solid transparent; font-family: 'JetBrains Mono', monospace;
}
.tag-accent { background: var(--color-accent-100); color: var(--color-accent-800); }
.tag-neutral { background: var(--color-neutral-100); color: var(--color-neutral-800); }
.tag-outline { border: 1px solid var(--color-accent); color: var(--color-accent); }
/* A tag has to be able to carry bad news too. Dashboard's mapping only ever
   needed "accent or neutral", but a suspended version and a rejected action
   are not neutral facts, and rendering them in the same grey as a draft is how
   a stopped agent reads as an idle one. Colour is stated for both grounds so
   the text stays legible in either theme. */
.tag-warn { background: color-mix(in srgb, #f59e0b 15%, transparent); color: #8a5a06; }
.tag-danger { background: color-mix(in srgb, #ef4444 12%, transparent); color: #9f2626; }
[data-theme="dark"] .tag-warn { color: #f3c46b; }
[data-theme="dark"] .tag-danger { color: #f5a2a2; }
.ab-badge-dot { width: 6px; height: 6px; border-radius: 99px; }
.ab-chip { display: inline-flex; align-items: center; gap: 6px; padding: var(--space-1) var(--space-2); border-radius: 0; font-size: var(--text-xs); border: 1px solid var(--color-divider); background: color-mix(in srgb, var(--color-text) 3%, transparent); color: var(--color-text); }
/* Some chips are pickers — a version, a connection type. The .ab-root button
   rule puts every button in the heading face, which a chip full of an
   identifier must opt back out of, and a picked chip has to *look* picked. */
button.ab-chip { font-family: var(--font-body); cursor: pointer; }
button.ab-chip.ab-mono { font-family: 'JetBrains Mono', monospace; }
.ab-chip.active { border-color: var(--color-accent); color: var(--color-accent); background: color-mix(in srgb, var(--color-accent) 10%, transparent); }
.ab-chip:disabled { opacity: .45; cursor: not-allowed; }

.seg { display: inline-flex; overflow: hidden; border: 1px solid var(--color-divider); border-radius: 0; }
.seg-opt { display: inline-flex; align-items: center; gap: 6px; padding: var(--space-2) var(--space-3); font-size: var(--text-sm); cursor: pointer; }
.seg-opt + .seg-opt { border-left: 1px solid var(--color-divider); }
.seg-opt:has(input:checked) { background: var(--color-accent); color: var(--color-bg); }

/* The segmented control paints its own selected state, so the native control it
   is built on must not also draw one beside the label. */
.radio input, .seg-opt input { position: absolute; opacity: 0; width: 0; height: 0; pointer-events: none; }
.seg-opt:has(input:focus-visible) { outline: 2px solid var(--color-accent); outline-offset: -2px; }

/* — tables — */
.table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
.table th {
  text-align: left; font-size: var(--text-2xs); letter-spacing: 0.08em; text-transform: uppercase;
  color: color-mix(in srgb, var(--color-text) 60%, transparent);
  padding: var(--space-2); border-bottom: 1px solid var(--color-divider);
}
.table td { padding: var(--space-2); border-bottom: 1px solid color-mix(in srgb, var(--color-text) 8%, transparent); }
.table tbody tr:hover { background: color-mix(in srgb, var(--color-text) 4%, transparent); }

.hr { height: 1px; border: 0; margin: var(--space-4) 0; background: var(--color-divider); }
.text-muted { color: color-mix(in srgb, var(--color-text) 55%, transparent); }

.ab-spin { animation: spin 1s linear infinite; }
.ab-toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: var(--color-surface); border: 1px solid var(--color-divider); padding: var(--space-3) var(--space-4); border-radius: 0; font-size: var(--text-sm); display: flex; align-items: center; gap: 9px; box-shadow: var(--shadow-lg); z-index: 200; animation: ab-toast-in .18s ease; max-width: min(560px, 90vw); }
@keyframes ab-toast-in { from { opacity: 0; transform: translateX(-50%) translateY(8px); } to { opacity: 1; transform: translateX(-50%) translateY(0); } }

.ab-error { font-size: var(--text-sm); color: #f87171; background: rgba(239, 68, 68, .08); border: 1px solid rgba(239, 68, 68, .3); padding: 9px 12px; border-radius: 0; margin: 0; line-height: 1.5; }

/* The canvas pans by dragging its background, so the background says so before
   anyone tries: grab while the graph can be moved, grabbing while it is. The
   node row keeps its own grab cursor, which is a different promise about a
   different thing (that box moves, not the view). */
.ab-canvas-wrap { position: relative; overflow: auto; background: var(--color-bg); cursor: grab; }
.ab-canvas-wrap.panning { cursor: grabbing; user-select: none; }
.ab-node { position: absolute; border-radius: 0; border: 1px solid var(--color-divider); background: var(--color-surface); box-shadow: var(--shadow-md); user-select: none; }
.ab-node-row { display: flex; align-items: center; gap: 8px; height: 100%; padding: 0 10px; cursor: grab; }
.ab-node.selected { border-color: var(--color-accent); box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-accent) 40%, transparent); }
/* Three marks a node can carry at once, drawn so they never cancel each other:
   a run is the loudest, a problem outranks a chat edit, and a chat edit fades. */
.ab-node.active-run { border-color: var(--color-accent); animation: agRing 1.2s ease-out infinite; }
.ab-node.changed-flag { border-color: var(--color-accent-2); box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-accent-2) 35%, transparent); }
.ab-node.error-flag { border-color: rgba(239, 68, 68, .55); }
.ab-node-start {
  position: absolute; top: -9px; left: 9px; z-index: 2;
  padding: 1px var(--space-2); font-family: 'JetBrains Mono', monospace; font-size: var(--text-2xs); letter-spacing: .1em;
  background: var(--color-accent); color: var(--color-bg);
}
/* The handles an edge is dragged between. Big enough to hit, square like
   everything else in this system. */
.ab-port {
  position: absolute; width: 12px; height: 12px; z-index: 3;
  background: var(--color-bg); border: 2px solid var(--color-accent);
  cursor: crosshair; transition: transform .12s, background .12s;
}
.ab-port:hover { transform: scale(1.25); background: var(--color-accent); }

.ab-tabs { display: flex; gap: 4px; padding: 0 22px; border-bottom: 1px solid var(--color-divider); overflow-x: auto; }
.ab-tab { padding: var(--space-3); font-size: var(--text-sm); color: color-mix(in srgb, var(--color-text) 60%, transparent); border: none; border-bottom: 2px solid transparent; display: flex; align-items: center; gap: 6px; background: none; white-space: nowrap; font-family: var(--font-heading); font-weight: 600; cursor: pointer; }
.ab-tab.active { color: var(--color-text); border-bottom-color: var(--color-accent); }

.ab-switch { width: 38px; height: 20px; border-radius: 0; background: var(--color-surface); border: 1px solid var(--color-divider); position: relative; cursor: pointer; flex-shrink: 0; }
.ab-switch.on { background: color-mix(in srgb, var(--color-accent) 35%, transparent); border-color: var(--color-accent); }
.ab-switch-knob { position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; border-radius: 0; background: color-mix(in srgb, var(--color-text) 50%, transparent); transition: left .15s; }
.ab-switch.on .ab-switch-knob { left: 20px; background: var(--color-accent); }

@media (max-width: 900px) { .ab-hide-mobile { display: none !important; } }
/* The header carries the page's actions now, so the things that are not about
   this page give way first when the row runs out of room. */
@media (max-width: 1080px) { .ab-hide-narrow { display: none !important; } }

a { color: var(--color-accent); text-decoration-color: color-mix(in srgb, var(--color-accent) 40%, transparent); }
a:hover { color: var(--color-accent-700); }

.ab-label { display: block; font-size: var(--text-xs); margin-bottom: 5px; color: color-mix(in srgb, var(--color-text) 70%, transparent); }
.ab-empty { text-align: center; padding: var(--space-10) var(--space-6); font-size: var(--text-sm); color: color-mix(in srgb, var(--color-text) 58%, transparent); }

.seg-opt:not(:has(input:checked)):hover { background: color-mix(in srgb, var(--color-text) 7%, transparent); }

/* ── One number and what it counts. The metric strip on Sandbox, Evaluation,
      Operations and the approval dossier. ── */
.ab-metric {
  flex: 1 1 116px; min-width: 116px; padding: 12px 14px;
  border: 1px solid var(--color-divider); background: var(--color-bg);
}
.ab-metric .val { font-family: var(--font-heading); font-weight: var(--font-heading-weight); font-size: var(--text-2xl); line-height: 1; }
.ab-metric .lbl {
  margin-top: var(--space-1); font-size: var(--text-2xs); letter-spacing: .13em; text-transform: uppercase;
  color: color-mix(in srgb, var(--color-text) 52%, transparent);
}

/* ── The three list rows: a trace line, a graded case, a version. Same frame,
      different contents — so they line up when two of them are on one screen. ── */
.ab-trace-item, .ab-eval-item, .ab-version-item {
  display: flex; gap: 12px; align-items: flex-start;
  padding: 11px 13px; border: 1px solid var(--color-divider); background: transparent;
}
.ab-version-item { align-items: center; }
.ab-trace-item { animation: agUp .3s ease both; }

/* ── Chat bubbles: the agent's side is outlined, the caller's is filled, which
      is the same pair the Designer interview and the graph chat draw. ── */
.ab-chat-bubble-user, .ab-chat-bubble-agent {
  max-width: 80%; padding: var(--space-3) var(--space-4); font-size: var(--text-md); line-height: 1.6;
  white-space: pre-wrap; word-break: break-word; border-radius: 0;
}
.ab-chat-bubble-user { align-self: flex-end; background: var(--color-accent); color: var(--color-bg); }
.ab-chat-bubble-agent { align-self: flex-start; border: 1px solid var(--color-divider); color: var(--color-text); }

/* ── Conversation rows in the chat sidebar ──
      Rename and remove stay hidden until the row is pointed at: a list of
      twenty conversations with forty buttons on it reads as a control panel
      rather than as a history. The :focus-within rule is what keeps them
      reachable without a mouse — hover alone would make them
      keyboard-invisible. */
.ab-convo-row:hover { background: color-mix(in srgb, var(--color-text) 4%, transparent) !important; }
.ab-convo-actions { opacity: 0; transition: opacity .12s; }
.ab-convo-row:hover .ab-convo-actions, .ab-convo-row:focus-within .ab-convo-actions { opacity: 1; }
/* Coarse pointers get no hover at all, so the actions are simply always there. */
@media (hover: none) { .ab-convo-actions { opacity: 1; } }

/* The shell every signed-in screen renders inside: one viewport, split once.
   Height is fixed and overflow hidden here so the scroll belongs to the page
   area (.ab-page) rather than to the document — which is what lets the sidebar
   and the header stay put without either measuring the other. */
.ab-shell {
  display: flex; height: 100vh; overflow: hidden;
  background: var(--color-bg); color: var(--color-text); font-family: var(--font-body);
}

/* ── The page frame ──
      Every screen inside the AppLayout shell is this same pair, Workspace and
      Enterprise alike, so no page re-derives the shell's padding or guesses
      where its own scroll lives. The shell is height:100vh with overflow
      hidden, so min-height:0 is not decoration: without it a flex child
      refuses to shrink below its content and the page never scrolls. ── */
.ab-page { flex: 1; min-height: 0; overflow-y: auto; }
.ab-page-inner { width: 100%; max-width: var(--page-max); padding: var(--page-pad-top) var(--page-pad-x) var(--page-pad-bottom); }
/* Reading columns — a form or a queue of dense rows is worse at full width.
   A matrix or a wide table is worse *narrow*, hence the third width. */
.ab-page-inner.narrow { max-width: var(--page-max-narrow); }
.ab-page-inner.wide { max-width: var(--page-max-wide); }

/* ── The page head ──
   A page no longer opens with its own name (ADR-0057 s4): the shell header
   already prints it, and printing it twice, forty pixels apart and at two
   sizes, was the loudest evidence that this app had no frame. What is left
   here is the fallback for the four screens that render outside the shell —
   Landing, sign-in, register — where there is no header to hand a title to. */
.ab-page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: var(--space-5); margin-bottom: var(--space-5); }
.ab-page-head h1 { margin: 0; }
.ab-page-lede { margin: 0; max-width: 68ch; font-size: var(--text-sm); line-height: 1.55; color: color-mix(in srgb, var(--color-text) 62%, transparent); }
.ab-page-actions { display: flex; gap: var(--space-2); flex-shrink: 0; }

/* ── The shell header ──
   One row, on every signed-in screen: where you are, what this page is called,
   and the actions that belong to *this page* — hoisted out of the page body by
   PageHead so the name and the buttons that act on it stay together instead
   of the name being in the bar and the buttons 90px below it. */
.ab-shell-head {
  display: flex; align-items: center; gap: var(--space-3);
  padding: 0 var(--page-pad-x); height: 52px; flex: none; z-index: 5;
  border-bottom: 1px solid var(--color-divider);
  background: color-mix(in srgb, var(--color-bg) 88%, transparent);
  backdrop-filter: blur(8px);
}
.ab-shell-head .crumb {
  margin: 0; font-size: var(--text-2xs); letter-spacing: .14em; text-transform: uppercase;
  color: color-mix(in srgb, var(--color-text) 45%, transparent);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: none; max-width: 34%;
}
/* A slash, not a second line: the crumb and the page's name are one path, and
   stacking them made the header 68px tall to say what fits on one row. */
.ab-shell-head .crumb::after { content: "/"; margin-left: var(--space-3); opacity: .5; }
.ab-shell-head h1 {
  margin: 0; font-size: var(--text-xl); line-height: 1.2; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ab-head-actions { display: flex; align-items: center; gap: var(--space-2); flex: none; margin-left: auto; }
.ab-head-tools { display: flex; align-items: center; gap: var(--space-2); flex: none; }

/* ── Settings: one mark, one menu ──
   Theme and language sat permanently in the header — two controls about the
   app, on every page, beside the name of the page, touched about twice a
   session. They are preferences, so they live where preferences live: behind
   one gear in the sidebar's user footer, next to the account they belong to.

   The menu opens upward because the footer is at the foot of the rail, and to
   the right of it because there is nothing to the left but the window edge. */
.ab-settings { position: relative; flex: none; }
.ab-settings-menu {
  position: absolute; bottom: calc(100% + var(--space-2)); z-index: 50;
  display: flex; flex-direction: column; gap: var(--space-1);
  min-width: 178px; padding: var(--space-3);
  background: var(--color-bg); border: 1px solid var(--color-divider);
  box-shadow: var(--shadow-lg);
  animation: agUp .16s ease both;
}
.ab-settings-menu[data-align="left"] { left: 0; }
.ab-settings-menu[data-align="right"] { right: 0; }
/* Collapsed, the rail is 64px wide and the menu is 178px: anchoring it to the
   gear's own left or right edge puts most of it off the left of the window,
   which is exactly what it did. It opens *beside* the gear instead, into the
   page, with its foot on the gear's foot. */
.ab-settings-menu[data-compact="1"] {
  left: calc(100% + var(--space-2));
  right: auto;
  bottom: 0;
}
.ab-settings-label {
  margin: var(--space-1) 0 0; font-size: var(--text-2xs); letter-spacing: .13em;
  text-transform: uppercase; color: color-mix(in srgb, var(--color-text) 45%, transparent);
}
.ab-settings-label:first-child { margin-top: 0; }
.ab-settings-menu .seg { width: 100%; }
.ab-settings-opt {
  flex: 1; display: inline-flex; align-items: center; justify-content: center; gap: var(--space-1);
  padding: var(--space-1) var(--space-2); border: 0; background: transparent;
  font: inherit; font-family: var(--font-heading); font-size: var(--text-xs);
  color: color-mix(in srgb, var(--color-text) 68%, transparent); cursor: pointer;
}
.ab-settings-opt + .ab-settings-opt { border-left: 1px solid var(--color-divider); }
.ab-settings-opt[data-on="1"] { background: var(--color-accent); color: var(--color-bg); }
.ab-settings-opt:not([data-on="1"]):hover { background: color-mix(in srgb, var(--color-text) 8%, transparent); }

/* ── A section of a page ──
   The one rhythm: rows sit --gap-stack apart inside a section, sections sit
   --gap-section apart. No page adds a margin of its own between two of
   these, which is what stopped every screen from spacing itself differently. */
.ab-section { display: flex; flex-direction: column; gap: var(--gap-stack); }
/* Not ".ab-section + .ab-section": the first group on a page is often a bare
   grid rather than a Section, and a section that follows one still has to
   stand clear of it. */
.ab-section:not(:first-child) { margin-top: var(--gap-section); }
.ab-section-head {
  display: flex; align-items: baseline; gap: var(--space-3);
  min-height: 26px; margin-bottom: var(--space-1);
}
.ab-section-head h2 { margin: 0; flex: none; max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* The rule is what separates two sections now, in place of 40px of air —
   it costs one pixel and says the same thing. */
.ab-section-head::after { content: ""; flex: 1; height: 1px; background: var(--color-divider); }
.ab-section-head .ab-section-tools { display: flex; align-items: center; gap: var(--space-2); flex: none; order: 3; }
.ab-section-lede {
  margin: 0; font-size: var(--text-sm); line-height: 1.55;
  color: color-mix(in srgb, var(--color-text) 60%, transparent);
}

/* A row of controls that act on what is under it. */
.ab-toolbar { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
.ab-toolbar-end { margin-left: auto; }

/* ── An inline note ──
   Something the screen has to say about the thing on it, and nothing more:
   a hairline, a mark, one line. The colour is carried by a 2px rule down the
   left rather than by flooding the row, because a filled amber band reads as
   an alarm whether or not it is one — which is how a note that *is* clamped to
   one line still gets read as "this always shows in full".
   As a <button> it also carries its own affordance: a caret that says there is
   more, and a hit area that says it can be clicked. */
.ab-note {
  display: flex; align-items: flex-start; gap: var(--space-2); width: 100%;
  padding: var(--space-2) var(--space-3); margin: 0;
  font: inherit; font-size: var(--text-sm); line-height: 1.5; text-align: left;
  color: color-mix(in srgb, var(--color-text) 78%, transparent);
  background: color-mix(in srgb, var(--color-text) 4%, transparent);
  border: 0; border-left: 2px solid var(--color-divider);
}
button.ab-note { cursor: pointer; transition: background .15s, border-color .15s; }
button.ab-note:hover { background: color-mix(in srgb, var(--color-text) 7%, transparent); }
.ab-note-warn { border-left-color: var(--ab-warn); background: color-mix(in srgb, #f59e0b 7%, transparent); }
.ab-note-danger { border-left-color: var(--ab-destructive); background: color-mix(in srgb, #ef4444 7%, transparent); }
.ab-note .ab-note-icon { flex: none; margin-top: 3px; }
.ab-note-warn .ab-note-icon { color: var(--ab-warn); }
.ab-note-danger .ab-note-icon { color: var(--ab-destructive); }
.ab-note-body { flex: 1; min-width: 0; }
.ab-note-caret { flex: none; margin-top: 2px; opacity: .55; transition: transform .15s, opacity .15s; }
button.ab-note[aria-expanded="true"] .ab-note-caret { transform: rotate(180deg); }
button.ab-note:hover .ab-note-caret { opacity: 1; }

/* ── Folded to the mark alone ──
   Closed, the note is its icon and a caret and nothing else: the sentence is
   not shortened, it is *not shown*. A full-width bar holding one line of amber
   still read as a standing alarm — the owner read a one-line clamp as "always
   shows in full" — so the closed state stops being a bar at all and becomes a
   mark you can open, the size of the badge beside it.

   The affordance has to survive losing the text, so the mark keeps every signal
   a control has: a full border rather than one edge, a hover and a focus ring,
   a caret that turns, and the whole refusal on the title. */
button.ab-note.folded {
  /* align-self as well as width: the note sits in a .ab-section, which is a
     flex column, and a stretched child is full width whatever its width says.
     Without this the mark is a 1132px bar containing one glyph — which is the
     same page-wide band, just emptier. */
  width: auto; align-self: flex-start;
  align-items: center; gap: var(--space-1);
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--color-divider); border-left-width: 2px;
}
button.ab-note.folded .ab-note-body { display: none; }
button.ab-note.folded .ab-note-icon { margin-top: 0; }
button.ab-note.folded .ab-note-caret { margin-top: 0; opacity: .75; }
button.ab-note-warn.folded { border-color: color-mix(in srgb, #f59e0b 45%, transparent); border-left-color: var(--ab-warn); }
button.ab-note-danger.folded { border-color: color-mix(in srgb, #ef4444 45%, transparent); border-left-color: var(--ab-destructive); }
button.ab-note.folded:hover { background: color-mix(in srgb, #f59e0b 16%, transparent); }

/* ── The composer on the home stage ──
   The canvas bar is one line, so it centres everything on that line. This one
   grows, and a row-shaped bar with a growing middle leaves its icon stranded
   at the top-left and its send button at the bottom-right with a void between
   them. So it is two rows inside one border: the text, then the controls under
   it — the shape every current AI composer settled on for the same reason. */
/* One row: mark, what you are typing, send. It was a column — the text on one
   row and the controls on a row of their own underneath — which spent a whole
   line of height on a mark and a button that fit beside the text perfectly
   well, and left an empty rule across the box whenever the text was short,
   which is almost always.
   Aligned to flex-end rather than centre so that when the box does grow, the
   mark and the button settle beside the *last* line instead of drifting to the
   middle of a paragraph. */
.ab-compose {
  display: flex; align-items: flex-end; gap: var(--space-2);
  width: 100%; padding: var(--space-2) var(--space-3);
  background: var(--color-bg); border: 1px solid var(--color-divider);
  box-shadow: var(--shadow-lg); transition: border-color .15s;
}
.ab-compose:focus-within { border-color: var(--color-accent); }
/* One line at rest, grown to fit what is in it (the JS sets the height from
   scrollHeight). rows={2} was the old bar's way of hinting "you can write more
   than a sentence here", and it paid for the hint with 24px of empty box on
   every visit; growing says the same thing only when it is true. */
.ab-compose textarea {
  flex: 1; min-width: 0; min-height: 26px; max-height: 180px; padding: 0; resize: none;
  border: 0; outline: none; background: transparent; color: var(--color-text);
  font: inherit; font-size: var(--text-md); line-height: 1.6; overflow-y: auto;
}
/* The mark keeps the row's left end and the button its right; both hold the
   textarea's own line height so a one-line box is exactly one line tall. */
.ab-compose > .ab-compose-mark { flex: none; display: flex; align-items: center; height: 26px; color: var(--ab-primary); }

/* The mark that carries an explanation (components/ui.jsx :: Hint). Sized to
   sit beside a heading of any size without pushing it around, and only
   coloured on hover so a page full of them still reads as headings. */
.ab-hint {
  display: inline-flex; align-items: center; vertical-align: middle;
  margin-left: 7px; cursor: help;
  color: color-mix(in srgb, var(--color-text) 34%, transparent);
  transition: color .15s;
}
.ab-hint:hover { color: var(--color-accent); }

/* A stack of cards, at the one gap the whole app stacks things at. */
.ab-stack { display: flex; flex-direction: column; gap: 12px; }
.ab-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

/* Machine-readable payload shown verbatim — tool arguments, a spec, a diff.
   Square and on the surface tone, like every other panel here. */
.ab-code {
  font-family: 'JetBrains Mono', monospace; font-size: var(--text-xs); line-height: 1.5;
  background: color-mix(in srgb, var(--color-text) 5%, transparent);
  border: 1px solid var(--color-divider); border-radius: 0;
  padding: var(--space-3); margin: 0; overflow-x: auto; white-space: pre;
}

/* A card that is calling something out rather than just holding it. */
.card-warn { border-color: color-mix(in srgb, #f59e0b 45%, transparent); background: color-mix(in srgb, #f59e0b 6%, transparent); }
.card-ok { border-color: color-mix(in srgb, var(--color-accent) 55%, transparent); background: color-mix(in srgb, var(--color-accent) 7%, transparent); }

.ab-scrollbar { scrollbar-width: thin; scrollbar-color: color-mix(in srgb, var(--color-text) 25%, transparent) transparent; }
.ab-scrollbar::-webkit-scrollbar { width: 9px; height: 9px; }
.ab-scrollbar::-webkit-scrollbar-thumb { background: color-mix(in srgb, var(--color-text) 22%, transparent); }
.ab-scrollbar::-webkit-scrollbar-track { background: transparent; }

/* ── Dialog ── */
.ab-dialog-backdrop {
  position: fixed; inset: 0; z-index: 300; display: grid; place-items: center; padding: 24px;
  background: color-mix(in srgb, var(--color-neutral-900) 55%, transparent); backdrop-filter: blur(2px);
}
.ab-dialog {
  width: min(460px, 100%); display: flex; flex-direction: column; gap: var(--space-3);
  padding: var(--space-6); border-radius: 0;
  background: var(--color-bg); border: 1px solid var(--color-divider); box-shadow: var(--shadow-lg);
}

.ab-root :focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px; }

/* ── Add MCP server: the connector gallery and its connect screens ──
   Cards rather than a list because the thing being chosen is a *product* the
   reader recognises by name and mark, and a row of text makes them read where
   they would otherwise glance. */
.mcp-panel { display: flex; flex-direction: column; gap: 12px; padding: 14px; border: 1px solid var(--color-divider); background: var(--color-surface); }
.mcp-tabs { display: flex; gap: 2px; padding: 3px; background: color-mix(in srgb, var(--color-text) 7%, transparent); }
.mcp-tab { border: 0; background: transparent; color: color-mix(in srgb, var(--color-text) 65%, transparent); font: inherit; font-size: var(--text-xs); padding: var(--space-1) var(--space-3); cursor: pointer; }
.mcp-tab.active { background: var(--color-surface); color: var(--color-text); font-weight: 500; }
.mcp-section { font-size: var(--text-xs); font-weight: 500; margin: 2px 0 0; }
.mcp-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(132px, 1fr)); gap: 8px; }
.mcp-card { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; padding: 12px; text-align: left; cursor: pointer; border: 1px solid var(--color-divider); background: var(--color-bg); color: var(--color-text); font: inherit; transition: border-color .12s ease; }
.mcp-card:hover { border-color: var(--color-accent); }
.mcp-card-icon { display: flex; }
.mcp-card-name { font-size: var(--text-sm); font-weight: 500; line-height: 1.3; }
.mcp-card-sub { font-size: var(--text-xs); color: color-mix(in srgb, var(--color-text) 55%, transparent); }

/* The picker itself, centred by the .ab-dialog-backdrop above. No backticks
   in this file's comments: the whole stylesheet is one template literal, so a
   backtick closes it and the rest of the line runs as JavaScript. Wide enough that the
   connector grid keeps three columns — the width the cards were drawn for —
   and capped in height so a long third-party list scrolls rather than pushing
   the buttons off screen. */
.mcp-dialog { width: min(560px, 94vw); max-height: min(78vh, 720px); overflow-y: auto; }
.mcp-dialog .mcp-panel { border: 1px solid var(--color-divider); background: var(--color-bg); box-shadow: var(--shadow-lg); padding: 18px; }

/* The divider between two credential paths that are genuinely alternatives —
   a rule with the word in it, so neither reads as a step after the other. */
.mcp-or { display: flex; align-items: center; gap: var(--space-3); color: color-mix(in srgb, var(--color-text) 45%, transparent); font-size: var(--text-xs); }
.mcp-or::before, .mcp-or::after { content: ""; flex: 1; height: 1px; background: var(--color-divider); }

/* The MCP node panel: the shelf of what this step is wired to. Marks and a
   door, laid out as a row so "what is attached" is one glance rather than a
   field label to read. */
.mcp-attached { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.mcp-chip { display: inline-flex; align-items: center; gap: 7px; padding: 7px 11px; border: 1px solid var(--color-divider); background: var(--color-bg); color: var(--color-text); font: inherit; cursor: pointer; transition: border-color .12s ease; }
.mcp-chip:hover { border-color: var(--color-accent); }
.mcp-chip.active { border-color: var(--color-accent); box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-accent) 25%, transparent); }
/* The attached connector is the panel's headline, so it is drawn at headline
   size — the size the reader's eye should land on first. */
.mcp-chip-lg { width: 100%; justify-content: flex-start; gap: var(--space-3); padding: var(--space-3) var(--space-4); font-size: var(--text-sm); }
.mcp-node-settings { display: flex; flex-direction: column; gap: 12px; padding: 12px; border-left: 2px solid var(--color-accent); background: color-mix(in srgb, var(--color-text) 5%, transparent); }

/* ── Click-to-configure rows ──
   The inspector shows a field's *state* in one line; the full editor opens in
   a centred dialog. Deep config inline is a wall the reader scrolls past —
   what belongs on the panel is what is set, not every control that sets it. */
.ab-field-row { display: flex; align-items: center; gap: 8px; width: 100%; padding: 8px 10px; border: 1px solid var(--color-divider); background: var(--color-surface); color: var(--color-text); font: inherit; font-size: var(--text-xs); cursor: pointer; text-align: left; transition: border-color .12s ease; }
.ab-field-row:hover:not(:disabled) { border-color: var(--color-accent); }
.ab-field-row:disabled { opacity: .6; cursor: default; }
.ab-field-row .frl { flex-shrink: 0; font-weight: 500; }
.ab-field-row .frs { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; color: var(--ab-muted); }
.ab-field-row .frs.warn { color: var(--ab-warn); }

/* The add-card wears the same frame as its siblings, dashed so the eye still
   sorts it as "the other one" without it leaving the row. */
.mcp-card-add { border-style: dashed; color: color-mix(in srgb, var(--color-text) 65%, transparent); }
.mcp-card-add:hover { color: var(--color-text); }

.mcp-connect { align-items: stretch; text-align: center; }
.mcp-connect-icon { display: flex; align-self: center; padding: 12px; border: 1px solid var(--color-divider); }
.mcp-connect-title { font-size: var(--text-md); font-weight: 500; margin: 0; }
/* What it can do, listed before anything is asked for. */
.mcp-abilities { display: flex; flex-direction: column; gap: 5px; padding: 11px; text-align: left; background: color-mix(in srgb, var(--color-text) 6%, transparent); }
.mcp-ability { display: flex; align-items: center; gap: var(--space-2); font-size: var(--text-xs); }
.mcp-secret { position: relative; display: flex; align-items: center; }
.mcp-secret .input { padding-left: 30px; padding-right: 34px; width: 100%; }
.mcp-secret-icon { position: absolute; left: 9px; opacity: .55; pointer-events: none; }
.mcp-secret-eye { position: absolute; right: 8px; border: 0; background: transparent; color: inherit; opacity: .6; cursor: pointer; display: flex; padding: 2px; }
.mcp-secret-eye:hover { opacity: 1; }

/* ── Floating canvas panels ──
   Agent Builder's answer to a cramped builder: the canvas is the room and
   everything else stands in it. Palette, inspector and chat float over the
   graph instead of carving columns out of it, so the graph gets the whole
   viewport and each panel scrolls by itself. */
.ab-float { position: absolute; z-index: 10; display: flex; background: var(--color-bg); border: 1px solid var(--color-divider); box-shadow: var(--shadow-lg); }
.ab-float-palette { left: 12px; top: 12px; max-height: calc(100% - 24px); }
.ab-float-inspector { right: 12px; top: 12px; bottom: 12px; }
.ab-palette-row { display: flex; align-items: center; gap: var(--space-2); width: 100%; padding: var(--space-2); border: 0; background: transparent; color: var(--color-text); font: inherit; font-size: var(--text-sm); text-align: left; cursor: grab; }
.ab-palette-row:hover { background: color-mix(in srgb, var(--color-text) 7%, transparent); }
/* The chat transcript, and the bar you talk to — bottom-centre over the graph,
   where a command line for the canvas belongs. */
.ab-chatlog { display: flex; flex-direction: column; gap: 8px; padding: 10px 14px; background: var(--color-bg); border: 1px solid var(--color-divider); box-shadow: var(--shadow-lg); overflow-y: auto; }
.ab-chatbar { display: flex; align-items: center; gap: 9px; padding: 6px 8px 6px 13px; background: var(--color-bg); border: 1px solid var(--color-divider); box-shadow: var(--shadow-lg); }
.ab-chatbar input, .ab-chatbar textarea { flex: 1; min-width: 0; border: 0; outline: none; background: transparent; color: var(--color-text); font: inherit; font-size: var(--text-md); resize: none; }
/* Tabs when they ride inside the slim header instead of on a row of their own. */
.ab-tabs-inline { display: flex; gap: 2px; overflow-x: auto; }
.ab-tabs-inline .ab-tab { padding: var(--space-2) var(--space-3); font-size: var(--text-sm); }

/* ── Pane resizers ──
   Invisible until touched: a divider that always shows would add a line beside
   every border the layout already draws. Wide enough to hit (5px + the hover
   forgiveness of the cursor), coloured only while it is being used. */
.pane-resizer { flex-shrink: 0; background: transparent; transition: background .12s ease; z-index: 5; }
.pane-resizer-x { width: 5px; cursor: col-resize; }
.pane-resizer-y { height: 5px; cursor: row-resize; }
.pane-resizer:hover, .pane-resizer.active { background: color-mix(in srgb, var(--color-accent) 35%, transparent); }
`;
