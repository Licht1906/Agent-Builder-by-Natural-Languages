import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, Bot, Check, ChevronDown, ChevronRight, MessageSquare, PanelLeft, Pencil, Plus, Send, ShieldCheck, ThumbsDown, ThumbsUp, Trash2, Wrench, X } from "lucide-react";
import * as api from "../lib/api";
import { ErrorNote, Spinner } from "../components/ui";
import { AgentBusy } from "../components/mascot";
import { useAgentActivity, useAgentSide } from "../components/agent-presence";
import { useLang } from "../i18n";
import SourcesPanel from "../components/SourcesPanel";
import Markdown from "../components/Markdown";
import ApprovalPayload from "../components/ApprovalPayload";
import { decided, newTurn, runReducer, settle, turnsFromHistory } from "../lib/turns";

/* ============================================================
   Talking to a published agent — the end-user's whole view of it.

   Two panes. The right one is the conversation: answers stream in token by
   token from the deployed version's workflow. The left one is every other
   conversation this person has had with this agent, which is what makes the
   right one worth having — before it, a reload lost the only pointer to a
   thread and the agent's memory of it became unreachable while still existing.

   The id still arrives on the stream's first event and is still sent with every
   later turn; what changed is that it is no longer the *only* copy. The server
   indexes each turn under its conversation, so the list survives a reload, a new
   tab, and a different machine — none of which a piece of React state does.

   A production thread is addressed per caller, so two people sharing one
   deployment never share a history, and neither sees the other in this list.
   ============================================================ */

/** How long ago, in words, for a list that is sorted by exactly this.
 *
 *  Formatted in the browser rather than the server because only the browser
 *  knows the reader's timezone. The server sends an instant; this turns it into
 *  "2h ago" for whoever is looking. */
