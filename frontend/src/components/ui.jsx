import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CheckCircle2, AlertCircle, Loader2, X, Sun, Moon, Info, ChevronDown, AlertTriangle, Settings } from "lucide-react";
import { LANGUAGES, useLang } from "../i18n";

/* ============================================================
   Explanations live one hover away, not on the page.

   Every screen here used to open with a paragraph explaining itself, and each
   panel with another. Read once, they are useful; read on every visit, they
   are furniture between the person and the numbers they came for. So the
   words are kept — nothing is deleted — and moved behind `Hint`: a small mark
   beside the heading that says "there is more to know here" and gives it up
   on hover.

   `title` rather than a custom popover on purpose: it is the browser's own,
   it survives a page that scrolls or a table that clips, and screen readers
   already announce it (the text is repeated as `aria-label` for the ones that
   do not). The cost is that it does not appear on keyboard focus alone —
   accepted, because no explanation here is load-bearing: the labels, numbers
   and refusals are all still on the page in words.
   ============================================================ */

/** The mark that carries a heading's explanation. Renders nothing without one. */
export function Hint({ text }) {
  if (!text) return null;
  return (
    <span className="ab-hint" role="note" title={text} aria-label={text}>
      <Info size={13} />
    </span>
  );
}

/* ============================================================
   The frame every page obeys (ADR-0057).

   Three primitives and one rule each:

     PageShell   — the page's margins and its reading width. A page never
                   states its own padding, so "the app feels tight" is one
                   edit to --page-pad-* and not thirty-seven.
     PageHead    — the page's name, its explanation, and the buttons that act
                   on the *whole page*. It renders nothing where it stands:
                   the name is handed up to the shell header, which was
                   already printing it, and the buttons are portalled up
                   beside it. Before this, every screen printed its title
                   twice — once at 21px in the header and again at 36px forty
                   pixels below — which was the plainest evidence that this
                   app had no frame at all.
     Section     — a group inside a page, and the only thing allowed to put
                   vertical space between two groups.

   What a page may still decide for itself: its reading width, what goes in
   its sections, and its own instruments. What it may not: margins, the
   distance between two sections, heading sizes, or where its title lives.
   ============================================================ */

const PageChromeContext = createContext(null);

/** Handed down by AppLayout. Pages never touch it directly — PageHead does. */
export function PageChromeProvider({ setHead, actionSlot, children }) {
  const value = useMemo(() => ({ setHead, actionSlot }), [setHead, actionSlot]);
  return <PageChromeContext.Provider value={value}>{children}</PageChromeContext.Provider>;
}

/** The page's margins and reading width.
 *
 *  `width`: "default" (1180px — lists, dashboards), "narrow" (860px — forms
 *  and queues of dense rows, which are worse at full width), "wide" (1440px —
 *  a matrix or a wide table, which are worse narrow). */
export function PageShell({ width = "default", className = "", children, ...rest }) {
  const w = width === "narrow" ? " narrow" : width === "wide" ? " wide" : "";
  return (
    <div className="ab-page ab-scrollbar">
      <main className={`ab-page-inner${w}${className ? " " + className : ""}`} {...rest}>
        {children}
      </main>
    </div>
  );
}

/** The page's name, its explanation, and the actions that act on all of it.
 *
 *  Renders nothing where it is written when the page is inside the shell: the
 *  title goes to the header that was already showing it, and `children` are
 *  portalled to the header's action slot so that the name and the buttons
 *  that act on it stay in one place. Outside the shell — the three entrance
 *  screens — there is no header to hand anything to, so it falls back to the
 *  block it used to be. */
export function PageHead({ title, hint, children }) {
  const chrome = useContext(PageChromeContext);
  const setHead = chrome?.setHead;

  /* Strings only, never elements: `children` is a fresh element on every
     render, so registering it as state would set state on every render
     forever. The buttons travel by portal instead, which needs no state. */
  useEffect(() => {
    if (!setHead) return undefined;
    setHead({ title, hint });
    return () => setHead(null);
  }, [setHead, title, hint]);

  if (!chrome) {
    return (
      <div className="ab-page-head">
        <h1>{title}<Hint text={hint} /></h1>
        {children ? <div className="ab-page-actions">{children}</div> : null}
      </div>
    );
  }
  return chrome.actionSlot && children ? createPortal(children, chrome.actionSlot) : null;
}

