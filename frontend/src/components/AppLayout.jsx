import { useEffect, useRef, useState } from "react";
import {
  Bot, Home, Layers, ShieldCheck, SlidersHorizontal, LogOut, RefreshCw,
  Library, Users, Gauge,
  ScrollText, GitCompare, ClipboardCheck, Coins, UserCheck,
} from "lucide-react";
import { Hint, PageChromeProvider, SettingsMenu } from "./ui";
import { LogoMark } from "./brand";
import { AgentPresence, useAgentBeat } from "./agent-presence";
import { useLang } from "../i18n";
import { isAdminEquivalent, isPm } from "../lib/roles";
import { PM_TABS } from "../pages/pm/tabs";

/* One icon per PM tab — the tab list itself lives in `pages/pm/tabs.js` so
   the sidebar and the page cannot disagree about what the tabs are. */
const PM_ICONS = {
  overview: <Gauge size={17} />,
  runs: <ScrollText size={17} />,
  quality: <GitCompare size={17} />,
  review: <ClipboardCheck size={17} />,
  cost: <Coins size={17} />,
  people: <UserCheck size={17} />,
};

const collapseIconSvg = `<svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg>`;
const expandIconSvg = `<svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg>`;

export default function AppLayout({
  session,
  view,
  crumb = "WORKSPACE",
  pageTitle = "Your agents",
  queueSize = null,
  goDashboard,
  startDesigner,
  openBuilder,
  openApprovals,
  openSettings,
  openUsers,
  openPm,
  openKnowledgeBases,
  onLogout,
  onReload,
  children
}) {
  const { t } = useLang();

  /* Closed until asked for, and remembered. 240px of permanent nav is a price
     paid on every screen by a person who already knows where they are going;
     the icon rail keeps every destination one click away and hands the width
     back to the work. Somebody who wants the labels opens it once and never
     thinks about it again. */
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem("agentlab.nav.sidebar") !== "open"; } catch { return true; }
  });

  /* A page change is a transformation, so it is animated rather than a cut, and
     the agent goes with it. The content is animated through the Web Animations
     API on a ref — a `key` here would remount whatever is inside, and the PM
     control room shares one component across six tab views, so remounting on a
     tab change would throw away its data and refetch. Nothing unmounts; the
     same element simply plays a move. `animate` is absent in jsdom, hence the
     optional call. */
  const pageRef = useRef(null);
  const beat = useAgentBeat();
  useEffect(() => {
    pageRef.current?.animate?.(
      [{ opacity: 0, transform: "translateY(9px)" }, { opacity: 1, transform: "none" }],
      { duration: 270, easing: "cubic-bezier(.2,.8,.2,1)" }
    );
    beat("exploring", null, 620);
  }, [view, beat]);

  /* What the page on screen says it is called, and where its buttons go.
     `setHead` and `setActionSlot` are useState setters, so they are stable
     for the life of the shell — a page's PageHead can depend on them without
     re-registering on every render. The slot starts null and is filled on the
     commit that attaches it, which costs one extra render at mount and saves
     every page from having to know the header exists. */
  const [head, setHead] = useState(null);
  const [actionSlot, setActionSlot] = useState(null);

  const toggleSide = () => setCollapsed((was) => {
    try { localStorage.setItem("agentlab.nav.sidebar", was ? "open" : "closed"); } catch { /* private mode */ }
    return !was;
  });

  const sideWidth = collapsed ? "64px" : "240px";
  const labelShow = collapsed ? "none" : "block";

  const initials = session?.email ? session.email.slice(0, 2).toUpperCase() : "AL";
  const userEmail = session?.email || "";
  const userRole = (session?.role || "creator").toUpperCase();
  const isAdmin = isAdminEquivalent(session?.role);
  /* A PM owns no agents and serves no audience, so the Workspace section
     would be an empty door for them; they get one section of their own —
     the control room's tabs, one question each (ADR-0053 §4). Admins get the
     same section beneath their own. */
  const pmOnly = isPm(session?.role);
  const navPm = PM_TABS.map((tab) => ({
    id: tab.view,
    label: t(tab.label),
    icon: PM_ICONS[tab.id],
    go: () => openPm(tab.id),
    active: view === tab.view || (tab.id === "overview" && view === "pm-dashboard"),
  }));

  /* No "Chat" entry. Talking to an agent is not a place in the workspace — it is
     something you do *to a particular agent*, so it is reached from that agent:
     its row in "Agents you can use", or the link its Deploy tab hands out. A nav
     item for it can only mean "the last agent you happened to open", which is
     the one thing the person clicking it never means. */
  /* Knowledge Bases lives here, in the Workspace section directly under the
     Workflow builder, rather than in a section of its own. The Enterprise
     (UC-1) feature area it used to sit in is no longer surfaced in the nav —
     the routes still exist in `App.jsx`, they just have no door here. */
  const navMain = [
    { id: "designer", label: t("Home"), icon: <Home size={17} />, go: startDesigner, active: view === "designer" },
    { id: "dashboard", label: t("Agents"), icon: <Bot size={17} />, go: goDashboard, active: view === "dashboard" || view === "chat" },
    { id: "builder", label: t("Workflow builder"), icon: <Layers size={17} />, go: openBuilder, active: view === "builder" || view === "spec" },
    { id: "knowledge-bases", label: t("Knowledge Bases"), icon: <Library size={17} />, go: openKnowledgeBases, active: view === "knowledge-bases" },
  ];

  const navAdmin = [
    { id: "admin-queue", label: t("Review queue"), icon: <ShieldCheck size={17} />, badge: queueSize, go: openApprovals, active: view === "admin-queue" || view === "admin-dossier" },
    { id: "admin-settings", label: t("Platform settings"), icon: <SlidersHorizontal size={17} />, go: openSettings, active: view === "admin-settings" },
    { id: "admin-users", label: t("Accounts"), icon: <Users size={17} />, go: openUsers, active: view === "admin-users" },
  ];

  return (
    /* One viewport, split once. The scroll lives inside the page area rather than
       on the document, so the sidebar and the header stay put without either of
       them having to guess how tall the other is. The class is also what
       `App.test.jsx` asks "is this page actually inside the shell" with. */
    <div className="ab-shell" style={{ "--sidebar-w": sideWidth }}>
      {/* ── SIDEBAR ── */}
      <aside
        style={{
          width: sideWidth,
          flex: "none",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-5)",
          padding: "var(--space-4) 0 var(--space-3)",
          borderRight: "1px solid var(--color-divider)",
          height: "100%",
          transition: "width .22s cubic-bezier(.4,0,.2,1)",
          background: "var(--color-bg)",
          zIndex: 10
        }}
      >
        {/* Brand */}
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", padding: "0 var(--space-4)", position: "relative" }}>
          {/* Collapsed, the mark is the only thing left saying where you are, so
              it stops being decorative and takes the name as its label. */}
          <LogoMark size={28} title={collapsed ? "Agentlab" : undefined} />
          <span style={{ fontFamily: "var(--font-heading)", fontSize: "var(--text-lg)", whiteSpace: "nowrap", overflow: "hidden", flex: 1, display: labelShow }}>Agentlab</span>
          <button
            type="button"
            onClick={toggleSide}
            aria-label={t("Toggle sidebar")}
            style={{
              position: "absolute",
              top: 3,
              right: -11,
              zIndex: 7,
              width: 22,
              height: 22,
              display: "grid",
              placeItems: "center",
              padding: 0,
              border: "1px solid var(--color-divider)",
              background: "var(--color-bg)",
              color: "color-mix(in srgb, var(--color-text) 60%, transparent)",
              cursor: "pointer",
              transition: "color .15s, border-color .15s"
            }}
          >
            <span style={{ width: 13, height: 13, display: "block" }} dangerouslySetInnerHTML={{ __html: collapsed ? expandIconSvg : collapseIconSvg }} />
          </button>
        </div>

        {/* Navigation */}
        <nav style={{ display: "flex", flexDirection: "column", gap: "var(--space-5)", padding: "0 var(--space-3)", flex: 1, minHeight: 0, overflowY: "auto" }}>
          {/* PM section — the PM role's whole sidebar; admins get it too, below */}
          {pmOnly && <PmSection items={navPm} labelShow={labelShow} title={t("PM control room")} />}

          {/* Workspace section */}
          {!pmOnly && (
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <NavLabel labelShow={labelShow}>{t("Workspace")}</NavLabel>
            {navMain.map((item) => (
              <NavItem key={item.id} label={item.label} icon={item.icon} onClick={item.go} active={item.active} labelShow={labelShow} />
            ))}
          </div>

          )}

          {/* Admin section */}
          {isAdmin && (
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <NavLabel labelShow={labelShow}>{t("Admin")}</NavLabel>
              {navAdmin.map((item) => (
                <NavItem key={item.id} label={item.label} icon={item.icon} onClick={item.go} active={item.active} labelShow={labelShow} badge={item.badge} />
              ))}
            </div>
          )}

          {/* The PM's tabs, for admins — after their own section: it is the
              screen they visit second, not the one they live in. */}
          {isAdmin && !pmOnly && <PmSection items={navPm} labelShow={labelShow} title={t("PM control room")} />}
        </nav>

        {/* User footer */}
        <div style={{ padding: "var(--space-3) var(--space-4) 0", borderTop: "1px solid var(--color-divider)", display: "flex", flexWrap: collapsed ? "wrap" : "nowrap", justifyContent: "center", alignItems: "center", gap: "var(--space-2)" }}>
          <span style={{ width: 28, height: 28, flex: "none", display: "grid", placeItems: "center", border: "1px solid var(--color-divider)", fontFamily: "var(--font-heading)", fontSize: "var(--text-xs)" }}>{initials}</span>
          <div style={{ minWidth: 0, flex: 1, display: labelShow }}>
            <p style={{ margin: 0, fontSize: "var(--text-sm)", lineHeight: 1.3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{userEmail}</p>
            <p style={{ margin: 0, fontSize: "var(--text-2xs)", lineHeight: 1.3, letterSpacing: ".08em", textTransform: "uppercase", color: "color-mix(in srgb, var(--color-text) 50%, transparent)" }}>{userRole}</p>
          </div>
          <SettingsMenu compact={collapsed} />
          <button type="button" onClick={onLogout} title={t("Log out")} className="btn btn-secondary" style={{ padding: 0, width: 28, height: 28, flex: "none" }}>
            <LogOut size={14} />
          </button>
        </div>
      </aside>

      {/* ── MAIN CONTENT ── */}
      <div style={{ flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column" }}>
        {/* ── HEADER ──
            One row: where you are, what this page is called, and the buttons
            that act on this page. It used to stack the crumb over the title,
            which cost 68px to say what fits on one line, and then every page
            printed the same title again in its own body at 36px. The title is
            printed once now, here, and a page hands it up through PageHead
            along with its actions (ADR-0057 s4). */}
        <header className="ab-shell-head">
          <p className="crumb">{crumb}</p>
          <h1>{head?.title || pageTitle}<Hint text={head?.hint} /></h1>
          {/* Where a page's own actions land, by portal. */}
          <div className="ab-head-actions" ref={setActionSlot} />
          {/* Reload is the only tool left here, and it is the only one that was
              ever about the page. Theme and language moved to the settings menu
              in the rail's footer: preferences touched twice a session were
              holding header space on every screen. */}
          <div className="ab-head-tools">
            {onReload && (
              <button type="button" onClick={onReload} className="btn btn-secondary btn-icon" title={t("Reload")}>
                <RefreshCw size={15} />
              </button>
            )}
          </div>
        </header>

        {/* Page content. Every page is a flex child of a box with a definite
            height, so a page that scrolls says so itself and a page that fills
            the viewport gets one without doing arithmetic on the header. */}
        <div ref={pageRef} style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
          <PageChromeProvider setHead={setHead} actionSlot={actionSlot}>
            {children}
          </PageChromeProvider>
        </div>
      </div>

      {/* ── THE AGENT ──
          Mounted once, here, for the life of the session — see
          `agent-presence.jsx`. Every screen's work shows up in this one
          creature instead of each screen drawing its own.

          A child of the shell rather than of the header, and last, for two
          reasons that are both about it being `position: fixed`: the header
          carries a `backdrop-filter`, which makes it a containing block and
          would pin a fixed child to the header's box instead of the viewport;
          and being last puts it after the sidebar and the page in paint order,
          so its z-index does not have to fight either of them.

          It sits *outside* the element the page transition animates, so a page
          change moves the page and leaves the agent standing — which is the
          whole point of it being one creature rather than forty. */}
      <AgentPresence />
    </div>
  );
}

/* The control room's section: one entry per tab. */
function PmSection({ items, labelShow, title }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <NavLabel labelShow={labelShow}>{title}</NavLabel>
      {items.map((item) => (
        <NavItem key={item.id} label={item.label} icon={item.icon} onClick={item.go} active={item.active} labelShow={labelShow} />
      ))}
    </div>
  );
}

/* The name of a group of destinations. */
function NavLabel({ labelShow, children }) {
  return (
    <p style={{ margin: "0 0 var(--space-2)", padding: "0 var(--space-2)", fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", whiteSpace: "nowrap", color: "color-mix(in srgb, var(--color-text) 65%, transparent)", display: labelShow }}>{children}</p>
  );
}

/* One sidebar entry — every one of them. All four sections drew this same
   button by hand, which is four places to change a nav row and four chances
   for them to stop matching. */
function NavItem({ label, icon, onClick, active, labelShow, badge }) {
  return (
    <button
      type="button"
      title={label}
      onClick={onClick}
      style={{
        display: "flex",
        alignItems: "center",
        gap: "var(--space-2)",
        width: "100%",
        padding: "var(--space-2)",
        border: 0,
        background: active ? "color-mix(in srgb, var(--color-text) 6%, transparent)" : "transparent",
        cursor: "pointer",
        font: "inherit",
        fontSize: "var(--text-sm)",
        textAlign: "left",
        position: "relative",
        overflow: "hidden",
        transition: "background .15s"
      }}
    >
      <span style={{ position: "absolute", left: 0, top: 5, bottom: 5, width: 2, background: active ? "var(--color-accent)" : "transparent" }} />
      {/* Names its own colour rather than inheriting one. "inherit" reached the
          <button>, and a button with no author colour is UA "buttontext" —
          black — so on the dark rail the inactive icons measured 1.17:1 and
          were, in the collapsed rail, the only thing a person had to navigate
          by. The root fix is in theme.js; this states the value so that the
          icon and its label are the same weight of grey instead of the icon
          coming out brighter than the word beside it. */}
      <span style={{ width: 17, height: 17, flex: "none", display: "flex", alignItems: "center", justifyContent: "center", color: active ? "var(--color-accent)" : "color-mix(in srgb, var(--color-text) 78%, transparent)" }}>{icon}</span>
      <span style={{ flex: 1, whiteSpace: "nowrap", color: active ? "var(--color-text)" : "color-mix(in srgb, var(--color-text) 78%, transparent)", display: labelShow }}>{label}</span>
      {badge ? <span className="tag tag-accent" style={{ display: labelShow }}>{badge}</span> : null}
    </button>
  );
}