function agoLabel(iso, t) {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "";
  const minutes = Math.floor((Date.now() - then.getTime()) / 60000);
  if (minutes < 1) return t("just now");
  if (minutes < 60) return t("{n}m ago", { n: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t("{n}h ago", { n: hours });
  const days = Math.floor(hours / 24);
  if (days < 7) return t("{n}d ago", { n: days });
  return then.toLocaleDateString();
}

export default function ChatPage({ agent, user }) {
  const { t } = useLang();
  const greeting = { from: "agent", text: t("Hi, I'm {name}. How can I help?", { name: agent.name }) };

  /* Whether the person chatting is the one who may decide a paused run. The
     decide endpoint is owner-only and version-addressed, and the version a
     published agent's run paused on is by construction the deployed one — so
     the owner can be offered the buttons right here, in the bubble that said
     "waiting". Before this, the chat said ⏸ and nothing else, and the creator
     had to know to open the builder on the right version to find the queue:
     the report *"hình như HITL không hiện"* was exactly that. */
  const isOwner = Boolean(user) && (user.id === agent.owner_id || (Boolean(agent.owner_email) && user.email === agent.owner_email));
  const canDecide = isOwner && Boolean(agent.deployed_version_id);
  /* A V1 pipeline answers at a different door than a V2 workflow. The running
     list says which; an agent reached some other way (a shared link) says
     nothing and is taken for a workflow, which is every agent built since
     ADR-0020. */
  const runsWorkflow = agent.runs_workflow !== false;
  const [messages, setMessages] = useState([greeting]);
  const [input, setInput] = useState("");
  const [conversationId, setConversationId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  /* ── The left pane's own state ── */
  const [history, setHistory] = useState([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [openingId, setOpeningId] = useState(null);
  const [renamingId, setRenamingId] = useState(null);
  const [renameText, setRenameText] = useState("");
  const [confirmingId, setConfirmingId] = useState(null);
  const [collapsed, setCollapsed] = useState(false);

  /* A conversation with a composer at the foot of it: the agent stands on the
     side away from the bar, beside the exchange rather than on top of it. */
  useAgentSide("left");

  useAgentActivity("processing", busy, t("the agent is working"));
  useAgentActivity("exploring", loadingHistory, t("loading your conversations"));

  const endRef = useRef(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy]);

  /* The list, re-read from the server rather than patched in place after a turn.
     A turn can create a conversation, retitle nothing, and move one row to the
     top all at once; re-reading is one request that gets all three right, where
     a local patch is three rules that can each be wrong. */
  const loadHistory = useCallback(async () => {
    try {
      setHistory(await api.conversations.list(agent.id));
    } catch (err) {
      // A sidebar that cannot load is not a reason to block the chat: the right
      // pane works without it, so this is reported and then left alone.
      setError(err.message);
    } finally {
      setLoadingHistory(false);
    }
  }, [agent.id]);

  /* Switching agents is switching to a different history and a different
     conversation — everything below is per agent, so all of it resets. */
  useEffect(() => {
    setConversationId(null);
    setMessages([greeting]);
    setError("");
    setLoadingHistory(true);
    loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent.id, loadHistory]);

  function newSession() {
    if (busy) return;
    setConversationId(null);
    setMessages([greeting]);
    setError("");
  }

  /* Open one conversation from the list.
   *
   * The transcript is fetched rather than remembered: this screen may never have
   * had it — the conversation could be from last week, or from another device —
   * so the checkpoint is the only thing that knows what was said. */
  async function openConversation(entry) {
    if (busy || entry.conversation_id === conversationId) return;
    setOpeningId(entry.conversation_id);
    setError("");
    try {
      const thread = await api.conversations.messages(agent.id, entry.conversation_id);
      // Rebuilt into the same turns the live stream builds — speech, folded
      // steps, activity chips, the approval card if the run is still waiting —
      // rather than one bubble per checkpoint line. The flat version is what
      // showed a tool's raw JSON and "Người duyệt đã đồng ý" as things the
      // agent had said.
      const turns = turnsFromHistory(thread.turns, thread.pending_approval);
      // The greeting is prepended to a loaded thread as well, so a reopened
      // conversation looks like the one that was left rather than starting
      // abruptly on whatever was said first.
      setMessages([greeting, ...turns]);
      setConversationId(entry.conversation_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setOpeningId(null);
    }
  }

  async function saveRename(entry) {
    const title = renameText.trim();
    setRenamingId(null);
    if (!title || title === entry.title) return;
    // Written to the screen before the server confirms: renaming is the one
    // action here with no interesting failure mode, and a label that waited for
    // a round trip would feel broken on a slow connection. `loadHistory` puts
    // the server's answer back either way.
    setHistory((rows) =>
      rows.map((row) => (row.conversation_id === entry.conversation_id ? { ...row, title } : row))
    );
    try {
      await api.conversations.rename(agent.id, entry.conversation_id, title);
    } catch (err) {
      setError(err.message);
    } finally {
      loadHistory();
    }
  }

  /* Take a conversation out of the list.
   *
   * "Remove", never "delete for ever" — the endpoint drops the index row and
   * leaves the checkpoint alone, so the wording here has to match what the
   * server actually does. Confirmed in place rather than in a modal, because
   * what is being risked is a row in a list. */
  async function removeConversation(entry) {
    setConfirmingId(null);
    setHistory((rows) => rows.filter((row) => row.conversation_id !== entry.conversation_id));
    if (entry.conversation_id === conversationId) newSession();
    try {
      await api.conversations.remove(agent.id, entry.conversation_id);
    } catch (err) {
      setError(err.message);
      loadHistory();
    }
  }

  /* Run a stream into the turn at `at`. One reducer for everything a run emits,
     shared by a fresh turn and by the second half of a paused run after a
     decision: the two are one execution from the run's point of view, and two
     copies of the rules would drift apart at the first fix applied to only one
     of them. The reducer itself lives in `lib/turns.js`, where it has no React
     to hide behind and its tests can call it directly. */
  function streamInto(at) {
    const apply = runReducer();
    return (name, data) => {
      if (name === "thread") {
        setConversationId(data.conversation_id);
        // The run's own name (ADR-0053): what a thumbs on this answer is
        // filed under, since the ledger row is written after the stream ends.
        if (data.trace_id) {
          setMessages((current) => current.map((turn, index) => (index === at ? { ...turn, trace_id: data.trace_id } : turn)));
        }
        return;
      }
      setMessages((current) => current.map((turn, index) => (index === at ? apply(turn, name, data) : turn)));
    };
  }

  function settleTurn(at) {
    setMessages((current) => current.map((turn, index) => (index === at ? settle(turn) : turn)));
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setError("");
    setBusy(true);
    // The turn's index is fixed the moment it is appended, so every event of
    // this run lands in it even if something else is appended later.
    const at = messages.length + 1;
    setMessages((current) => [...current, { from: "user", text }, newTurn()]);

    try {
      const body = { message: text, conversation_id: conversationId };
      if (runsWorkflow) await api.runs.deployedChat(agent.id, body, streamInto(at));
      else await api.runs.pipelineChat(agent.id, body, streamInto(at));
      settleTurn(at);
    } catch (err) {
      setError(err.message);
      setMessages((current) => current.filter((_, index) => index !== at));
    } finally {
      setBusy(false);
      // After the turn, not before: the row is written server-side by the same
      // request that ran it, so re-reading any earlier would race the write that
      // created this conversation.
      loadHistory();
    }
  }

  /* Decide a paused run from the card that announced the pause, and stream the
     rest of it into the *same* turn: the decision, the action it unblocked and
     the answer that follows all belong to the exchange that paused, not to a
     new bubble appearing out of nowhere. The decision is recorded server-side
     before the graph moves (ADR-0024), so a stream that dies here still leaves
     the record; what this screen owes is only to show what happened next. */
  async function decide(at, approved, reason) {
    if (busy) return;
    const approvalId = messages[at]?.approval?.id;
    if (!approvalId) return;
    setError("");
    setBusy(true);
    setMessages((current) => current.map((turn, index) => (index === at ? decided(turn, approved) : turn)));
    try {
      await api.runs.decideApproval(agent.id, agent.deployed_version_id, approvalId, { approved, reason }, streamInto(at));
      settleTurn(at);
    } catch (err) {
      setError(err.message);
      setMessages((current) =>
        current.map((turn, index) =>
          index === at ? { ...turn, pending: false, approval: { ...turn.approval, decided: false, approved: null } } : turn
        )
      );
    } finally {
      setBusy(false);
    }
  }

  const sideWidth = collapsed ? 0 : 264;

  return (
    <main style={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden" }}>
      {/* ══ Conversation history ══ */}
      <aside
        style={{
          width: sideWidth,
          flex: "none",
          display: "flex",
          flexDirection: "column",
          borderRight: collapsed ? "none" : "1px solid var(--color-divider)",
          overflow: "hidden",
          transition: "width .22s cubic-bezier(.4,0,.2,1)",
          background: "var(--color-bg)",
        }}
      >
        <div style={{ padding: "var(--space-3) var(--space-3) var(--space-2)", display: "flex", flexDirection: "column", gap: "var(--space-2)", minWidth: 264 }}>
          <p style={{ margin: 0, padding: "0 var(--space-1)", fontSize: "var(--text-2xs)", letterSpacing: ".14em", textTransform: "uppercase", color: "color-mix(in srgb, var(--color-text) 45%, transparent)" }}>
            {t("Conversations")}
          </p>
          <button type="button" onClick={newSession} disabled={busy} className="btn btn-secondary" style={{ height: 36, justifyContent: "flex-start" }}>
            <Plus size={14} />{t("New conversation")}
          </button>
        </div>

        <div className="ab-scrollbar" style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "0 var(--space-3) var(--space-4)", minWidth: 264 }}>
          {loadingHistory && (
            <p style={{ margin: "8px 4px", fontSize: "var(--text-sm)", color: "color-mix(in srgb, var(--color-text) 50%, transparent)" }}>
              <Spinner size={12} /> {t("Loading…")}
            </p>
          )}

          {!loadingHistory && history.length === 0 && (
            <p style={{ margin: "8px 4px", fontSize: "var(--text-sm)", lineHeight: 1.6, color: "color-mix(in srgb, var(--color-text) 50%, transparent)" }}>
              {t("Nothing here yet. Say something below and this conversation will be saved for you to come back to.")}
            </p>
          )}

          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {history.map((entry) => {
              const active = entry.conversation_id === conversationId;
              const renaming = entry.conversation_id === renamingId;
              const confirming = entry.conversation_id === confirmingId;

              if (renaming) {
                return (
                  <div key={entry.conversation_id} style={{ display: "flex", gap: 4, padding: "var(--space-1)" }}>
                    <input
                      className="input"
                      autoFocus
                      value={renameText}
                      aria-label={t("Conversation name")}
                      onChange={(event) => setRenameText(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") { event.preventDefault(); saveRename(entry); }
                        if (event.key === "Escape") setRenamingId(null);
                      }}
                      style={{ flex: 1, minWidth: 0, height: 32, fontSize: "var(--text-sm)" }}
                    />
                    <button type="button" onClick={() => saveRename(entry)} title={t("Save")} className="btn btn-secondary" style={{ flex: "none", padding: 0, width: 30, height: 32 }}>
                      <Check size={13} />
                    </button>
                  </div>
                );
              }

              return (
                <div
                  key={entry.conversation_id}
                  className="ab-convo-row"
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    padding: "var(--space-2)",
                    background: active ? "color-mix(in srgb, var(--color-text) 6%, transparent)" : "transparent",
                    position: "relative",
                  }}
                >
                  <span style={{ position: "absolute", left: 0, top: 6, bottom: 6, width: 2, background: active ? "var(--color-accent)" : "transparent" }} />
                  <button
                    type="button"
                    onClick={() => openConversation(entry)}
                    disabled={busy}
                    title={entry.title}
                    style={{
                      flex: 1,
                      minWidth: 0,
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      border: 0,
                      background: "transparent",
                      padding: 0,
                      font: "inherit",
                      textAlign: "left",
                      cursor: busy ? "default" : "pointer",
                      color: "inherit",
                    }}
                  >
                    {openingId === entry.conversation_id
                      ? <Spinner size={12} />
                      : <MessageSquare size={13} style={{ flex: "none", color: active ? "var(--color-accent)" : "color-mix(in srgb, var(--color-text) 45%, transparent)" }} />}
                    <span style={{ minWidth: 0, flex: 1 }}>
                      <span style={{ display: "block", fontSize: "var(--text-sm)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: active ? "var(--color-text)" : "color-mix(in srgb, var(--color-text) 78%, transparent)" }}>
                        {entry.title}
                      </span>
                      <span style={{ display: "block", fontFamily: "ui-monospace, monospace", fontSize: "var(--text-2xs)", color: "color-mix(in srgb, var(--color-text) 45%, transparent)" }}>
                        {agoLabel(entry.last_message_at, t)}
                      </span>
                    </span>
                  </button>

                  {/* Row actions. Two steps for the destructive one, and the
                      second step is the only place the word "remove" is acted
                      on — a single click on a list this dense is too easy to
                      make by accident. */}
                  <span className="ab-convo-actions" style={{ display: "flex", gap: 2, flex: "none" }}>
                    {confirming ? (
                      <>
                        <button type="button" onClick={() => removeConversation(entry)} title={t("Remove from my list")} className="btn btn-secondary" style={{ padding: 0, width: 24, height: 24, color: "#dc2626" }}>
                          <Check size={12} />
                        </button>
                        <button type="button" onClick={() => setConfirmingId(null)} title={t("Cancel")} className="btn btn-secondary" style={{ padding: 0, width: 24, height: 24 }}>
                          <X size={12} />
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          type="button"
                          onClick={() => { setRenamingId(entry.conversation_id); setRenameText(entry.title); }}
                          title={t("Rename")}
                          className="btn btn-secondary"
                          style={{ padding: 0, width: 24, height: 24 }}
                        >
                          <Pencil size={12} />
                        </button>
                        <button type="button" onClick={() => setConfirmingId(entry.conversation_id)} title={t("Remove from my list")} className="btn btn-secondary" style={{ padding: 0, width: 24, height: 24 }}>
                          <Trash2 size={12} />
                        </button>
                      </>
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </aside>

      {/* ══ The conversation ══ */}
      <section style={{ flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {/* ── Agent identity ── */}
        <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "var(--space-3) var(--space-6)", borderBottom: "1px solid var(--color-divider)", flex: "none" }}>
          <button
            type="button"
            onClick={() => setCollapsed((shut) => !shut)}
            title={collapsed ? t("Show conversations") : t("Hide conversations")}
            aria-label={collapsed ? t("Show conversations") : t("Hide conversations")}
            className="btn btn-secondary"
            style={{ flex: "none", padding: 0, width: 32, height: 32 }}
          >
            <PanelLeft size={15} />
          </button>
          <span style={{ width: 32, height: 32, flex: "none", display: "grid", placeItems: "center", border: "1px solid var(--color-divider)", color: "var(--color-accent)" }}>
            <Bot size={16} />
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <p style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: "var(--text-xl)", lineHeight: 1.2 }}>{agent.name}</p>
            <p style={{ margin: 0, fontFamily: "ui-monospace, monospace", fontSize: "var(--text-xs)", color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>
              {agent.owner_email ? t("published by {email}", { email: agent.owner_email }) : t("published agent")}
            </p>
          </div>
        </div>

        {/* ── Transcript ── */}
        <div className="ab-scrollbar" style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "var(--space-6)", display: "flex", flexDirection: "column", gap: 14, maxWidth: 840, width: "100%", margin: "0 auto" }}>
          <div style={{ flex: "1 0 auto" }} />
          {messages.map((message, index) =>
            message.from === "user" ? (
              <div key={index} style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", animation: "agUp .3s ease both" }}>
                <p className="ab-chat-bubble-user" style={{ margin: 0 }}>{message.text}</p>
              </div>
            ) : (
              <AgentTurn
                key={index}
                turn={message}
                canDecide={canDecide}
                busy={busy}
                onDecide={(approved, reason) => decide(index, approved, reason)}
              />
            )
          )}
          <ErrorNote>{error}</ErrorNote>
          <div ref={endRef} />
        </div>

        {/* ── Composer ── */}
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
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); }
            }}
            style={{ flex: 1, minHeight: 46, height: 46, resize: "none" }}
          />
          <button type="submit" disabled={busy || !input.trim()} className="btn btn-primary" style={{ height: 46, paddingInline: "var(--space-6)", flex: "none" }}>
            <Send size={15} />{busy ? t("Sending…") : t("Send")}
          </button>
        </form>
      </section>
    </main>
  );
}

/* ============================================================
   One agent turn, drawn from the structure `lib/turns.js` builds.

   Top to bottom: what the run did (chips), what earlier steps said (folded),
   the answer, the approval card if the run stopped for one, and the sources.
   A turn with no answer says something anyway — a tool that ran, an error —
   in words, never as the payload it came from.
   ============================================================ */
export function AgentTurn({ turn, canDecide, busy, onDecide }) {
  const { t } = useLang();
  const thinking = turn.pending && !turn.text;
  const spoken = Boolean(turn.text);
  const quiet = !spoken && !turn.approval && !turn.pending;
  const lastTool = quiet ? turn.activity.findLast((item) => item.kind === "tool" && item.ok) : null;
  const lastDecision = quiet && !lastTool ? turn.activity.findLast((item) => item.kind === "decision") : null;
  const showBubble = thinking || spoken || quiet;

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", animation: "agUp .3s ease both", maxWidth: "100%" }}>
      <ActivityTrail items={turn.activity} />
      <StepsTrail steps={turn.steps} />
      {showBubble && (
        <p
          className="ab-chat-bubble-agent"
          style={{ margin: 0, ...(turn.failed ? { borderColor: "rgba(239,68,68,.3)", color: "#f87171" } : {}) }}
        >
          {thinking && <AgentBusy label={t("thinking")} size={38} />}
          {spoken && <Markdown text={turn.text} />}
          {quiet && lastTool && <ToolOutcome item={lastTool} />}
          {quiet && !lastTool && lastDecision && <Markdown text={lastDecision.text} />}
          {quiet && !lastTool && !lastDecision && <span style={{ opacity: 0.7 }}>{t("(the run produced no text)")}</span>}
          {turn.pending && spoken && (
            <span style={{ display: "inline-block", width: 7, height: 15, background: "var(--color-accent)", verticalAlign: -2, marginLeft: 2, animation: "agBlink 1s steps(1) infinite" }} />
          )}
        </p>
      )}
      {turn.approval && <ApprovalCard approval={turn.approval} canDecide={canDecide} busy={busy} onDecide={onDecide} />}
      {!turn.pending && <SourcesPanel entries={turn.sources} />}
      {!turn.pending && turn.trace_id && <TurnThumbs traceId={turn.trace_id} />}
    </div>
  );
}

/** The cheapest label there is (PM playbook step 4): one thumbs on the answer
 *  just received, filed under the run's `trace_id`. A note is optional and
 *  only asked for on a thumbs-down — the request of a person who disliked an
 *  answer is the golden case nobody has to invent (slide 11). */
function TurnThumbs({ traceId }) {
  const { t } = useLang();
  const [vote, setVote] = useState(null);
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [thanked, setThanked] = useState(false);

  async function send(up, comment) {
    setBusy(true);
    try {
      await api.runFeedback.leave(traceId, up, comment);
      setVote(up);
      setAsking(false);
      setThanked(true);
    } catch {
      // A thumbs that fails to land is not worth an error banner in a chat.
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 6, fontSize: "var(--text-xs)" }}>
      <button type="button" className={`btn btn-sm btn-icon ${vote === true ? "btn-primary" : "btn-ghost"}`} disabled={busy} onClick={() => send(true, "")} title={t("Good answer")} aria-label={t("Good answer")} style={{ width: 28, height: 28 }}>
        <ThumbsUp size={13} />
      </button>
      <button type="button" className={`btn btn-sm btn-icon ${vote === false ? "btn-primary" : "btn-ghost"}`} disabled={busy} onClick={() => setAsking((v) => !v)} title={t("Bad answer")} aria-label={t("Bad answer")} style={{ width: 28, height: 28 }}>
        <ThumbsDown size={13} />
      </button>
      {asking && (
        <>
          <input className="ab-input" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("What was wrong? (optional)")} style={{ minWidth: 220, fontSize: "var(--text-xs)" }} />
          <button type="button" className="btn btn-sm btn-secondary" disabled={busy} onClick={() => send(false, note)}>{t("Send")}</button>
        </>
      )}
      {thanked && !asking && <span style={{ color: "color-mix(in srgb, var(--color-text) 55%, transparent)" }}>{t("Thanks — recorded.")}</span>}
    </div>
  );
}

/** "Đã thực hiện gmail.send_email" — a turn that ended on an action and said
 *  nothing after it. The payload is there for whoever wants it, folded. */
function ToolOutcome({ item }) {
  const { t } = useLang();
  const [open, setOpen] = useState(false);
  return (
    <>
      {t("Done: {tool}", { tool: item.tool })}
      {item.result ? (
        <>
          {" "}
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen((shown) => !shown)} style={{ padding: "0 var(--space-1)", fontSize: "var(--text-xs)", border: "none", verticalAlign: "baseline" }}>
            {open ? t("hide result") : t("show result")}
          </button>
          {open && <RawResult text={item.result} />}
        </>
      ) : null}
    </>
  );
}