/** A group inside a page — and the only thing that puts space between groups.
 *
 *  The head is a label and a hairline rather than 40px of air: the rule costs
 *  one pixel and says the same thing, which is the whole trade this frame is
 *  making. `actions` are the controls that act on this group only; anything
 *  that acts on the whole page belongs to PageHead. */
export function Section({ title, hint, lede, actions, children, ...rest }) {
  return (
    <section className="ab-section" {...rest}>
      {(title || actions) && (
        <div className="ab-section-head">
          {title ? <h2>{title}<Hint text={hint} /></h2> : null}
          {actions ? <div className="ab-section-tools">{actions}</div> : null}
        </div>
      )}
      {lede ? <p className="ab-section-lede">{lede}</p> : null}
      {children}
    </section>
  );
}

/** Something the screen has to say about the thing on it.
 *
 *  `fold` closes it down to **its mark and nothing else** — no line of text,
 *  no band across the page. Not one word is dropped: the whole sentence is one
 *  click away, and it is on `title` and `aria-label` for anything that never
 *  clicks. The first version of this showed a mark plus one clamped line, and
 *  that still read as a standing alarm; a filled amber bar the width of the
 *  page says "something is wrong here, now" whatever is written in it, and
 *  what it is actually reporting is a fact you can look up when you want it.
 *
 *  A mark on its own has to keep saying it is a control, so it keeps every
 *  signal one has: a full border, a hover, a focus ring, and a caret that
 *  turns when it opens. */
export function Note({ tone = "", fold = false, children, text, label }) {
  const body = text ?? children;
  const [open, setOpen] = useState(false);
  /* A new refusal is a new thing to read, so it arrives folded even if the
     last one was left open — otherwise "open" is sticky for the session and
     the fold stops being a fold. */
  useEffect(() => { setOpen(false); }, [text]);
  if (!body) return null;

  const icon = tone === "warn" || tone === "danger"
    ? <AlertTriangle size={14} className="ab-note-icon" aria-hidden="true" />
    : null;

  if (!fold) {
    return (
      <p className={`ab-note${tone ? ` ab-note-${tone}` : ""}`}>
        {icon}<span className="ab-note-body">{body}</span>
      </p>
    );
  }

  const full = typeof body === "string" ? body : undefined;
  return (
    <button
      type="button"
      className={`ab-note${tone ? ` ab-note-${tone}` : ""}${open ? "" : " folded"}`}
      aria-expanded={open}
      title={full}
      /* Closed there is no visible text at all, so the button has to carry its
         own name — otherwise the control announces as nothing but a glyph. */
      aria-label={label || full}
      onClick={() => setOpen((was) => !was)}
    >
      {icon}
      <span className="ab-note-body">{body}</span>
      <ChevronDown size={13} className="ab-note-caret" aria-hidden="true" />
    </button>
  );
}

/* `TopNav` used to live here: the header a page drew for itself when it
   rendered outside `AppLayout`. Nothing does any more — the last three
   holdouts (Enterprise, Knowledge Bases, Accounts) are shell children — so
   the component and the `.ab-navbar` / `.ab-logo` rules behind it are gone.
   A page that needs its own action puts it in `.ab-page-head`. */

/* ============================================================
   The version lifecycle, as a badge.

   These are `VersionStatus` on the backend, all nine of them, and the labels are
   the stages a creator actually moves through. A status this map does not know
   would render as itself rather than as "Draft" — a wrong label is worse than an
   unfamiliar one on a screen whose whole job is to say where you are.
   ============================================================ */
