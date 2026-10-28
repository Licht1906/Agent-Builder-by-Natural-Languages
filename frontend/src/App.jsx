import { useCallback, useEffect, useState } from "react";
import * as api from "./lib/api";
import { isAdminEquivalent, isPm } from "./lib/roles";
import { useLang } from "./i18n";
import { GLOBAL_CSS } from "./theme";
import { Toast } from "./components/ui";
import { AgentMascot } from "./components/mascot";
import { AgentPresenceProvider } from "./components/agent-presence";
import LandingPage from "./pages/Landing";
import LoginPage from "./pages/Login";
import RegisterPage from "./pages/Register";
import Dashboard, { screenForVersion } from "./pages/Dashboard";
import DesignerPage from "./pages/Designer";
import SpecPage from "./pages/Spec";
import BuilderPage from "./pages/Builder";
import ChatPage from "./pages/Chat";
import { AdminQueue, AdminDossier } from "./pages/AdminApprovals";
import AdminSettings from "./pages/AdminSettings";
import AdminUsers from "./pages/AdminUsers";
import PMDashboard from "./pages/PMDashboard";
import { PM_TABS, PM_VIEWS, pmTabOf } from "./pages/pm/tabs";
import KnowledgeBasesPage from "./pages/KnowledgeBases";

/* ============================================================
   Views, and what each one is *about*.

   No router library: this app is a wizard, and every screen is reached by
   finishing the one before it. What matters is that the thing being edited —
   an agent and one of its versions — travels in state rather than being
   re-derived from a URL, because the backend hands back the version an edit
   actually landed on and it is not always the one that was opened.
   ============================================================ */
import AppLayout from "./components/AppLayout";

/** The agent a `?agent=` link is asking for, if this page was opened by one.
 *
 *  Read once, at module load, rather than watched: there is no router here, so
 *  the query string only ever changes by a fresh page load anyway. Anything that
 *  is not a plain integer is not an id, and is ignored rather than sent to the
 *  API to be refused. */
function linkedAgentId() {
  try {
    const asked = new URLSearchParams(window.location.search).get("agent");
    return asked && /^\d+$/.test(asked) ? Number(asked) : null;
  } catch {
    return null;
  }
}