function RawResult({ text }) {
  return (
    <pre className="ab-mono" style={{ fontSize: "var(--text-xs)", margin: "6px 0 0", padding: "var(--space-1) var(--space-2)", whiteSpace: "pre-wrap", wordBreak: "break-word", background: "var(--color-surface)", border: "1px solid var(--color-divider)", maxWidth: "100%", overflowX: "auto" }}>
      {String(text)}
    </pre>
  );
}

/** What the run did this turn, as a column of small chips above the answer.
 *
 *  A tool call, a verdict, a stop — each one line, each readable without
 *  opening it, each openable for the payload. This is the place a person looks
 *  when the answer says "đã gửi" and they want to see *that* it was sent; it is
 *  not the answer, so it never takes the bubble. */
export function ActivityTrail({ items }) {
  const { t } = useLang();
  const [openIndex, setOpenIndex] = useState(null);
  if (!items || items.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3, marginBottom: 6, maxWidth: "80%" }}>
      {items.map((item, index) => {
        const open = openIndex === index;
        const look = chipLook(item, t);
        const openable = item.kind === "tool" && Boolean(item.result || item.args);
        return (
          <div key={index} style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", maxWidth: "100%" }}>
            <button
              type="button"
              onClick={() => openable && setOpenIndex(open ? null : index)}
              title={openable ? t("show result") : undefined}
              style={{ display: "inline-flex", alignItems: "center", gap: 6, maxWidth: "100%", padding: "0 var(--space-2)", border: "1px solid var(--color-divider)", background: "transparent", color: look.color, cursor: openable ? "pointer" : "default", font: "inherit", fontFamily: "ui-monospace, monospace", fontSize: "var(--text-xs)", textAlign: "left" }}
            >
              {look.icon}
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {look.label}{item.simulated ? ` · ${t("simulated")}` : ""}
              </span>
            </button>
            {open && item.args && <RawResult text={JSON.stringify(item.args, null, 1)} />}
            {open && item.result && <RawResult text={item.result} />}
          </div>
        );
      })}
    </div>
  );
}