const VERSION_STATUS = {
  spec_pending_confirmation: { c: "#a78bfa", label: "Spec to review" },
  spec_confirmed: { c: "#a78bfa", label: "Spec confirmed" },
  workflow_pending_confirmation: { c: "#f59e0b", label: "Editing workflow" },
  workflow_confirmed: { c: "#38bdf8", label: "Workflow confirmed" },
  draft: { c: "#6b7fa3", label: "Draft" },
  sandbox_passed: { c: "#38bdf8", label: "Sandbox passed" },
  pending_approval: { c: "#f59e0b", label: "Pending review" },
  approved: { c: "#00d4a8", label: "Approved" },
  rejected: { c: "#ef4444", label: "Changes requested" },
};

function tint(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

export function StatusBadge({ status }) {
  const { t } = useLang();
  if (!status) return null;
  const known = VERSION_STATUS[status];
  const color = known ? known.c : "#6b7fa3";
  return (
    <span className="tag" style={{ color, background: tint(color, 0.1), borderColor: tint(color, 0.25), border: `1px solid ${tint(color, 0.25)}` }}>
      <span style={{ width: 6, height: 6, borderRadius: 99, background: color }} />
      {known ? t(known.label) : status}
    </span>
  );
}

export function DeployedBadge({ deployed }) {
  const { t } = useLang();
  const color = deployed ? "#00d4a8" : "#6b7fa3";
  return (
    <span className="tag" style={{ color, background: tint(color, 0.1), borderColor: tint(color, 0.25), border: `1px solid ${tint(color, 0.25)}` }}>
      <span style={{ width: 6, height: 6, borderRadius: 99, background: color }} />
      {deployed ? t("Published") : t("Not published")}
    </span>
  );
}

/** A toast that can carry bad news as well as good — an API refusal is the most
 *  common thing this app has to say, and saying it in the "success" colour is
 *  how a failure gets read as a success. */
export function Toast({ toast, onClose }) {
  if (!toast) return null;
  const bad = toast.kind === "error";
  return (
    <div className="ab-toast" style={bad ? { borderColor: "rgba(239,68,68,.3)" } : undefined}>
      {bad ? <AlertCircle size={15} style={{ color: "#f87171", flexShrink: 0 }} />
           : <CheckCircle2 size={15} style={{ color: "var(--color-accent)", flexShrink: 0 }} />}
      <span style={{ flex: 1 }}>{toast.message}</span>
      <button className="btn btn-ghost btn-icon" style={{ padding: 0, width: 24, height: 24 }} onClick={onClose}><X size={13} /></button>
    </div>
  );
}

export function Spinner({ size = 15 }) {
  return <Loader2 size={size} className="ab-spin" />;
}

export function ErrorNote({ children }) {
  if (!children) return null;
  return <p className="ab-error">{children}</p>;
}

export function Switch({ on, onClick, disabled }) {
  return (
    <div
      className={`ab-switch${on ? " on" : ""}`}
      style={{ opacity: disabled ? 0.5 : 1, pointerEvents: disabled ? "none" : "auto" }}
      onClick={onClick}
      role="switch"
      aria-checked={on}
    >
      <div className="ab-switch-knob" />
    </div>
  );
}

/* ============================================================
   Theme toggle button — Sun / Moon icon.

   The choice is stored, because a theme that resets on every reload is not a
   choice. `main.jsx` applies it before the first paint; this only has to keep
   the two in step.
   ============================================================ */
const THEME_KEY = "agentlab.theme";

export function ThemeToggle() {
  const [dark, setDark] = useState(() => document.documentElement.dataset.theme === "dark");

  const toggle = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.dataset.theme = next ? "dark" : "light";
    try { localStorage.setItem(THEME_KEY, next ? "dark" : "light"); } catch { /* private mode */ }
  };

  return (
    <button
      type="button"
      className="btn btn-secondary btn-icon"
      onClick={toggle}
      title={dark ? "Light mode" : "Dark mode"}
      style={{ width: 34, height: 34 }}
    >
      {dark ? <Sun size={17} /> : <Moon size={17} />}
    </button>
  );
}

/* ============================================================
   Language toggle — EN / VI.

   A segmented pair rather than a single button that cycles: with two languages
   a cycle is the same click, but the pair *shows which one you are in*, which is
   the thing somebody who has just landed on the wrong language needs to see.
   ============================================================ */
