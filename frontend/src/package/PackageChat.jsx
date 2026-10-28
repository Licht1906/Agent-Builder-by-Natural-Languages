import { useEffect, useRef, useState } from "react";
import { Bot, KeyRound, LogIn, LogOut, Send } from "lucide-react";
import { messageOf, parseJson, readEvents } from "../lib/api";
import { useLang } from "../i18n";
import { GLOBAL_CSS } from "../theme";
import { ErrorNote } from "../components/ui";
import { AgentTurn } from "../pages/Chat";
import { decided, newTurn, runReducer, settle } from "../lib/turns";
import {
  authHeaders,
  beginLogin,
  carriesToken,
  claimsOf,
  completeLogin,
  currentToken,
  keepToken,
  signOut,
  takeFragmentToken,
} from "./session";

/* ============================================================
   The exported package's chat (DS-17 / ADR-0059 §10): sign in the way
   `runtime.yaml` says, ask, read the answer with its sources, decide the
   mail it drafted. Nothing else — no builder, no history pane, no feedback.

   The turn itself is Chat V2's: `AgentTurn` draws it and `lib/turns.js`
   builds it from the same event names, so an approval card here shows the
   payload exactly as the platform does, verbatim, the thing the runtime
   hashed. The runtime only ever shows and decides the asker's own pauses,
   so whoever is chatting may decide.
   ============================================================ */

class Refused extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export default function PackageChat({ fetcher = fetch }) {
  const { t } = useLang();
  const [config, setConfig] = useState(null);
  const [signedIn, setSignedIn] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const response = await fetcher("/ui-config");
        if (!response.ok) throw new Error(t("This package could not describe itself (HTTP {status})", { status: response.status }));
        const found = await response.json();
        const mode = found.auth.mode;
        if (mode === "oidc" && found.auth.oidc) await completeLogin(found.auth.oidc, { fetcher });
        if (mode === "jwt") takeFragmentToken();
        if (!live) return;
        setConfig(found);
        setSignedIn(!carriesToken(mode) || Boolean(currentToken()));
      } catch (err) {
        if (live) setError(err.message);
      }
    })();
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetcher]);

  const expired = (message) => {
    signOut();
    setSignedIn(false);
    setError(message);
  };

  return (
    <div className="ab-root" style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
      <style>{GLOBAL_CSS}</style>
      {!config && !error && <p className="text-muted" style={{ margin: "auto" }}>{t("Loading…")}</p>}
      {!config && error && <div style={{ margin: "auto", maxWidth: 520 }}><ErrorNote>{error}</ErrorNote></div>}
      {config && !signedIn && (
        <SignIn config={config} fetcher={fetcher} error={error} onToken={() => { setError(""); setSignedIn(true); }} />
      )}
      {config && signedIn && (
        <Conversation config={config} fetcher={fetcher} onExpired={() => expired(t("Your sign-in has ended — sign in again."))} onSignOut={() => { signOut(); setSignedIn(false); }} />
      )}
    </div>
  );
}