/** Icon, colour and sentence for one activity item. */
function chipLook(item, t) {
  const muted = "color-mix(in srgb, var(--color-text) 60%, transparent)";
  const bad = "#dc2626";
  if (item.kind === "tool") {
    if (item.ok === null) return { icon: <Spinner size={11} />, color: muted, label: t("Running {tool}…", { tool: item.tool }) };
    if (item.ok) return { icon: <Check size={12} />, color: "var(--color-accent)", label: t("Ran {tool}", { tool: item.tool }) };
    return { icon: <X size={12} />, color: bad, label: t("{tool} failed", { tool: item.tool }) };
  }
  if (item.kind === "decision") {
    return { icon: <ShieldCheck size={12} />, color: item.ok ? "var(--color-accent)" : bad, label: item.text };
  }
  if (item.kind === "stopped") return { icon: <AlertTriangle size={12} />, color: "var(--color-canh-bao)", label: item.text };
  return { icon: <AlertTriangle size={12} />, color: bad, label: item.text };
}

/** Những gì các bước trước đã nói, gấp lại — không phải xoá đi.
 *
 *  Một workflow chạy nhiều node Agent/LLM và **node nào cũng** phát chữ ra
 *  đây: một graph trích xuất rồi soạn thảo sẽ nói hai lần. Chỉ có lời cuối
 *  là câu trả lời, nên chỉ nó đứng lại trong bong bóng — nhưng lời trước đó
 *  không biến mất, nó thu thành một dòng bấm được ngay phía trên.
 *
 *  Mặc định đóng: một người hỏi agent thì muốn đọc câu trả lời, không phải
 *  đọc phần dàn giáo. Nhưng khi câu trả lời trông sai thì đúng phần dàn giáo
 *  đó là thứ nói cho họ biết agent đã hiểu gì — và trước đây nó bị xoá mất.
 */