export function LanguageToggle() {
  const { lang, setLang } = useLang();
  return (
    <div className="seg" style={{ height: 34, flex: "none" }} role="group" aria-label="Language">
      {LANGUAGES.map((entry) => (
        <button
          key={entry.id}
          type="button"
          onClick={() => setLang(entry.id)}
          title={entry.name}
          aria-pressed={lang === entry.id}
          style={{
            padding: "0 var(--space-3)",
            border: 0,
            background: lang === entry.id ? "var(--color-accent)" : "transparent",
            color: lang === entry.id ? "var(--color-bg)" : "color-mix(in srgb, var(--color-text) 65%, transparent)",
            font: "inherit",
            fontFamily: "var(--font-heading)",
            fontSize: "var(--text-sm)",
            cursor: "pointer",
          }}
        >
          {entry.label}
        </button>
      ))}
    </div>
  );
}

/* The top bar and the session block moved into `AppLayout` when the sidebar
   shell landed — every signed-in screen is inside it, so a page that drew its
   own header drew a second one. */

/* ============================================================
   Settings — one control, one menu.

   Theme and language used to be two controls parked in the shell header: a
   two-button segmented language pair and a sun/moon toggle, both of them
   permanently on screen, on every page, next to the name of the page. Neither
   is about the page, and neither is touched twice a session — they were paying
   for prime header space with the frequency of a preference.

   So they move behind one mark, the way ChatGPT and every app with more than
   two preferences does it: a gear, a small menu, both settings one click in.
   It lives in the sidebar's user footer, beside the account it belongs to,
   which is also where a person goes looking for "my stuff".

   The menu is the only piece of chrome in this app that traps a click outside
   itself, so both ways out are wired: Escape, and a pointer landing anywhere
   that is not the menu. A popover you cannot dismiss without finding its
   button again is the one failure mode of this pattern.
   ============================================================ */

export function SettingsMenu({ compact = false, align = "left" }) {
  const { t, lang, setLang } = useLang();
  const [open, setOpen] = useState(false);
  const [dark, setDark] = useState(() => document.documentElement.dataset.theme === "dark");
  const wrapRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const away = (event) => { if (!wrapRef.current?.contains(event.target)) setOpen(false); };
    const key = (event) => { if (event.key === "Escape") setOpen(false); };
    /* pointerdown, not click: a menu that closes on click-up stays open under
       a drag that started outside it, which reads as it ignoring you. */
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", key);
    };
  }, [open]);

  const setTheme = (next) => {
    setDark(next);
    document.documentElement.dataset.theme = next ? "dark" : "light";
    try { localStorage.setItem("agentlab.theme", next ? "dark" : "light"); } catch { /* private mode */ }
  };

  return (
    <div className="ab-settings" ref={wrapRef}>
      <button
        type="button"
        className="btn btn-secondary btn-icon"
        style={{ width: 28, height: 28, flex: "none" }}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("Settings")}
        title={t("Settings")}
        onClick={() => setOpen((was) => !was)}
      >
        <Settings size={14} />
      </button>

      {open && (
        <div className="ab-settings-menu" role="menu" data-align={align} data-compact={compact ? "1" : "0"}>
          <p className="ab-settings-label">{t("Theme")}</p>
          <div className="seg" role="group" aria-label={t("Theme")}>
            <button type="button" className="ab-settings-opt" aria-pressed={!dark} data-on={!dark ? "1" : "0"} onClick={() => setTheme(false)}>
              <Sun size={13} />{t("Light")}
            </button>
            <button type="button" className="ab-settings-opt" aria-pressed={dark} data-on={dark ? "1" : "0"} onClick={() => setTheme(true)}>
              <Moon size={13} />{t("Dark")}
            </button>
          </div>

          <p className="ab-settings-label">{t("Language")}</p>
          <div className="seg" role="group" aria-label={t("Language")}>
            {LANGUAGES.map((entry) => (
              <button
                key={entry.id}
                type="button"
                className="ab-settings-opt"
                aria-pressed={lang === entry.id}
                data-on={lang === entry.id ? "1" : "0"}
                onClick={() => setLang(entry.id)}
              >
                {entry.name}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