export default function App() {
  const { t } = useLang();
  const [session, setSession] = useState(null);
  const [booting, setBooting] = useState(true);
  const [view, setView] = useState("landing");
  const [agent, setAgent] = useState(null);
  const [versionId, setVersionId] = useState(null);
  const [reviewVersionId, setReviewVersionId] = useState(null);
  /* Which half of the review screen is showing — "pending" (the default,
     ADR-0064) or "decided". Held here rather than inside `AdminQueue` so
     that opening a settled dossier and coming back lands on the list it was
     opened from; state inside the page would be remounted away. */
  const [reviewTab, setReviewTab] = useState("pending");
  const [toast, setToast] = useState(null);
  const [queueSize, setQueueSize] = useState(null);
  const [pendingAgentId, setPendingAgentId] = useState(linkedAgentId);

  const notify = useCallback((message, kind = "ok") => {
    setToast({ message, kind });
    setTimeout(() => setToast(null), kind === "error" ? 6000 : 2800);
  }, []);

  /* Land on the agent a shared link named, rather than on the dashboard.
     Survives the sign-in detour: somebody handed the link to a colleague who is
     not signed in yet, and dropping it at the login screen would mean the link
     works for exactly the people who did not need it. */
  const followLink = useCallback(async (targetId) => {
    setPendingAgentId(null);
    try {
      const chosen = await api.agents.read(targetId);
      setAgent(chosen);
      setView("chat");
    } catch (err) {
      // Refused, deleted, or never published to this account. The dashboard is
      // already on screen underneath, so saying why is the whole remedy.
      notify(t("That link's agent is not available to you — {reason}", { reason: err.message }), "error");
    }
  }, [notify]);

  /* Signed in, wherever the session came from. Lands on the dashboard first and
     lets a link move on from there, rather than waiting on the lookup: the
     dashboard is the right place to be standing if the link turns out not to
     resolve, and it is the right place to be watching while it does. */
  const landSignedIn = useCallback((user) => {
    setSession(user);
    // The PM lands on the one screen that is theirs (ADR-0051); everyone else
    // on the Designer's home stage — the workspace opens on "describe a task",
    // the way every prompt-first product opens, and the agent list is one
    // sidebar click away rather than the front door.
    setView(isPm(user.role) ? "pm-overview" : "designer");
    if (isAdminEquivalent(user.role)) {
      api.approvals.queue().then((q) => setQueueSize(q.length)).catch(() => {});
    }
    if (pendingAgentId) followLink(pendingAgentId);
  }, [pendingAgentId, followLink]);

  useEffect(() => {
    if (!api.getToken()) { setBooting(false); return; }
    let cancelled = false;
    api.auth.me()
      .then((user) => { if (!cancelled) landSignedIn(user); })
      .catch(() => api.clearToken())
      .finally(() => { if (!cancelled) setBooting(false); });
    return () => { cancelled = true; };
    // Boot runs once; `landSignedIn` closes over the link read at module load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const signedIn = (user) => {
    landSignedIn(user);
    notify(t("Signed in as {email}", { email: user.email }));
  };

  const logout = () => {
    api.clearToken();
    setSession(null);
    setAgent(null);
    setVersionId(null);
    setView("landing");
  };

  const goDashboard = () => { setView("dashboard"); setAgent(null); setVersionId(null); };

  const openAgent = (chosen, latest) => {
    setAgent(chosen);
    setVersionId(latest ? latest.id : null);
    setView(latest ? screenForVersion(latest) : "spec");
  };

  const openChat = (chosen) => { setAgent(chosen); setView("chat"); };

  const specProposed = async (proposal) => {
    try {
      setAgent(await api.agents.read(proposal.agent_id));
    } catch {
      setAgent({ id: proposal.agent_id, name: t("New agent") });
    }
    setVersionId(proposal.version_id);
    setView("spec");
    notify(t("Specification drafted — review every field before confirming"));
  };

  const openBuilder = (targetVersionId) => {
    setVersionId(targetVersionId);
    setView("builder");
  };

  const handleOpenBuilder = async () => {
    if (agent && versionId) {
      setView("builder");
      return;
    }
    try {
      const mine = await api.agents.mine();
      if (mine && mine.length > 0) {
        const target = mine[0];
        const versions = await api.agents.versions(target.id);
        const latest = versions.length ? versions[versions.length - 1] : null;
        openAgent(target, latest);
        return;
      }
    } catch {}
    setView("designer");
  };

  if (booting) {
    return (
      <div className="ab-root">
        <style>{GLOBAL_CSS}</style>
        {/* The agent is the first thing on screen and the last — it opens the
            session rather than a spinner standing in for it. */}
        <div className="ab-empty" style={{ paddingTop: "36vh" }}><AgentMascot state="processing" size={96} /></div>
      </div>
    );
  }

  /* Where you are, and what this page is — the two halves of the one title
     the shell header prints (ADR-0057 s4).

     The crumb is the *section of the app*, always: WORKSPACE, ADMIN,
     ENTERPRISE, PRODUCT. It used to be the agent's name on the spec and
     builder screens, which made it an object label rather than a path — and
     since both of those screens already name the agent in their own chrome
     (the builder in the very field that renames it), the header was printing
     that name a second time an inch above it. A page whose subject genuinely
     is its title says so with PageHead, which overrides pageTitle from
     inside the page. */
  let crumb = t("WORKSPACE");
  let pageTitle = t("Your agents");
  if (view === "designer") {
    crumb = t("WORKSPACE");
    pageTitle = t("AI Solution Designer");
  } else if (view === "spec") {
    crumb = t("WORKSPACE");
    pageTitle = t("Agent Solution Specification");
  } else if (view === "builder") {
    crumb = t("WORKSPACE");
    pageTitle = t("Workflow Builder");
  } else if (view === "chat") {
    crumb = t("WORKSPACE");
    pageTitle = agent?.name || t("Agent Chat");
  } else if (view === "admin-queue") {
    crumb = t("ADMIN");
    pageTitle = t("Review queue");
  } else if (view === "admin-settings") {
    crumb = t("ADMIN");
    pageTitle = t("Platform settings");
  } else if (view === "admin-dossier") {
    crumb = t("ADMIN");
    pageTitle = t("Approval dossier");
  } else if (view === "knowledge-bases") {
    crumb = t("WORKSPACE");
    pageTitle = t("Data sources");
  } else if (view === "admin-users") {
    crumb = t("ADMIN");
    pageTitle = t("Accounts");
  } else if (PM_VIEWS.has(view) || view === "pm-dashboard") {
    crumb = isPm(session?.role) ? t("PRODUCT") : t("ADMIN");
    pageTitle = t("PM control room");
  }

  /* The control room's tabs are views of their own (`pages/pm/tabs.js`), so
     the sidebar, the page header and the page agree on where the reader is. */
  const openPm = (tabId) => setView(PM_TABS.find((tab) => tab.id === tabId)?.view ?? "pm-overview");

  return (
    /* Presence wraps the whole app, not the shell: the signed-out screens draw
       their own agent, and a screen that declares an activity while signing in
       still has somewhere to declare it to. */
    <AgentPresenceProvider>
    <div className="ab-root">
      <style>{GLOBAL_CSS}</style>

      {!session && view === "landing" && (
        <LandingPage goLogin={() => setView("login")} goRegister={() => setView("register")} />
      )}
      {!session && view === "login" && (
        <LoginPage onSignedIn={signedIn} goLanding={() => setView("landing")} goRegister={() => setView("register")} />
      )}
      {!session && view === "register" && (
        // UC1-01 (#117): registering no longer signs you in — every new
        // account is `pending` until an admin activates it — so this screen
        // only ever routes to login, never to the dashboard.
        <RegisterPage goLanding={() => setView("landing")} goLogin={() => setView("login")} />
      )}

      {/* Every signed-in screen is inside the shell. It used to be a subset —
          Enterprise, Knowledge Bases and Accounts each drew their own header
          instead — and the sibling arrangement that allowed was a real trap:
          the shell is `height:100vh; overflow:hidden`, so a page rendered
          beside it began one whole viewport below the fold, on screen in the
          DOM sense and blank in the sense the person clicking the tab meant.
          `App.test.jsx` still guards it by counting "Log out" buttons. */}
      {session && (
        <AppLayout
          session={session}
          view={view}
          crumb={crumb}
          pageTitle={pageTitle}
          queueSize={queueSize}
          goDashboard={goDashboard}
          startDesigner={() => setView("designer")}
          openBuilder={handleOpenBuilder}
          openApprovals={() => setView("admin-queue")}
          openUsers={() => setView("admin-users")}
          openPm={openPm}
          openSettings={() => setView("admin-settings")}
          openKnowledgeBases={() => setView("knowledge-bases")}
          onLogout={logout}
          onReload={() => window.location.reload()}
        >
          {view === "dashboard" && (
            <Dashboard
              openAgent={openAgent}
              openChat={openChat}
              startDesigner={() => setView("designer")}
              notify={notify}
            />
          )}

          {view === "designer" && <DesignerPage onSpecReady={specProposed} />}

          {view === "knowledge-bases" && <KnowledgeBasesPage notify={notify} />}

          {view === "admin-users" && <AdminUsers notify={notify} />}

          {(PM_VIEWS.has(view) || view === "pm-dashboard") && (
            <PMDashboard tab={pmTabOf(view)} openTab={openPm} notify={notify} />
          )}

          {view === "spec" && agent && versionId && (
            <SpecPage
              agentId={agent.id}
              agentName={agent.name}
              versionId={versionId}
              openBuilder={openBuilder}
              notify={notify}
            />
          )}

          {view === "builder" && agent && versionId && (
            <BuilderPage
              agent={agent}
              versionId={versionId}
              setVersionId={setVersionId}
              notify={notify}
            />
          )}

          {view === "chat" && agent && <ChatPage agent={agent} user={session} />}

          {view === "admin-queue" && (
            <AdminQueue
              openVersion={(id) => { setReviewVersionId(id); setView("admin-dossier"); }}
              tab={reviewTab}
              setTab={setReviewTab}
            />
          )}

          {view === "admin-settings" && <AdminSettings notify={notify} />}

          {view === "admin-dossier" && reviewVersionId && (
            <AdminDossier
              versionId={reviewVersionId}
              back={() => setView("admin-queue")}
              notify={notify}
            />
          )}
        </AppLayout>
      )}

      <Toast toast={toast} onClose={() => setToast(null)} />
    </div>
    </AgentPresenceProvider>
  );
}