export function StepsTrail({ steps }) {
  const { t } = useLang();
  const [open, setOpen] = useState(false);
  if (!steps || steps.length === 0) return null;
  return (
    <div style={{ maxWidth: "80%", marginBottom: 4 }}>
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        onClick={() => setOpen((shown) => !shown)}
        style={{ padding: "0 var(--space-1)", fontSize: "var(--text-xs)", border: "none", color: "var(--ab-muted, color-mix(in srgb, var(--color-text) 55%, transparent))" }}
      >
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        {t("{count} earlier steps", { count: steps.length })}
      </button>
      {open && steps.map((step, index) => (
        <p
          key={index}
          className="ab-chat-bubble-agent"
          style={{ margin: "4px 0 0", fontSize: "var(--text-sm)", opacity: 0.72, borderStyle: "dashed" }}
        >
          <Markdown text={step} />
        </p>
      ))}
    </div>
  );
}

/* The pause, as a card of its own rather than a "⏸" glued to the front of a
   bubble: it is not something the agent said, it is the run standing still
   until a person moves it. The creator gets the buttons; everyone else is told
   who is being waited for. Once decided, the card stays and says so — the
   decision is part of the record of this exchange. */
export function ApprovalCard({ approval, canDecide, busy, onDecide }) {
  const { t } = useLang();
  const who = approval.role || t("an approver");
  const waiting = !approval.decided;
  return (
    <div style={{ maxWidth: "80%", marginTop: 4, border: `1px solid ${waiting ? "var(--color-accent)" : "var(--color-divider)"}`, background: waiting ? "var(--color-accent-100)" : "transparent", color: waiting ? "var(--color-accent-800)" : "inherit", padding: "var(--space-2) var(--space-3)", animation: "agUp .3s ease both" }}>
      <p style={{ margin: 0, display: "flex", alignItems: "center", gap: 8, fontFamily: "var(--font-heading)", fontSize: "var(--text-md)" }}>
        <ShieldCheck size={15} />
        {waiting ? t("Waiting on {who}", { who }) : approval.approved ? `✓ ${t("Approved")}` : `✕ ${t("Rejected")}`}
      </p>
      {approval.message && (
        <p style={{ margin: "6px 0 0", fontSize: "var(--text-sm)", lineHeight: 1.6, whiteSpace: "pre-wrap", wordBreak: "break-word", opacity: waiting ? 1 : 0.75 }}>
          <Markdown text={approval.message} />
        </p>
      )}
      {waiting && <ApprovalPayload actions={approval.actions} />}
      {waiting && (
        canDecide
          ? <ApprovalButtons disabled={busy} onDecide={onDecide} />
          : <p style={{ margin: "8px 0 0", fontSize: "var(--text-xs)", opacity: 0.8 }}>{t("The agent's creator will decide; the run continues once they have.")}</p>
      )}
    </div>
  );
}

/* Approve on the spot; reject with the reason the backend insists on
   (ADR-0012 §2 — a rejection without a reason is refused, so the form does
   not offer one). */
function ApprovalButtons({ disabled, onDecide }) {
  const { t } = useLang();
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, marginTop: 6 }}>
      {!rejecting && (
        <>
          <button className="btn btn-primary btn-sm" disabled={disabled} onClick={() => onDecide(true, "")}>
            <Check size={13} />{t("Approve")}
          </button>
          <button className="btn btn-secondary btn-sm" disabled={disabled} onClick={() => setRejecting(true)}>
            <X size={13} />{t("Reject")}
          </button>
        </>
      )}
      {rejecting && (
        <>
          <input
            className="input"
            style={{ width: 260 }}
            autoFocus
            placeholder={t("Why not? (required)")}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && reason.trim() && onDecide(false, reason.trim())}
          />
          <button className="btn btn-danger btn-sm" disabled={disabled || !reason.trim()} onClick={() => onDecide(false, reason.trim())}>
            {t("Reject")}
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => setRejecting(false)}>{t("Cancel")}</button>
        </>
      )}
    </div>
  );
}