function SignIn({ config, fetcher, error, onToken }) {
  const { t } = useLang();
  const [pasted, setPasted] = useState("");
  const [failure, setFailure] = useState("");
  const { mode, oidc } = config.auth;

  async function viaIssuer() {
    setFailure("");
    try {
      await beginLogin(oidc, { fetcher });
    } catch (err) {
      setFailure(err.message);
    }
  }

  return (
    <div style={{ margin: "auto", width: "min(420px, 92vw)", display: "flex", flexDirection: "column", gap: 14 }}>
      <p style={{ margin: 0, display: "flex", alignItems: "center", gap: 10, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>
        <Bot size={20} />{config.agent}
      </p>
      {mode === "oidc" && oidc?.client_id && (
        <button type="button" className="btn btn-primary" onClick={viaIssuer}>
          <LogIn size={15} />{t("Sign in with your company account")}
        </button>
      )}
      {mode === "oidc" && !oidc?.client_id && (
        <ErrorNote>{t("The package has no auth.oidc.client_id, so this page cannot sign anyone in — ask IT to set it in runtime.yaml.")}</ErrorNote>
      )}
      {mode === "jwt" && (
        <form
          onSubmit={(event) => { event.preventDefault(); if (pasted.trim()) { keepToken(pasted); onToken(); } }}
          style={{ display: "flex", flexDirection: "column", gap: 8 }}
        >
          <label className="text-muted" style={{ fontSize: "var(--text-sm)" }} htmlFor="package-token">
            {t("Paste the access token your company's sign-in gave you")}
          </label>
          <textarea id="package-token" className="input" rows={3} value={pasted} onChange={(event) => setPasted(event.target.value)} />
          <button type="submit" className="btn btn-primary" disabled={!pasted.trim()}><KeyRound size={15} />{t("Continue")}</button>
        </form>
      )}
      <ErrorNote>{failure || error}</ErrorNote>
    </div>
  );
}

function Conversation({ config, fetcher, onExpired, onSignOut }) {
  const { t } = useLang();
  const [messages, setMessages] = useState([{ from: "agent", ...newTurn(), pending: false, text: t("Hi, I'm {name}. How can I help?", { name: config.agent }) }]);
  const [input, setInput] = useState("");
  const [conversationId, setConversationId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const endRef = useRef(null);
  const who = carriesToken(config.auth.mode) ? claimsOf(currentToken() || "")?.email : null;

  useEffect(() => { endRef.current?.scrollIntoView?.({ behavior: "smooth" }); }, [messages, busy]);

  /* One run streamed into the turn at `at` — a fresh question, or the rest of a paused run after its decision. */
  async function streamInto(at, path, body) {
    const response = await fetcher(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Refused(response.status, messageOf(parseJson(text), response.status));
    }
    const apply = runReducer();
    await readEvents(response, (name, data) => {
      if (name === "thread") {
        setConversationId(data.conversation_id);
        return;
      }
      setMessages((current) => current.map((turn, index) => (index === at ? apply(turn, name, data) : turn)));
    });
    setMessages((current) => current.map((turn, index) => (index === at ? settle(turn) : turn)));
  }

  function failed(err) {
    if (err.status === 401) onExpired();
    else setError(err.message);
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setError("");
    setBusy(true);
    const at = messages.length + 1;
    setMessages((current) => [...current, { from: "user", text }, newTurn()]);
    try {
      await streamInto(at, "/chat", { message: text, conversation_id: conversationId });
    } catch (err) {
      setMessages((current) => current.filter((_, index) => index !== at));
      failed(err);
    } finally {
      setBusy(false);
    }
  }

  async function decide(at, approved, reason) {
    const approvalId = messages[at]?.approval?.id;
    if (busy || !approvalId) return;
    setError("");
    setBusy(true);
    setMessages((current) => current.map((turn, index) => (index === at ? decided(turn, approved) : turn)));
    try {
      await streamInto(at, `/approvals/${approvalId}`, approved ? { approved: true } : { approved: false, reason });
    } catch (err) {
      setMessages((current) =>
        current.map((turn, index) => (index === at ? { ...turn, pending: false, approval: { ...turn.approval, decided: false, approved: null } } : turn))
      );
      failed(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <header style={{ display: "flex", alignItems: "center", gap: 12, padding: "var(--space-3) var(--space-6)", borderBottom: "1px solid var(--color-divider)", flex: "none" }}>
        <span style={{ width: 32, height: 32, display: "grid", placeItems: "center", border: "1px solid var(--color-divider)", color: "var(--color-accent)" }}>
          <Bot size={16} />
        </span>
        <p style={{ flex: 1, margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)" }}>{config.agent}</p>
        {who && <span className="ab-mono text-muted" style={{ fontSize: "var(--text-xs)" }}>{who}</span>}
        {carriesToken(config.auth.mode) && (
          <button type="button" className="btn btn-secondary btn-sm" onClick={onSignOut}><LogOut size={13} />{t("Sign out")}</button>
        )}
      </header>

      <div className="ab-scrollbar" style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "var(--space-6)", display: "flex", flexDirection: "column", gap: 14, maxWidth: 840, width: "100%", margin: "0 auto" }}>
        <div style={{ flex: "1 0 auto" }} />
        {messages.map((message, index) =>
          message.from === "user" ? (
            <div key={index} style={{ display: "flex", flexDirection: "column", alignItems: "flex-end" }}>
              <p className="ab-chat-bubble-user" style={{ margin: 0 }}>{message.text}</p>
            </div>
          ) : (
            <AgentTurn key={index} turn={message} canDecide busy={busy} onDecide={(approved, reason) => decide(index, approved, reason)} />
          )
        )}
        <ErrorNote>{error}</ErrorNote>
        <div ref={endRef} />
      </div>

      <form
        onSubmit={(event) => { event.preventDefault(); send(); }}
        style={{ display: "flex", gap: 10, alignItems: "flex-end", padding: "var(--space-4) var(--space-6)", borderTop: "1px solid var(--color-divider)", maxWidth: 840, width: "100%", margin: "0 auto", flex: "none" }}
      >
        <textarea
          className="input"
          rows={2}
          aria-label={t("Message")}
          placeholder={t("Message this agent…")}
          value={input}
          disabled={busy}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); } }}
          style={{ flex: 1, minHeight: 46, height: 46, resize: "none" }}
        />
        <button type="submit" disabled={busy || !input.trim()} className="btn btn-primary" style={{ height: 46, paddingInline: "var(--space-6)", flex: "none" }}>
          <Send size={15} />{busy ? t("Sending…") : t("Send")}
        </button>
      </form>
    </>
  );
}
